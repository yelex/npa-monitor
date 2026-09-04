"""Ретроспективный поиск публикаций за произвольный период через Yandex Cloud Search
API v2 — отдельный CLI (`python -m parser.discovery_search`), не часть ежедневного
`python -m parser` (docs/SPEC_yandex_search_discovery.md, раздел 5: решение в пользу
отдельного инструмента, не встраивания в оркестратор).

Закрывает ограничение `parser/orchestrator.py`: обход листингов источников работает
только на горизонте в дни (`MAX_PAGES_PER_SOURCE`), не подходит для поиска за месяцы/
годы назад (SPEC, раздел 1) — например, для бэктеста классификатора на уже известных
изменениях НПА. Дальше по конвейеру — то же самое, что и в `orchestrator.py::
_process_publication`: белый список домена → дедуп по URL (`db.service.
register_document_seen`, общий с ежедневным обходом — общая БД, повторных сигналов не
будет) → классификация (`parser/classifier.py`) → создание сигнала
(`parser/signals.py`). Разница только в источнике публикаций: не листинг одного сайта,
а поисковая выдача сразу по всему белому списку доменов, ограниченная периодом.

Проверено вживую 2026-08-20 (SPEC, раздел 4а): оператор `date:YYYYMMDD..YYYYMMDD` в
`queryText` — точный диапазон дат; `site:d1 | site:d2 | …` по всем доменам справочника
— в одном запросе, без обнаруженного лимита на этом масштабе (14 доменов). Референс —
`measure_deepagent/revision_agent/npa_search.py` (переиспользован контракт API и
разбор base64/XML, не код целиком — в этом проекте `httpx`/`tenacity`, не `requests`).

**Обнаружено и исправлено вживую 2026-08-20 (SPEC, раздел 4б):** первая версия
ограничивалась 10 документами за вызов независимо от запрошенного `resultsCount` —
этого поля вообще нет в реальной схеме запроса (`resultsCount` — из референсной
реализации `measure_deepagent`/`auto`, сервер его молча игнорировал). Причина
ограничения — `GroupSpec.group_mode` по умолчанию `GROUP_MODE_DEEP` (группировка по
домену, 1 документ на домен на страницу). Настоящая схема найдена в открытых
protobuf-определениях API (`github.com/yandex-cloud/cloudapi`,
`yandex/cloud/searchapi/v2/search_service.proto`): нужен `groupSpec.groupMode =
GROUP_MODE_FLAT` (без группировки по домену) + `groupSpec.groupsOnPage` (1–100, реально
работает — проверено, дало 100 документов вместо 10); страница задаётся не
top-level `page`, а `query.page` (проверено: `query.page=1` даёт другую страницу
результатов, top-level `page` молча игнорировался, как и `resultsCount`).

**Ежедневный режим (`run_daily_discovery`, 2026-08-20):** пользователь подтвердил, что
биллинг Яндекса для проекта не критичен (SPEC, раздел 5) — поэтому, помимо ручного CLI
с явным периодом, есть и путь для встраивания в суточный прогон
(`parser/__main__.py`), по одному запросу на ЖС из `data/life_situations.yaml` (не
хардкод — новая ЖС в справочнике подхватывается без правки кода, AGENTS.md раздел 1).
Не сделано как `SourceSpec`/`process_source` в `orchestrator.py`: тем источникам не
нужно знать окно поиска ДО вызова `fetch_page` (объезжают всё подряд и сами решают, где
остановиться, сравнивая `published_at` с окном); Yandex Search, наоборот, требует
диапазон дат прямо в тексте запроса, до самого вызова API — окно нужно знать заранее.
Поэтому `run_daily_discovery` сам считает окно на каждую ЖС через `parser/state.py`
(тот же механизм доверстывания, что и у листинговых источников, отдельная запись
`SourceState` на ЖС: `yandex_search:<id>`) и вызывает уже готовый `run_discovery_search`
с получившимся диапазоном.

**Обнаружено и исправлено вживую 2026-08-20 (по жалобе на «обрезанные» заголовки в
боте):** заголовки многих сигналов от Yandex Search обрывались после первого слова
(«Уволенные », «Людям с ») — причина не в боте и не в БД, а в разборе ответа API:
`Element.findtext("title")`/`.text` отдаёт только текст ДО первого дочернего элемента,
а заголовки с подсветкой совпавших слов приходят как `<title>Уволенные
<hlword>ветераны</hlword> боевых действий...</title>` — весь текст после первого
`<hlword>` терялся. Для `<passage>` (сниппет) уже был правильный разбор через
`itertext()` (обходит все текстовые узлы, включая текст после дочерних элементов), для
`<title>` — нет. См. `_element_text`.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import logging
import re
from collections.abc import Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx
import tenacity
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from config import get_settings
from db.catalog import LifeSituation, all_domains, load_life_situations
from db.service import register_document_seen
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import Classifier
from parser.fetcher import SourceUnavailable, fetch
from parser.filters import is_domain_whitelisted
from parser.models import Publication
from parser.signals import build_signal, is_review_aggregate
from parser.state import fetch_window_start, mark_source_processed

# Не `logging.getLogger(__name__)`: запущенный как `python -m parser.discovery_search`
# модуль выполняется с `__name__ == "__main__"` (поведение `-m`, не путь импорта), тогда
# логгер не был бы дочерним от "parser" и `-v` (см. main()) переставал бы показывать
# построчный трейс — обнаружено вживую при первом прогоне CLI. `parser/__main__.py`
# решает то же самое так же — хардкодит "parser".
log = logging.getLogger("parser.discovery_search")

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# GroupSpec.groups_on_page: "1-100" в search_service.proto (см. докстринг модуля).
MAX_GROUPS_ON_PAGE = 100
# SearchQuery.query_text: (length) = "<=400" в search_query.proto.
MAX_QUERY_TEXT_LENGTH = 400

SOURCE_KEY = "yandex_search"

_HLWORD_RE = re.compile(r"</?hlword>")
_RETRYABLE_EXCEPTIONS = (httpx.TransportError, httpx.HTTPStatusError)

# Query-параметры постраничного просмотрщика, не различающие документ — найдено вживую
# 2026-08-20 (жалоба пользователя): `minjust.consultant.ru/.../document/60711?items=1&
# page=2`, `...&page=8`, `...&page=12` — Яндекс индексирует каждую страницу отдельным
# URL одного и того же документа, дедуп по точному URL (`db.service.
# register_document_seen`) не считал их дублями — 4 сигнала на один и тот же приказ.
_PAGINATION_QUERY_PARAMS = frozenset({"page"})


def _canonical_url(url: str) -> str:
    """Убирает параметры пагинации просмотрщика документа (см. выше) — до дедупа и до
    создания сигнала, чтобы разные страницы одного документа схлопывались в один URL
    (`register_document_seen` дедуплицирует по точному URL, не по документу)."""
    parsed = urlsplit(url)
    kept_query = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in _PAGINATION_QUERY_PARAMS
    ]
    return urlunsplit(parsed._replace(query=urlencode(kept_query)))


def _fetch_full_title(url: str) -> str | None:
    """Полный `<title>` страницы — заголовок из выдачи Yandex Search часто обрывается
    многоточием (сниппет, не полный заголовок; см. `search_yandex`, поле `title`) —
    найдено вживую 2026-08-20 по жалобе пользователя на «обрезанные» заголовки уже
    после фикса `_element_text` (тот чинил другой баг — потерю текста после
    `<hlword>`, не укорачивание самим Яндексом).

    Вызывается только для уже признанных релевантными публикаций (`run_
    discovery_search`), не для всех найденных — иначе на каждый прогон приходилось бы
    по сотне лишних сетевых запросов на ЖС вместо десятка. Best-effort: недоступный
    источник, не-текстовый контент (PDF/DOCX) или отсутствие `<title>` — не ошибка,
    просто остаёмся при заголовке от Яндекса (`None`, вызывающий код не подставляет
    значение)."""
    try:
        result = fetch(url, access="direct")
    except SourceUnavailable:
        return None
    content_type = (result.content_type or "").split(";")[0].strip().lower()
    if content_type not in ("text/html", "application/xhtml+xml"):
        return None  # PDF/DOCX и т.п. — нет <title>, разбирать нечего
    title_tag = BeautifulSoup(result.text, "html.parser").find("title")
    if title_tag is None:
        return None
    text = title_tag.get_text(strip=True)
    return text or None


def _element_text(el: ET.Element | None) -> str:
    """Полный текст элемента, включая текст после вложенных `<hlword>` (подсветка
    совпавших слов в выдаче) — **исправляет найденный вживую 2026-08-20 баг**:
    `Element.findtext()`/`.text` отдаёт только текст ДО первого дочернего элемента, а
    заголовки с подсветкой вида `<title>Уволенные <hlword>ветераны</hlword> боевых
    действий...</title>` обрывались после первого слова («Уволенные ») — весь текст
    после первого `<hlword>` терялся. `itertext()` обходит все текстовые узлы, включая
    `.tail` после каждого дочернего элемента — так уже был написан разбор `<passage>`,
    для `<title>` та же логика не была применена изначально."""
    if el is None:
        return ""
    return _HLWORD_RE.sub("", "".join(el.itertext()))


class YandexSearchUnavailable(Exception):
    """Yandex Search API не ответил (или ответил ошибкой) за 3 попытки."""


def build_query_text(query: str, domains: Iterable[str], date_from: dt.date, date_to: dt.date) -> str:
    """`(site:d1 | site:d2 | …) query date:YYYYMMDD..YYYYMMDD` — см. докстринг модуля,
    оба оператора проверены вживую."""
    site_filter = " | ".join(f"site:{domain}" for domain in sorted(domains))
    date_filter = f"date:{date_from:%Y%m%d}..{date_to:%Y%m%d}"
    return f"({site_filter}) {query} {date_filter}".strip()


@tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    retry=tenacity.retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    reraise=True,
)
def _post(client: httpx.Client, api_key: str, payload: dict) -> httpx.Response:
    response = client.post(
        SEARCH_URL,
        headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response


def search_yandex(
    query_text: str,
    *,
    api_key: str,
    folder_id: str,
    results_count: int = 50,
    page: int = 0,
    source_key: str = SOURCE_KEY,
) -> list[Publication]:
    """Публикации из выдачи Yandex Search — тот же тип, что и `parser/sources/*`, чтобы
    дальше можно было прогнать через общий классификатор/сборку сигнала без адаптации.

    `groupSpec.groupMode = GROUP_MODE_FLAT` — без группировки по домену: с дефолтным
    `GROUP_MODE_DEEP` результат обрезался до 1 документа на домен на страницу (SPEC
    раздел 4б) — не годится, когда несколько релевантных документов лежат на одном
    домене (например несколько НПА на `docs.cntd.ru` за период).

    `published_at` всегда `None`: у ответа API нет надёжной даты публикации документа
    (`<modtime>` — дата индексации Яндексом, не дата акта на портале-источнике) — окно
    поиска уже задано оператором `date:` в самом запросе, повторная фильтрация по дате
    здесь не нужна.
    """
    payload = {
        "query": {"queryText": query_text, "searchType": "SEARCH_TYPE_RU", "page": page},
        "folderId": folder_id,
        "responseFormat": "FORMAT_XML",
        "groupSpec": {
            "groupMode": "GROUP_MODE_FLAT",
            "groupsOnPage": min(results_count, MAX_GROUPS_ON_PAGE),
        },
    }
    try:
        with httpx.Client() as client:
            response = _post(client, api_key, payload)
    except _RETRYABLE_EXCEPTIONS as exc:
        raise YandexSearchUnavailable(f"Yandex Search недоступен после 3 попыток: {exc!r}") from exc

    raw_data = response.json().get("rawData")
    if not raw_data:
        return []

    root = ET.fromstring(base64.b64decode(raw_data))
    error = root.find(".//error")
    if error is not None:
        # Штатный случай "ничего не найдено" (error code="15") приходит так же, как и
        # реальная ошибка запроса — в обоих случаях для вызывающего кода это просто
        # пустая выдача, не повод падать (SPEC раздел 4а: так и было подтверждено
        # вживую на заведомо пустом диапазоне дат).
        log.debug("Yandex Search: %s", error.text)
        return []

    publications = []
    for doc in root.findall(".//doc"):
        url = doc.findtext("url")
        if not url:
            continue
        title_el = doc.find("title")
        title = _element_text(title_el)
        passage = doc.find(".//passages/passage")
        summary = _element_text(passage) or None
        publications.append(
            Publication(
                source_key=source_key,
                title=title,
                url=_canonical_url(url),
                published_at=None,
                summary=summary,
            )
        )
    return publications


@dataclasses.dataclass
class DiscoverySearchResult:
    found: int = 0
    duplicates: int = 0
    irrelevant: int = 0
    reviews: int = 0  # docs/SPEC_review_filter_discovery.md: обзоры/агрегаторы, сигнал не создаётся
    new_signals: int = 0  # в dry-run — сколько сигналов было бы создано, без записи в БД


def run_discovery_search(
    session: Session,
    classifier: Classifier,
    *,
    query: str,
    date_from: dt.date,
    date_to: dt.date,
    api_key: str,
    folder_id: str,
    domains: set[str] | None = None,
    results_count: int = 100,
    max_pages: int = 1,
    source_key: str = SOURCE_KEY,
    dry_run: bool = False,
) -> DiscoverySearchResult:
    """`max_pages` > 1 — каждая дополнительная страница это ещё один платный вызов
    API (SPEC раздел 4а, п.6: биллинг — сознательный выбор вызывающего кода, не
    автоматическое досасывание всей выдачи). Останавливается раньше `max_pages`, если
    страница пришла короче `results_count` — значит, дальше документов уже нет.

    `source_key` — метка источника в `DocumentSeen`/`Publication` (не то же самое, что
    ключ окна доверстывания в `parser/state.py` — тот передаётся отдельно вызывающим
    кодом, см. `run_daily_discovery`); по умолчанию общий `"yandex_search"` (как и
    было для ручного CLI), при вызове из `run_daily_discovery` — своя ЖС."""
    domains = domains if domains is not None else all_domains()
    query_text = build_query_text(query, domains, date_from, date_to)
    log.info("запрос Yandex Search: %s", query_text)

    result = DiscoverySearchResult()
    for page in range(max_pages):
        publications = search_yandex(
            query_text,
            api_key=api_key,
            folder_id=folder_id,
            results_count=results_count,
            page=page,
            source_key=source_key,
        )
        result.found += len(publications)

        for pub in publications:
            log.debug("публикация: %r %s", pub.title, pub.url)
            if not is_domain_whitelisted(pub.url, domains):
                # Защита от неточного `site:`-матчинга самим Яндексом — на практике не
                # встречалось (SPEC раздел 4а), но фильтр по URL по разделу 13 AGENTS.md
                # обязателен независимо от того, что уже отфильтровал сам запрос.
                log.debug("  домен не в белом списке — пропуск")
                continue

            if not dry_run:
                _, created = register_document_seen(session, source_key=pub.source_key, doc_url=pub.url)
                if not created:
                    result.duplicates += 1
                    log.debug("  уже обработана ранее (дубликат по URL) — пропуск")
                    continue

            trace = classifier.explain(pub)
            log.debug("  %s", trace.format())
            if not trace.result.is_relevant:
                result.irrelevant += 1
                continue

            # docs/SPEC_review_filter_discovery.md: тот же фильтр обзоров/агрегаторов,
            # что и в оркестраторе (`parser/orchestrator.py::_process_publication`) — до
            # `_fetch_full_title`, чтобы не тянуть заголовок страницы, которая всё равно
            # будет отброшена. Инцидент #296: `is_review_aggregate` дополнительно ловит
            # обзорные URL/заголовки, которые `detect_event_type` не распознал как REVIEW.
            if is_review_aggregate(pub, trace.result):
                result.reviews += 1
                log.debug("  отфильтровано: обзор (без конкретики)")
                continue

            # Полный заголовок — только для уже отобранных релевантных публикаций
            # (не для всех найденных: иначе сотня лишних запросов на ЖС за прогон),
            # классификация (region/priority/categories) уже посчитана по трейсу
            # выше, из исходного title+summary — на неё замена заголовка не влияет.
            full_title = _fetch_full_title(pub.url)
            if full_title:
                pub = dataclasses.replace(pub, title=full_title)

            if dry_run:
                result.new_signals += 1
                continue

            signal = build_signal(session, pub, trace.result)
            if signal is not None:
                result.new_signals += 1
                log.debug("  -> сигнал создан, id=%s", signal.id)

        if len(publications) < results_count:
            break  # короче запрошенного — дальше документов больше нет

    return result


def _bounded_keywords_query(keywords: Sequence[str], *, reserved_length: int) -> str:
    """Ключевые слова ЖС (`data/life_situations.yaml`), обрезанные с конца, чтобы
    итоговый `queryText` не превысил лимит API (`MAX_QUERY_TEXT_LENGTH`) вместе с
    site:-фильтром и `date:`-оператором (`reserved_length` — их суммарная длина,
    см. `run_daily_discovery`). Понадобилось для ЖС с длинным списком ключевых слов
    (например, «инвалиды» — 5 фраз) на полном белом списке из 14 доменов, который сам
    по себе занимает больше половины лимита."""
    budget = MAX_QUERY_TEXT_LENGTH - reserved_length
    full_text = " ".join(keywords)
    if len(full_text) <= budget:
        return full_text

    kept: list[str] = []
    length = 0
    for keyword in keywords:
        added_length = len(keyword) + (1 if kept else 0)
        if length + added_length > budget:
            break
        kept.append(keyword)
        length += added_length

    log.warning(
        "запрос ЖС обрезан до %d из %d ключевых слов (лимит Yandex Search — %d символов на запрос)",
        len(kept),
        len(keywords),
        MAX_QUERY_TEXT_LENGTH,
    )
    return " ".join(kept)


@dataclasses.dataclass
class DailyDiscoveryRunResult:
    source_key: str
    ok: bool
    result: DiscoverySearchResult | None = None
    error: str | None = None


def run_daily_discovery(
    session: Session,
    classifier: Classifier,
    *,
    api_key: str,
    folder_id: str,
    life_situations: Iterable[LifeSituation] | None = None,
    domains: set[str] | None = None,
    results_count: int = MAX_GROUPS_ON_PAGE,
    now: dt.datetime | None = None,
) -> list[DailyDiscoveryRunResult]:
    """Ежедневная подстраховка через Yandex Search — по одному запросу на ЖС из
    справочника (см. докстринг модуля). Окно поиска — с даты последнего успешного
    прогона этой ЖС (`parser/state.py::fetch_window_start`, то же доверстывание, что и
    у листинговых источников), отдельная запись `SourceState` на ЖС
    (`yandex_search:<id>`), чтобы сбой одной ЖС не сбивал окно остальных.

    Сбой одной ЖС (сеть, лимит API) не прерывает обход остальных — тот же принцип
    изоляции ошибок, что и в `orchestrator.py::process_source` (AGENTS.md раздел 12).
    Коммитит после каждой ЖС — частичный сбой не откатывает уже обработанные."""
    now = now or dt.datetime.now(dt.timezone.utc)
    life_situations = list(life_situations) if life_situations is not None else load_life_situations()
    domains = domains if domains is not None else all_domains()

    results = []
    for situation in life_situations:
        source_key = f"{SOURCE_KEY}:{situation.id}"
        window_start = fetch_window_start(session, source_key, now=now)
        date_from, date_to = window_start.date(), now.date()

        reserved_length = len(build_query_text("", domains, date_from, date_to))
        query = _bounded_keywords_query(situation.keywords, reserved_length=reserved_length)

        try:
            search_result = run_discovery_search(
                session,
                classifier,
                query=query,
                date_from=date_from,
                date_to=date_to,
                api_key=api_key,
                folder_id=folder_id,
                domains=domains,
                results_count=results_count,
                max_pages=1,
                source_key=source_key,
            )
        except YandexSearchUnavailable as exc:
            log.warning(
                "Yandex Search недоступен для ЖС %s, пропуск до следующего прогона: %s", situation.id, exc
            )
            results.append(DailyDiscoveryRunResult(source_key=source_key, ok=False, error=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001
            log.exception("неожиданная ошибка при поиске по ЖС %s, пропуск", situation.id)
            results.append(DailyDiscoveryRunResult(source_key=source_key, ok=False, error=str(exc)))
            continue

        mark_source_processed(session, source_key, success_at=now)
        session.commit()
        results.append(DailyDiscoveryRunResult(source_key=source_key, ok=True, result=search_result))
        log.info(
            "Yandex Search %s: найдено=%d дублей=%d нерелевантных=%d обзоров=%d новых сигналов=%d",
            source_key,
            search_result.found,
            search_result.duplicates,
            search_result.irrelevant,
            search_result.reviews,
            search_result.new_signals,
        )

    return results


def main() -> None:
    cli_parser = argparse.ArgumentParser(description=__doc__)
    cli_parser.add_argument(
        "--from", dest="date_from", required=True, type=dt.date.fromisoformat,
        help="начало периода, YYYY-MM-DD",
    )
    cli_parser.add_argument(
        "--to", dest="date_to", type=dt.date.fromisoformat, default=None,
        help="конец периода, YYYY-MM-DD (по умолчанию — сегодня)",
    )
    cli_parser.add_argument("--query", required=True, help="поисковый запрос: тема/реквизиты НПА")
    cli_parser.add_argument(
        "--domains", default=None,
        help="подмножество доменов через запятую (по умолчанию — весь белый список "
        "data/sources.yaml+regions.yaml)",
    )
    cli_parser.add_argument(
        "--results-count", type=int, default=MAX_GROUPS_ON_PAGE,
        help=f"документов на страницу выдачи (макс. {MAX_GROUPS_ON_PAGE})",
    )
    cli_parser.add_argument(
        "--max-pages", type=int, default=1,
        help="сколько страниц выдачи запросить (каждая — отдельный платный вызов API)",
    )
    cli_parser.add_argument(
        "--dry-run", action="store_true",
        help="показать, что было бы найдено/классифицировано, БД не менять",
    )
    cli_parser.add_argument(
        "-v", "--verbose", action="store_true", help="подробный трейс по каждой публикации"
    )
    args = cli_parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if args.verbose:
        logging.getLogger("parser").setLevel(logging.DEBUG)

    date_to = args.date_to or dt.date.today()
    domains = {d.strip() for d in args.domains.split(",")} if args.domains else None

    settings = get_settings()
    if not settings.yandex_search_api_key or not settings.yandex_search_folder_id:
        raise SystemExit("YANDEX_SEARCH_API_KEY / YANDEX_SEARCH_FOLDER_ID не заданы в .env")

    engine = make_engine(settings.database_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    classifier = Classifier.load()

    with session_factory() as session:
        result = run_discovery_search(
            session,
            classifier,
            query=args.query,
            date_from=args.date_from,
            date_to=date_to,
            api_key=settings.yandex_search_api_key,
            folder_id=settings.yandex_search_folder_id,
            domains=domains,
            results_count=args.results_count,
            max_pages=args.max_pages,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            session.commit()

    log.info(
        "готово%s: найдено=%d дублей=%d нерелевантных=%d обзоров=%d новых сигналов=%d",
        " (dry-run, БД не изменена)" if args.dry_run else "",
        result.found,
        result.duplicates,
        result.irrelevant,
        result.reviews,
        result.new_signals,
    )


if __name__ == "__main__":
    main()
