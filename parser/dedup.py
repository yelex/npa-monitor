"""Канонизация URL перед дедупом публикаций, PLAN.md Фаза 9 п.2,
docs/SPEC_url_canonicalization.md.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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
