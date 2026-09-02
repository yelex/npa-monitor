"""Фильтры публикаций перед дальнейшей обработкой, AGENTS.md разделы 4.6 и 13,
PLAN.md Фаза 3.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_TEXT_CONTENT_TYPE_PREFIXES = ("text/html", "text/plain", "application/xhtml+xml")
_PDF_CONTENT_TYPE = "application/pdf"

# Известные статичные/справочные страницы, ошибочно похожие на свежие публикации
# (дата в самом URL меняется при обходе, контент по сути не датирован) — PLAN.md
# Фаза 9 п.1, docs/SPEC_stale_publications_filter.md. Список, а не жёсткая проверка в
# коде обхода — расширять по мере обнаружения новых паттернов.
_EXCLUDED_URL_PATTERNS = (
    # sfr.gov.ru/branches/<регион>/info/~<дата>/<id> — регионально растиражированные
    # справочные материалы, не публикации о событии (docs/SPEC_stale_publications_filter.md).
    re.compile(r"^https?://(?:www\.)?sfr\.gov\.ru/branches/[^/]+/info/"),
    # vrf.tass.ru — агрегатор региональных СМИ (перепечатки «Популярные новости
    # России», <регион>/<издание>-ru/<id>), не источник публикаций о событии —
    # docs/SPEC_vrf_tass_aggregator_filter.md.
    re.compile(r"^https?://(?:www\.)?vrf\.tass\.ru/"),
)


def is_excluded_path(url: str) -> bool:
    """True, если `url` — известная статичная/справочная страница, не публикация о
    событии (см. `_EXCLUDED_URL_PATTERNS`) — такие публикации не должны создавать сигнал.
    """
    return any(pattern.match(url) for pattern in _EXCLUDED_URL_PATTERNS)


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_domain_whitelisted(url: str, whitelisted_domains: set[str]) -> bool:
    """AGENTS.md раздел 13: «Передаваемые/принимаемые ссылки проверяются на допустимые
    домены (белый список)». Поддомены белого списка домена считаются допустимыми
    (`www.kremlin.ru` проходит для `kremlin.ru` из справочника).
    """
    host = domain_of(url)
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in whitelisted_domains)


#: docs/SPEC_news_activity_filter.md: тег причины отсечения — фиксированная строка,
#: чтобы отличать в логах/статистике автоматический pre-filter от ручной чистки
#: эксперта (`db.enums.RejectionReason.NOT_NPA`) и от других pre-filter'ов (обзоры
#: `2abcc0f`, vrf.tass `bb416e5`).
NOT_NPA_ACTIVITY_REASON = "not_npa_activity"

# docs/SPEC_news_activity_filter.md: применяется только к новостным СМИ — это
# источники "контекста" по AGENTS.md разделу 4 ("не единственное основание для
# сигнала"), где заголовки регулярно содержат речь чиновников/статистику/криминал
# без отношения к конкретному НПА. Первоисточники (sfr.gov.ru, mos.ru, pravo.gov.ru
# и т.п.) сюда не входят — там заголовок сам по себе описывает документ.
_NEWS_ACTIVITY_DOMAINS = {"tass.ru", "ria.ru", "rg.ru"}

# Группа 1 — глаголы речи/деятельности рядом с ФИО или должностью.
_SPEECH_VERBS = (
    "рассказал", "рассказала", "рассказали",
    "заявил", "заявила", "заявили",
    "сообщил", "сообщила", "сообщили",
    "объявил", "объявила", "объявили",
    "пообещал", "пообещала", "пообещали",
    "пояснил", "пояснила", "пояснили",
)
_POSITION_MARKERS = (
    "министр", "губернатор", "мэр", "председатель", "глава",
    "вице-премьер", "премьер", "депутат", "сенатор", "омбудсмен", "уполномоченный",
)
# "Фамилия: ..." — типичный заголовок-цитата ТАСС/РИА ("Голикова: правительство...").
_NAME_COLON_RE = re.compile(r"^[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.)?\s*:")
_CAPITALIZED_WORD_RE = re.compile(r"^[А-ЯЁ][а-яё]+$")

# Группа 2 — цифровая статистика ("96%", "тысячи получают") без реквизитов НПА.
_STAT_RE = re.compile(r"\d+\s?%|\bтысяч[а-я]*\b|\bмиллион[а-я]*\b|\bмлн\b")
_NPA_REQUISITE_RE = re.compile(r"№\s?\d+|\bот\s+\d{1,2}\.\d{2}\.\d{4}\b")

# NPA-lock (docs/SPEC_news_activity_filter.md, ревью): если в заголовке+описании есть
# реквизиты НПА или упоминание вида документа — заголовок описывает конкретный акт,
# группы 1/3 (речь чиновника / криминал) не должны его отсекать, даже если попутно
# сработал глагол речи или слово "прокуратура". Пример из ревью: «Выплаты инвалидам
# вследствие военной травмы увеличены — постановление № 10 от 01.01.2026».
_NPA_LOCK_RE = re.compile(
    r"№\s?\d+"
    r"|\bот\s+\d{1,2}\.\d{2}\.\d{4}\b"
    r"|\b(?:постановлени[а-я]*|закон[а-я]*|приказ[а-я]*|распоряжени[а-я]*)\b"
)

# Группа 3 — криминал/казус (случай про конкретного человека, не про меру поддержки).
# Только словосочетания-казусы, без голых "следствие"/"прокуратура" — ревью нашло, что
# подстрочный поиск "следствие" бил по "вследствие", а "прокуратур" — по любому
# упоминанию («Прокуратура разъяснила о выплатах инвалидам»).
_CRIME_ACTION_MARKERS = (
    "оформила фиктивн", "оформил фиктивн",
    "незаконно получил", "незаконно получила", "незаконно получили",
    "мошенниц", "приговор", "уголовное дело", "осужд",
)
_CRIME_MARKER_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(marker) for marker in _CRIME_ACTION_MARKERS) + r")"
)
# "уголовное следствие" / "следствие по делу" — не голое "следствие" (бьёт "вследствие").
_CRIME_INVESTIGATION_RE = re.compile(r"\bуголовн\w*\s+след\w*|\bследстви[ея]\s+по\s+делу\b")
# "прокуратура" — только рядом с криминальным контекстом (дело/незаконно/приговор/осужд),
# не любое упоминание (прокуратура разъясняет закон о льготах — не криминал).
_PROSECUTOR_RE = re.compile(r"\bпрокуратур\w*")
_CRIME_CONTEXT_RE = re.compile(r"\bдел[а-я]*\b|\bнезаконно\b|\bприговор\w*|\bосужд\w*")


def _domain_matches(url: str, domains: set[str]) -> bool:
    host = domain_of(url)
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _has_speech_activity(title: str) -> bool:
    if _NAME_COLON_RE.match(title):
        return True
    lowered = title.lower()
    for verb in _SPEECH_VERBS:
        idx = lowered.find(verb)
        if idx == -1:
            continue
        prefix_words = title[:idx].split()
        if prefix_words and _CAPITALIZED_WORD_RE.match(prefix_words[-1]):
            return True
        if any(marker in lowered for marker in _POSITION_MARKERS):
            return True
    return False


def _has_bare_statistic(title: str, description: str) -> bool:
    if not _STAT_RE.search(title):
        return False
    return not _NPA_REQUISITE_RE.search(f"{title} {description}")


def _has_crime_marker(title: str) -> bool:
    lowered = title.lower()
    if _CRIME_MARKER_RE.search(lowered) or _CRIME_INVESTIGATION_RE.search(lowered):
        return True
    return bool(_PROSECUTOR_RE.search(lowered) and _CRIME_CONTEXT_RE.search(lowered))


def is_news_activity_noise(url: str, title: str, description: str | None = None) -> bool:
    """True, если `title` (из новостного СМИ tass/ria/rg — см. `_NEWS_ACTIVITY_DOMAINS`)
    похож на новость о деятельности/происшествии, а не на публикацию о НПА —
    docs/SPEC_news_activity_filter.md. Precision-over-recall: реагирует только на явные
    паттерны (см. группы 1–3 выше), спорные заголовки не трогает — пусть решает эксперт.

    Не первоисточники (sfr.gov.ru, mos.ru, pravo.gov.ru и т.п.) — всегда False, там
    заголовок сам по себе описывает документ, эвристика для них не откалибрована.
    """
    if not title or not _domain_matches(url, _NEWS_ACTIVITY_DOMAINS):
        return False
    description = description or ""
    # NPA-lock: если реквизиты/вид документа уже названы — это публикация про конкретный
    # акт, группы 1 и 3 её не отсекают, даже если попутно есть речевой глагол или
    # "прокуратура" (группа 2 и так требует отсутствие реквизитов — своя проверка).
    if _NPA_LOCK_RE.search(f"{title} {description}"):
        return False
    return (
        _has_speech_activity(title)
        or _has_bare_statistic(title, description)
        or _has_crime_marker(title)
    )


def is_text_content(content_type: str | None) -> bool:
    """AGENTS.md раздел 4.6: «Только текстовые публикации: новости, анонсы, карточки
    документов. Видео, подкасты, инфографика без текстового описания — не
    обрабатываются». PDF считаем текстовым контентом — `parser/pdf.py` извлекает текст
    (текстовый слой или OCR).
    """
    if not content_type:
        return False
    normalized = content_type.split(";")[0].strip().lower()
    return normalized in _TEXT_CONTENT_TYPE_PREFIXES or normalized == _PDF_CONTENT_TYPE
