"""Дедуп публикаций, PLAN.md Фаза 9 п.2:
- `canonicalize_url` — нормализация URL (docs/SPEC_url_canonicalization.md);
- `find_duplicate_title` — вторичный слой по содержанию, точное совпадение заголовка +
  LLM (GLM) как основной инструмент для пограничных случаев (docs/SPEC_content_dedup.md).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from parser.llm import ClassifierLLMClient, LLMError

log = logging.getLogger(__name__)

# Окно, в пределах которого сравниваются заголовки на дубль по содержанию (раздел 3.3
# docs/SPEC_content_dedup.md) — с запасом относительно разрыва в 1-3 дня, наблюдённого
# в дампе пилота между дублирующими публикациями на разных поддоменах mos.ru.
TITLE_DEDUP_WINDOW = dt.timedelta(days=5)

# Query-параметры, не меняющие идентичность документа (пагинация/вкладки/варианты
# формата одного и того же документа) — список, не жёсткая проверка в коде, расширять
# по мере обнаружения новых источников шума (аналогично _EXCLUDED_URL_PATTERNS в
# parser/filters.py). См. docs/SPEC_url_canonicalization.md, раздел 2.
NOISY_QUERY_PARAMS = frozenset(
    {
        "index",  # publication.pravo.gov.ru — номер вкладки/страницы одного документа
        "word_file",  # publication.pravo.gov.ru — вариант формата того же документа
        "pdf_file",
        "items",  # minjust.consultant.ru
    }
)


def canonicalize_url(url: str) -> str:
    """Нормализует `url` для сравнения дублей: https, без `www.`, без шумовых
    query-параметров (см. `NOISY_QUERY_PARAMS`, отсортированы для стабильного порядка),
    без fragment, без завершающего `/` в пути.

    Не меняет `Signal.source_url`, показываемый эксперту — только ключ дедупа
    (`documents_seen.doc_url`), см. docs/SPEC_url_canonicalization.md.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in NOISY_QUERY_PARAMS
    ]
    query = urlencode(sorted(query_pairs))

    path = parsed.path.rstrip("/") or "/"

    return urlunparse(("https", netloc, path, "", query, ""))


_TITLE_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

# Ниже этого порога заголовки считаются заведомо разными публикациями — LLM не
# вызывается (иначе на каждую новую публикацию уходил бы бесполезный запрос).
# Выбрано с запасом: пара заголовков на разные темы обычно имеет пересечение токенов
# < 0.2 (общие частицы/предлоги), пара про одно событие с переформулировкой — заметно
# выше. См. docs/SPEC_content_dedup.md, раздел 3.2.
TITLE_CANDIDATE_MIN_SIMILARITY = 0.4

_SAME_PUBLICATION_PROMPT = """Ниже два заголовка новостных публикаций. Определи, \
идёт ли речь об одной и той же новости/публикации (например, перепечатка/синдикация \
на другом сайте или поддомене с переформулированным заголовком), или это две разные \
публикации.

Заголовок 1: {title_a}
Заголовок 2: {title_b}

Ответь одним словом: ДА (это одна и та же публикация) или НЕТ (это разные публикации)."""


def normalize_title(title: str) -> str:
    """Токенизация заголовка для сравнения: без регистра, без пунктуации/пробелов-
    вариаций. Не лемматизация (см. `parser.ru_stem` для другой задачи — совпадения
    ключевых слов) — здесь сравниваются заголовки целиком."""
    return " ".join(_TITLE_TOKEN_RE.findall(title.lower()))


def title_similarity(a: str, b: str) -> float:
    """Коэффициент Жаккара по множествам нормализованных токенов — дешёвая
    предварительная оценка «похожести» двух заголовков."""
    tokens_a = set(_TITLE_TOKEN_RE.findall(a.lower()))
    tokens_b = set(_TITLE_TOKEN_RE.findall(b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _llm_confirms_same_publication(
    title_a: str, title_b: str, llm_client: ClassifierLLMClient
) -> bool:
    prompt = _SAME_PUBLICATION_PROMPT.format(title_a=title_a, title_b=title_b)
    try:
        answer = llm_client.complete(prompt)
    except LLMError:
        log.warning("LLM недоступен при сравнении заголовков, считаем публикации разными", exc_info=True)
        return False
    return answer.strip().lower().startswith("да")


def find_duplicate_title(
    candidate_title: str,
    existing: Iterable[tuple[int, str, int | None]],
    *,
    llm_client: ClassifierLLMClient | None = None,
) -> tuple[int, int | None] | None:
    """Ищет среди `existing` (id, title, signal_id) публикацию, содержательно
    совпадающую с `candidate_title`. Точное совпадение нормализованного заголовка —
    сразу дубликат (без LLM). Иначе — берётся самый похожий кандидат в «пограничной»
    зоне (`TITLE_CANDIDATE_MIN_SIMILARITY` <= similarity < 1.0) и, если передан
    `llm_client`, решение отдаётся LLM (AGENTS.md раздел 5, решение пользователя
    2026-08-24 — GLM как основной инструмент для этого сравнения, не только fallback).
    Без `llm_client` (GLM не сконфигурирован) пограничные случаи не считаются дублями —
    деградация до точной нормализации.

    Возвращает (id, signal_id) найденного дубликата или `None`.
    """
    existing = list(existing)  # может понадобиться два прохода — материализуем один раз
    normalized_candidate = normalize_title(candidate_title)

    best_id: int | None = None
    best_signal_id: int | None = None
    best_score = 0.0

    for doc_id, title, signal_id in existing:
        if not title:
            continue
        if normalize_title(title) == normalized_candidate:
            return doc_id, signal_id
        score = title_similarity(candidate_title, title)
        if score > best_score:
            best_score, best_id, best_signal_id = score, doc_id, signal_id

    if (
        best_id is not None
        and llm_client is not None
        and TITLE_CANDIDATE_MIN_SIMILARITY <= best_score < 1.0
    ):
        matched_title = next(title for doc_id, title, _ in existing if doc_id == best_id)
        if _llm_confirms_same_publication(candidate_title, matched_title, llm_client):
            return best_id, best_signal_id

    return None
