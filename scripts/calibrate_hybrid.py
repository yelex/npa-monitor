"""Калибровка порогов Stage B (T_cos, T_bm25, зазор RRF), docs/SPEC_hybrid_classifier.md,
раздел «Калибровка и приёмка».

Заготовка, не финальная калибровка: пороги передаются аргументами (не хардкод в коде),
скрипт только печатает precision/recall на трёх выборках при заданных порогах — без
авто-тюнинга (спека это прямо исключает: подбор порогов автоматическим перебором на
выборке из 40-60 примеров — верный способ подогнать пороги под сам held-out, а не под
класс проблемы, план `/tmp/claude_hybrid_classifier_plan.md`, раздел 5).

- Выборка 1 (не-регрессия) — реальные сигналы БД (`--db`), полный `Classifier.classify`
  (Stage A+B вместе). Работает уже сейчас против боевой/тестовой БД.
- Выборки 2 (held-out «потерянные» Stage A) и 3 (контроль нерелевантных) — см. TODO у
  `SAMPLE_2_HELD_OUT`/`SAMPLE_3_CONTROL` ниже: это ЗАГЛУШКИ на тестовых фикстурах (тот
  же класс примеров, что `tests/test_hybrid_classifier.py`), а не собранная спекой
  реальная выборка (20-30 заголовков pravo.gov.ru по канцеляризмам + 15-20
  контрольных нерелевантных, AGENTS.md раздел 16 п.15) — это отдельная задача сбора
  данных, не автоматизируется этим скриптом.

Запуск:
    python -m scripts.calibrate_hybrid
    python -m scripts.calibrate_hybrid --t-cos 0.55 --t-bm25 3.0 --rrf-gap 0.0002
    python -m scripts.calibrate_hybrid --db data/npa_monitor.db
"""
from __future__ import annotations

import argparse
import dataclasses

from config import Settings
from db.catalog import (
    ClassificationKeywords,
    LifeSituation,
    load_classification_keywords,
    load_life_situations,
)
from db.enums import RejectionReason, SignalStatus
from db.models import Signal
from db.session import make_engine, make_session_factory, session_scope
from parser.hybrid_classifier import DEFAULT_RRF_GAP, DEFAULT_T_BM25, DEFAULT_T_COS, classify_hybrid
from parser.models import Publication
from parser.ru_stem import find_matches


@dataclasses.dataclass(frozen=True)
class Example:
    title: str
    source_key: str
    expect_relevant: bool


# --- Выборка 2 (held-out, спека раздел «Калибровка и приёмка») ---
# ЗАГЛУШКА: спека требует 20-30 РЕАЛЬНЫХ заголовков pravo.gov.ru, которые Stage A не
# находит (искать по канцеляризмам «в связи с прохождением»/«в связи с проведением»/
# «отдельным категориям граждан» на pravo.gov/Яндекс) — не собрано, задача отдельная.
# Ниже — тот же класс фикстур, что tests/test_hybrid_classifier.py::KAMCHATKA_STYLE_TITLE,
# только чтобы скрипт был исполним без держателя реальных данных.
SAMPLE_2_HELD_OUT: tuple[Example, ...] = (
    Example(
        "Постановление Правительства Камчатского края № 412-П о дополнительных выплатах "
        "отдельным категориям граждан, принимающим участие в специальной военной операции",
        "publication.pravo.gov.ru/document/4100202608240009",
        True,
    ),
    Example(
        "Распоряжение о предоставлении дополнительных льгот лицам, удостоенным звания "
        "ветерана боевых действий",
        "publication.pravo.gov.ru/document/1",
        True,
    ),
    Example(
        "Приказ о ежемесячной денежной выплате гражданам, признанным инвалидами по "
        "общему заболеванию",
        "publication.pravo.gov.ru/document/2",
        True,
    ),
    Example(
        "Распоряжение о дополнительных выплатах гражданам, заключившим контракт о "
        "добровольном содействии в выполнении задач специальной военной операции",
        "publication.pravo.gov.ru/document/3",
        True,
    ),
)

# --- Выборка 3 (контроль, спека раздел «Калибровка и приёмка») ---
# ЗАГЛУШКА: спека требует 15-20 РЕАЛЬНЫХ нерелевантных заголовков; см. комментарий к
# SAMPLE_2_HELD_OUT. Реальный ложный кейс ("пятилетка Китая") — AGENTS.md раздел 16 п.15.
SAMPLE_3_CONTROL: tuple[Example, ...] = (
    Example("Постановление о пятилетнем плане развития рыболовства Китая", "rg.ru", False),
    Example(
        "Отчёт о ходе реализации национального проекта «Экология» в 2026 году",
        "rg.ru",
        False,
    ),
    Example(
        "Постановление об утверждении порядка предоставления субсидий субъектам малого "
        "предпринимательства",
        "government.ru",
        False,
    ),
    Example(
        "Распоряжение о создании рабочей группы по вопросам развития туризма",
        "government.ru",
        False,
    ),
)


def _publication(example: Example) -> Publication:
    return Publication(
        source_key=example.source_key,
        title=example.title,
        url=f"https://{example.source_key}",
        published_at=None,
    )


def _is_relevant_with_thresholds(
    pub: Publication,
    *,
    life_situations: tuple[LifeSituation, ...],
    keywords: ClassificationKeywords,
    t_cos: float,
    t_bm25: float,
    rrf_gap: float,
) -> bool:
    """Тот же Stage A + Stage B поток, что `parser.classifier.Classifier.explain`, но с
    порогами Stage B, переданными аргументами (не смешивается с `Classifier.classify`,
    который всегда использует пороги по умолчанию `parser.hybrid_classifier`)."""
    text = f"{pub.title} {pub.summary or ''}"
    text_lower = text.lower()

    categories = tuple(
        situation.category for situation in life_situations if find_matches(text_lower, situation.keywords)
    )
    topic_block_matches = find_matches(text_lower, keywords.topic_block)
    document_marker_matches = find_matches(text_lower, keywords.document_markers)

    if not categories and (topic_block_matches or document_marker_matches):
        decision = classify_hybrid(text, t_cos=t_cos, t_bm25=t_bm25, rrf_gap=rrf_gap)
        if decision is not None and decision.accepted:
            categories = (decision.category,)

    return bool(categories) and bool(topic_block_matches)


@dataclasses.dataclass
class Metrics:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fp
        return self.tp / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.fn
        return self.tp / denom if denom else None

    def add(self, *, predicted: bool, expected: bool) -> None:
        if predicted and expected:
            self.tp += 1
        elif predicted and not expected:
            self.fp += 1
        elif not predicted and expected:
            self.fn += 1
        else:
            self.tn += 1


def _evaluate(
    examples: tuple[Example, ...],
    *,
    life_situations: tuple[LifeSituation, ...],
    keywords: ClassificationKeywords,
    t_cos: float,
    t_bm25: float,
    rrf_gap: float,
) -> Metrics:
    metrics = Metrics()
    for example in examples:
        predicted = _is_relevant_with_thresholds(
            _publication(example),
            life_situations=life_situations,
            keywords=keywords,
            t_cos=t_cos,
            t_bm25=t_bm25,
            rrf_gap=rrf_gap,
        )
        metrics.add(predicted=predicted, expected=example.expect_relevant)
    return metrics


def _print_metrics(name: str, metrics: Metrics, *, target: str) -> None:
    print(f"{name} (n={metrics.tp + metrics.fp + metrics.tn + metrics.fn}):")
    print(f"  tp={metrics.tp} fp={metrics.fp} tn={metrics.tn} fn={metrics.fn}")
    precision = metrics.precision
    recall = metrics.recall
    no_predictions = "  precision=n/a (нет положительных прогнозов)"
    no_positives = "  recall=n/a (нет ожидаемых положительных)"
    print(f"  precision={precision:.2f}" if precision is not None else no_predictions)
    print(f"  recall={recall:.2f}" if recall is not None else no_positives)
    print(f"  цель спеки: {target}")
    print()


def _load_regression_sample(db_path: str) -> tuple[Example, ...]:
    """Выборка 1 (план, раздел 5): сигналы БД, метка релевантности — по исходу
    эксперта, не по хранившемуся `is_relevant` (сигналы физически не содержат false
    negatives сегодняшнего классификатора — см. предупреждение спеки/плана: годится
    только для проверки «не сломали то, что уже работало»). `Дубликат` исключается —
    это дедуп, отдельная задача, не про ЖС."""
    settings = Settings(database_path=db_path)
    engine = make_engine(settings.database_path)
    factory = make_session_factory(engine)
    examples: list[Example] = []
    with session_scope(factory) as session:
        for signal in session.query(Signal).all():
            if signal.rejection_reason == RejectionReason.DUPLICATE:
                continue
            if signal.rejection_reason in (RejectionReason.NOT_TARGET_CATEGORY, RejectionReason.NOT_NPA):
                expect_relevant = False
            elif signal.status in (SignalStatus.SENT_TO_AGENT, SignalStatus.COMPLETED):
                expect_relevant = True
            else:
                continue  # промежуточный статус, эксперт ещё не решил — не участвует в метрике
            examples.append(
                Example(
                    title=signal.title or "",
                    source_key=(signal.source_url or "").split("//")[-1],
                    expect_relevant=expect_relevant,
                )
            )
    return tuple(examples)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--t-cos", type=float, default=DEFAULT_T_COS, help=f"порог cos_top1 (по умолчанию {DEFAULT_T_COS})"
    )
    parser.add_argument(
        "--t-bm25",
        type=float,
        default=DEFAULT_T_BM25,
        help=f"порог bm25_top1 (по умолчанию {DEFAULT_T_BM25})",
    )
    parser.add_argument(
        "--rrf-gap",
        type=float,
        default=DEFAULT_RRF_GAP,
        help=f"минимальный зазор RRF (по умолчанию {DEFAULT_RRF_GAP})",
    )
    parser.add_argument(
        "--db", default=None, help="путь к БД для выборки 1 (не-регрессия); по умолчанию — без выборки 1"
    )
    args = parser.parse_args()

    print(f"Пороги: T_cos={args.t_cos} T_bm25={args.t_bm25} RRF-зазор={args.rrf_gap}\n")

    life_situations = load_life_situations()
    keywords = load_classification_keywords()
    eval_kwargs = dict(
        life_situations=life_situations,
        keywords=keywords,
        t_cos=args.t_cos,
        t_bm25=args.t_bm25,
        rrf_gap=args.rrf_gap,
    )

    if args.db:
        sample_1 = _load_regression_sample(args.db)
        if sample_1:
            _print_metrics(
                "Выборка 1 (не-регрессия, БД)", _evaluate(sample_1, **eval_kwargs), target="precision >= 0.90"
            )
        else:
            print("Выборка 1 (не-регрессия, БД): сигналов с решённым исходом не найдено\n")
    else:
        print("Выборка 1 (не-регрессия) пропущена — передай --db для прогона против БД\n")

    _print_metrics(
        "Выборка 2 (held-out, ЗАГЛУШКА — см. докстринг модуля)",
        _evaluate(SAMPLE_2_HELD_OUT, **eval_kwargs),
        target="recall >= 0.60",
    )
    _print_metrics(
        "Выборка 3 (контроль нерелевантных, ЗАГЛУШКА — см. докстринг модуля)",
        _evaluate(SAMPLE_3_CONTROL, **eval_kwargs),
        target="precision >= 0.85",
    )


if __name__ == "__main__":
    main()
