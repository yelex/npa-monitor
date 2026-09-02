"""Stage B — гибридный fallback-классификатор ЖС (BM25 + embedding),
docs/SPEC_hybrid_classifier.md, план /tmp/claude_hybrid_classifier_plan.md.

Запускается только когда Stage A (`parser/classifier.py::match_categories`) не нашёл ни
одной ЖС-категории, но текст похож на меру/НПА (совпал тематический блок или маркер
документа) — второй проход поверх keyword-пути, не замена (precision важнее recall в
проекте, план раздел 1). Stage A самим модулем не трогается — интеграция только со
стороны `parser/classifier.py::Classifier.explain`.

Пайплайн на якорях `data/hybrid_anchors.yaml` (расширяемо без кода, как
`life_situations.yaml`):
1. cos-близость заголовка к якорям через rubert-tiny2 (ONNX, CPU) — эталон подключения
   `auto/agent/src/src/product_agent/agent/agents/semantic_retriever.py`
   (`ort.InferenceSession` + `tokenizers.Tokenizer` + mean-pooling + L2-норма, косинус =
   скалярное произведение нормированных векторов).
2. BM25 (`rank_bm25.BM25Okapi`) с той же обрезкой окончаний, что Stage A
   (`parser/ru_stem.py::stem_tokens`), плюс отсечение коротких токенов (предлоги/союзы
   3 буквы и короче) — на корпусе из ~30 якорей единичное совпадение вроде «о»/«в»
   даёт заметный BM25-скор из-за низкого N (см. отчёт реализации), это шум, не сигнал.
3. Слияние — Reciprocal Rank Fusion (`RRF_K=60`), не взвешенная сумма: cos и BM25 живут
   на разных нестабильных шкалах, BM25-скор плывёт при каждом расширении корпуса якорей
   (план, раздел 2, п.3).
4. Приём: явный лидер по RRF (зазор от второй категории >= `DEFAULT_RRF_GAP`) И хотя бы
   один сырой скор лидера выше своего порога (`DEFAULT_T_COS`/`DEFAULT_T_BM25`) — вторая
   защита от того, что два слабых, но взаимно топовых сигнала дают ложно уверенный RRF
   (план, раздел 2, п.4; проверено вживую на «пятилетка Китая»-подобных синтетических
   примерах — без порога сырых скоров чистый RRF-зазор их не отсеивает, см. отчёт
   реализации).

   Пороги по умолчанию подобраны вручную на синтетических примерах (не калибровка на
   held-out выборке из спеки) — отправная точка, а не финальное значение;
   `scripts/calibrate_hybrid.py` — печатает метрики на заданных порогах, пороги
   передаются аргументами именно для того, чтобы их можно было пересчитать без правки
   кода после сбора held-out выборки (раздел «Калибровка и приёмка» спеки).

Fallback: эмбеддер недоступен (нет `onnxruntime`/`tokenizers`/`rank_bm25`, нет весов,
любая ошибка загрузки) -> `get_stage_b_context()` возвращает `None`, Stage B
пропускается целиком (BM25 соло намеренно не гоняем — план, раздел 3), один warning при
первом обращении — тот же паттерн, что `parser/llm.py::get_default_client()`.
"""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from db.catalog import load_hybrid_anchors
from db.enums import SignalCategory
from parser.ru_stem import stem_tokens

if TYPE_CHECKING:
    from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent / "models" / "rubert-tiny2"
RRF_K = 60

# Пороги приёма — отправная точка, подобранная вручную (см. докстринг модуля), не
# финальная калибровка на held-out выборке.
DEFAULT_T_COS = 0.60
DEFAULT_T_BM25 = 4.0
DEFAULT_RRF_GAP = 0.0003

# Токены короче — предлоги/союзы («о», «в», «по», «из»...), на корпусе из ~30 якорей
# дают BM25-скор, непропорциональный смысловой значимости совпадения.
_MIN_BM25_TOKEN_LEN = 3


def _bm25_tokens(text: str) -> list[str]:
    return [t for t in stem_tokens(text) if len(t) >= _MIN_BM25_TOKEN_LEN]


@dataclasses.dataclass(frozen=True)
class CategoryScore:
    """Скоры одной ЖС-категории для одной публикации — элемент `HybridDecision.ranking`."""

    category: SignalCategory
    cos_top1: float
    cos_anchor: str
    bm25_top1: float
    bm25_anchor: str
    rrf: float


@dataclasses.dataclass(frozen=True)
class HybridDecision:
    """Результат Stage B — как принятый, так и near-miss (`accepted=False`).

    near-miss не отбрасывается (п.7 спеки: пишется в trace-лог для периодического
    аудита якорей/словаря — `parser/classifier.py::ClassificationTrace.format()`, вместо
    того, чтобы каждый пропуск класса «Камчатка» открывался только по инциденту).
    """

    accepted: bool
    category: SignalCategory  # лидер по RRF, независимо от accepted
    anchor: str  # якорь-победитель (см. classify_hybrid)
    cos_score: float
    bm25_score: float
    rrf_gap: float
    ranking: tuple[CategoryScore, ...]  # все категории по убыванию RRF


class RubertTinyEmbedder:
    """ONNX-инференс rubert-tiny2. Эталон подключения — `auto/agent/src/src/
    product_agent/agent/agents/semantic_retriever.py::SemanticRetriever._encode_onnx`
    (`ort.InferenceSession` CPU + `tokenizers.Tokenizer` + mean-pooling + L2-норма)."""

    def __init__(self, model_dir: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer as HFTokenizer

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        self._session = ort.InferenceSession(
            str(model_dir / "model_quantized.onnx"), opts, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = HFTokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.enable_padding()
        self._tokenizer.enable_truncation(max_length=64)
        self._expected_inputs = {inp.name for inp in self._session.get_inputs()}

    def encode(self, texts: list[str]) -> np.ndarray:
        """L2-нормированные mean-pooled эмбеддинги (косинус = скалярное произведение)."""
        encodings = self._tokenizer.encode_batch(texts)
        onnx_in = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in self._expected_inputs:
            onnx_in["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
        onnx_in = {k: v for k, v in onnx_in.items() if k in self._expected_inputs}
        out = self._session.run(None, onnx_in)[0]
        mask = np.expand_dims(onnx_in["attention_mask"], -1).astype(np.float32)
        vecs = np.sum(out * mask, axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        norms = np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9, None)
        return (vecs / norms).astype(np.float32)


@dataclasses.dataclass(frozen=True)
class _StageBContext:
    """Кэш, построенный один раз за процесс — эмбеддер, якоря, BM25-индекс якорей."""

    embedder: RubertTinyEmbedder
    # Порядок категорий — как в hybrid_anchors.yaml, детерминированный tie-break RRF при
    # равных рангах (см. classify_hybrid) — не set()/словарный порядок по хэшу.
    categories: tuple[SignalCategory, ...]
    anchor_texts: tuple[str, ...]
    anchor_categories: tuple[SignalCategory, ...]  # категория каждого anchor_texts[i]
    anchor_vecs: np.ndarray
    bm25: BM25Okapi


_UNSET = object()
_context: _StageBContext | None = _UNSET  # type: ignore[assignment]
_warned = False


def _warn_once(reason: str) -> None:
    global _warned
    if not _warned:
        logger.warning("Stage B (гибридный классификатор ЖС) отключён: %s", reason)
        _warned = True


def _load_context(model_dir: Path) -> _StageBContext | None:
    try:
        anchor_sets = load_hybrid_anchors()
    except Exception as exc:  # noqa: BLE001 — сломанный справочник = Stage B недоступен
        _warn_once(f"data/hybrid_anchors.yaml не загрузился: {exc!r}")
        return None

    try:
        from rank_bm25 import BM25Okapi
    except ImportError as exc:
        _warn_once(f"rank_bm25 не установлен: {exc!r}")
        return None

    if not (model_dir / "model_quantized.onnx").exists():
        _warn_once(f"веса rubert-tiny2 не найдены в {model_dir}")
        return None

    try:
        embedder = RubertTinyEmbedder(model_dir)
    except Exception as exc:  # noqa: BLE001 — нет onnxruntime/tokenizers, OOM и т.п.
        _warn_once(f"не удалось загрузить rubert-tiny2: {exc!r}")
        return None

    categories = tuple(a.category for a in anchor_sets)
    anchor_texts: list[str] = []
    anchor_categories: list[SignalCategory] = []
    for anchor_set in anchor_sets:
        for phrase in anchor_set.anchors:
            anchor_texts.append(phrase)
            anchor_categories.append(anchor_set.category)

    try:
        anchor_vecs = embedder.encode(anchor_texts)
    except Exception as exc:  # noqa: BLE001
        _warn_once(f"не удалось векторизовать data/hybrid_anchors.yaml: {exc!r}")
        return None

    bm25 = BM25Okapi([_bm25_tokens(t) for t in anchor_texts])
    return _StageBContext(
        embedder=embedder,
        categories=categories,
        anchor_texts=tuple(anchor_texts),
        anchor_categories=tuple(anchor_categories),
        anchor_vecs=anchor_vecs,
        bm25=bm25,
    )


def get_stage_b_context() -> _StageBContext | None:
    """Строит и кэширует контекст Stage B (эмбеддер, якоря, BM25-индекс) один раз за
    процесс. `None`, если Stage B недоступен — вызывающий код должен трактовать это как
    «Stage B пропущен», не как ошибку (см. докстринг модуля, «Fallback»)."""
    global _context
    if _context is _UNSET:
        _context = _load_context(MODEL_DIR)
    return _context


def reset_cache() -> None:
    """Сбросить кэш контекста — для тестов (переключение fallback-сценариев между
    тестами внутри одного процесса pytest)."""
    global _context, _warned
    _context = _UNSET
    _warned = False


def classify_hybrid(
    text: str,
    *,
    t_cos: float = DEFAULT_T_COS,
    t_bm25: float = DEFAULT_T_BM25,
    rrf_gap: float = DEFAULT_RRF_GAP,
) -> HybridDecision | None:
    """Stage B поверх текста публикации (заголовок+summary — тот же текст, что видит
    Stage A). `None` — Stage B недоступен (`get_stage_b_context()` вернул `None`);
    иначе — `HybridDecision`, возможно с `accepted=False` (near-miss).

    `t_cos`/`t_bm25`/`rrf_gap` — параметры, не константы в теле функции: нужны
    `scripts/calibrate_hybrid.py` для прогона одного и того же пайплайна с разными
    порогами без правки кода (спека, раздел «Калибровка и приёмка»)."""
    context = get_stage_b_context()
    if context is None:
        return None

    query_vec = context.embedder.encode([text])[0]
    cos_all = context.anchor_vecs @ query_vec
    bm25_all = np.asarray(context.bm25.get_scores(_bm25_tokens(text)))

    per_category: dict[SignalCategory, dict[str, float | str]] = {}
    for category in context.categories:
        idxs = [i for i, c in enumerate(context.anchor_categories) if c == category]
        best_cos_i = max(idxs, key=lambda i: cos_all[i])
        best_bm25_i = max(idxs, key=lambda i: bm25_all[i])
        per_category[category] = {
            "cos": float(cos_all[best_cos_i]),
            "cos_anchor": context.anchor_texts[best_cos_i],
            "bm25": float(bm25_all[best_bm25_i]),
            "bm25_anchor": context.anchor_texts[best_bm25_i],
        }

    # sorted() стабильна — при равных скорах (типичный случай: BM25=0.0 сразу у
    # нескольких категорий, ни один якорь не пересёкся лексически) порядок ранга
    # определяется порядком `context.categories` (из hybrid_anchors.yaml), а не хэшем
    # множества/словаря — иначе ранжирование плавает между запусками процесса.
    cats_by_cos = sorted(context.categories, key=lambda c: -per_category[c]["cos"])
    cats_by_bm25 = sorted(context.categories, key=lambda c: -per_category[c]["bm25"])
    rank_cos = {c: rank + 1 for rank, c in enumerate(cats_by_cos)}
    rank_bm25 = {c: rank + 1 for rank, c in enumerate(cats_by_bm25)}
    rrf = {c: 1 / (RRF_K + rank_cos[c]) + 1 / (RRF_K + rank_bm25[c]) for c in context.categories}

    ranking = tuple(
        CategoryScore(
            category=c,
            cos_top1=per_category[c]["cos"],
            cos_anchor=per_category[c]["cos_anchor"],
            bm25_top1=per_category[c]["bm25"],
            bm25_anchor=per_category[c]["bm25_anchor"],
            rrf=rrf[c],
        )
        for c in sorted(context.categories, key=lambda c: -rrf[c])
    )

    leader = ranking[0]
    gap = leader.rrf - ranking[1].rrf if len(ranking) > 1 else leader.rrf
    accepted = gap >= rrf_gap and (leader.cos_top1 >= t_cos or leader.bm25_top1 >= t_bm25)
    # Якорь-победитель для объяснимости эксперту (план, раздел 2, п.5):
    # если порог прошёл cos — показываем cos-якорь; если принято только за счёт bm25 —
    # bm25-якорь; в near-miss (ни один не прошёл) — cos-якорь как более информативный
    # (BM25 на коротких заголовках часто просто 0.0, его якорь пуст/шумовый).
    if leader.cos_top1 >= t_cos:
        anchor = leader.cos_anchor
    elif accepted:
        anchor = leader.bm25_anchor
    else:
        anchor = leader.cos_anchor

    return HybridDecision(
        accepted=accepted,
        category=leader.category,
        anchor=anchor,
        cos_score=leader.cos_top1,
        bm25_score=leader.bm25_top1,
        rrf_gap=gap,
        ranking=ranking,
    )
