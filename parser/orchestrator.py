"""Ежедневный оркестратор обхода источников, PLAN.md Фаза 6.

Связывает уже написанные части парсера в единый прогон: доверстывание окна
(`parser/state.py`) → обход листинга (`parser/sources/*`) → белый список домена
(`parser/filters.py`) → дедуп (`db.service.register_document_seen`) → классификация
(`parser/classifier.py`) → сборка сигнала (`parser/signals.py`) → отметка источника
обработанным (`parser/state.py`).

Источники «иные» (`parser/sources/other.py` — СМИ/агрегаторы, AGENTS.md раздел 4:
«контекст, не единственное основание для сигнала») включены в общий обход наравне с
остальными: их домены не входят ни в один региональный справочник и не в федеральный
список, поэтому `parser.classifier.detect_region` для них всегда вернёт «Не определён»,
что по разделу 4.1 AGENTS.md принудительно ограничивает приоритет средним — это и есть
фактическое ограничение их «веса» без отдельного механизма подтверждения (которого нет
и не требовался явно ни одним пунктом ТЗ).

Фильтр «только текстовые публикации» (AGENTS.md раздел 4.6, `parser/filters.py::
is_text_content`) здесь не применяется: он проверяет Content-Type фактически
скачанной страницы, а адаптеры `sources/*` дают только заголовок/ссылку/дату из
листинга, не скачивая каждую статью — все источники в `data/sources.yaml` и так
текстовые (новостные ленты, реестры документов), видео/подкаст-платформ среди них нет.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections.abc import Callable, Iterable

from sqlalchemy.orm import Session

from config import get_settings
from db.catalog import all_domains
from db.enums import Priority
from db.service import link_document_to_signal, recent_documents_with_titles, register_document_seen
from parser.classifier import Classifier
from parser.dedup import TITLE_DEDUP_WINDOW, canonicalize_url, find_duplicate_title
from parser.fetcher import SourceUnavailable
from parser.filters import NOT_NPA_ACTIVITY_REASON, is_domain_whitelisted, is_excluded_path, is_news_activity_noise
from parser.llm import ClassifierLLMClient, get_default_client
from parser.llm_priority import apply_refinements, chunk_signal_ids, log_refinement, refine_priorities_batch
from parser.models import Publication
from parser.signals import build_signal, is_review_aggregate
from parser.sources import government, kremlin, mintrud, mos_ru, msupport_dszn, other, pravo_gov, sfr
from parser.state import fetch_window_start, mark_source_failed, mark_source_processed

log = logging.getLogger(__name__)

MAX_PAGES_PER_SOURCE = 5  # защита от бесконечной пагинации; окна обхода небольшие
# docs/SPEC_pravo_gov_day_by_day.md: эскалация periodType daily->weekly->monthly
# (docs/SPEC_pravo_gov_pagination_depth.md) заменена обходом по календарным дням МСК
# (`periodType=day&date=DD.MM.YYYY`) — `weekly`/`monthly` физически не окно "последние
# N дней от now", а фиксированный текущий период, недостижимый для catch-up старше
# текущей недели/месяца (диагностика 02.09, /tmp/claude_pravo_diag.md). Живьём — ~150
# документов/день (~5 стр. по 30 позиций), потолок с запасом.
PRAVO_GOV_DAY_MAX_PAGES = 30


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    source_key: str
    fetch_page: Callable[..., list[Publication]]  # fetch_page(page=N) -> [Publication, ...]
    paginated: bool = True  # False — источник без пагинации (напр. RSS), одна страница
    max_pages: int = MAX_PAGES_PER_SOURCE  # предохранитель классической (не по дням) пагинации
    # docs/SPEC_pravo_gov_pagination_depth.md, п.1: допуск на неидеальную сортировку
    # листинга источника — публикация старше окна на величину до допуска ещё не
    # останавливает обход (0 для большинства источников — сохраняет прежнее поведение).
    window_tolerance: dt.timedelta = dt.timedelta(0)
    # docs/SPEC_pravo_gov_day_by_day.md: источники с обходом по календарным дням (МСК),
    # а не классической пагинацией по "текущему периоду" (см. PRAVO_GOV_DAY_MAX_PAGES
    # выше — periodType=weekly/monthly на pravo.gov.ru оказался нерабочим для catch-up).
    # Если задано — process_source игнорирует max_pages/window_tolerance/paginated и
    # обходит `day_plan(window_start, now)` день за днём, каждый со своим предохранителем
    # `day_max_pages`. None у источников без понятия календарного дня (как раньше).
    day_plan: Callable[[dt.datetime, dt.datetime], list[dt.date]] | None = None
    day_max_pages: int = PRAVO_GOV_DAY_MAX_PAGES


@dataclasses.dataclass
class SourceRunResult:
    source_key: str
    ok: bool
    new_signals: int = 0
    duplicates: int = 0
    irrelevant: int = 0
    excluded: int = 0
    reviews: int = 0
    news_activity: int = 0
    error: str | None = None
    # PLAN.md Фаза 11 / docs/SPEC_llm_priority.md: id сигналов этого источника с
    # regex-приоритетом MEDIUM/LOW, созданных за этот прогон — материал для
    # LLM-приоритизации после основного цикла (`run_all`), HIGH туда не попадает.
    medium_low_signal_ids: list[int] = dataclasses.field(default_factory=list)


def build_source_specs(*, ru_proxy_url: str | None = None) -> list[SourceSpec]:
    """Федеральные + региональные (Москва) листинги. RSNET-домены (`ru_proxy`,
    AGENTS.md раздел 4/docs/STAGE0.md раздел 2.1) получают `ru_proxy_url`."""
    return [
        SourceSpec(sfr.SOURCE_KEY, sfr.fetch_news),
        SourceSpec(mintrud.SOURCE_KEY, mintrud.fetch_docs),
        SourceSpec(mos_ru.SOURCE_KEY, mos_ru.fetch_documents),
        SourceSpec(msupport_dszn.SOURCE_KEY, msupport_dszn.fetch_news),
        SourceSpec(
            pravo_gov.SOURCE_KEY,
            lambda page=1, period="day", date=None: pravo_gov.fetch_documents(
                page=page, period=period, date=date, ru_proxy_url=ru_proxy_url
            ),
            day_plan=pravo_gov.build_day_plan,
            day_max_pages=PRAVO_GOV_DAY_MAX_PAGES,
        ),
        SourceSpec(
            kremlin.SOURCE_KEY, lambda page=1: kremlin.fetch_news(page=page, ru_proxy_url=ru_proxy_url)
        ),
        SourceSpec(
            government.SOURCE_KEY,
            lambda page=1: government.fetch_docs(page=page, ru_proxy_url=ru_proxy_url),
        ),
    ]


def build_other_source_specs() -> list[SourceSpec]:
    """«Иные» источники (RSS) — отдельно, `other.fetch_feed` без пагинации."""
    return [
        SourceSpec(domain, lambda page=1, _domain=domain: other.fetch_feed(_domain), paginated=False)
        for domain in other.FEEDS
    ]


def process_source(
    session: Session,
    classifier: Classifier,
    spec: SourceSpec,
    *,
    whitelisted_domains: set[str] | None = None,
    now: dt.datetime | None = None,
    llm_client: ClassifierLLMClient | None = None,
) -> SourceRunResult:
    now = now or dt.datetime.now(dt.timezone.utc)
    whitelisted_domains = whitelisted_domains if whitelisted_domains is not None else all_domains()
    window_start = fetch_window_start(session, spec.source_key, now=now)

    result = SourceRunResult(source_key=spec.source_key, ok=True)
    window_covered = True  # остаётся True, только если обход подтвердил полное покрытие окна
    try:
        if spec.day_plan is not None:
            window_covered = _process_source_by_day(
                session, classifier, spec, window_start, now, whitelisted_domains, result, llm_client
            )
        else:
            window_covered = _process_source_paginated(
                session, classifier, spec, window_start, now, whitelisted_domains, result, llm_client
            )
    except SourceUnavailable as exc:
        # AGENTS.md раздел 12: «Источник недоступен — 3 попытки, затем пропуск до
        # следующего цикла» — fetch() их уже сделал; здесь просто не отмечаем источник
        # обработанным, чтобы окно в следующий раз включило пропущенный период
        # (доверстывание, parser/state.py).
        log.warning("источник недоступен, пропуск до следующего цикла: %s", exc)
        mark_source_failed(session, spec.source_key, at=now)
        return SourceRunResult(source_key=spec.source_key, ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        # Любая другая ошибка одного источника (неверная конфигурация вроде отсутствующего
        # RU_PROXY_URL, баг в парсинге конкретного адаптера и т.п.) не должна ронять весь
        # суточный прогон — обнаружено вживую: неверно настроенный RU_PROXY_URL для
        # pravo_gov прерывал обход всех источников после него. С полным traceback в лог
        # (не как SourceUnavailable — это неожиданная ошибка, а не штатный «сайт лежит»).
        log.exception("неожиданная ошибка при обходе источника %s, пропуск", spec.source_key)
        mark_source_failed(session, spec.source_key, at=now)
        return SourceRunResult(source_key=spec.source_key, ok=False, error=str(exc))

    if window_covered:
        mark_source_processed(session, spec.source_key, success_at=now)
    return result


def _process_source_paginated(
    session: Session,
    classifier: Classifier,
    spec: SourceSpec,
    window_start: dt.datetime,
    now: dt.datetime,
    whitelisted_domains: set[str],
    result: SourceRunResult,
    llm_client: ClassifierLLMClient | None,
) -> bool:
    """Классический обход источника постраничной пагинацией, стоп по дате/предохранителю
    страниц (docs/SPEC_pravo_gov_pagination_depth.md, п.1) — всё, кроме pravo_gov
    (см. `_process_source_by_day` и `SourceSpec.day_plan`)."""
    page = 1
    while page <= spec.max_pages:
        publications = spec.fetch_page(page=page)
        if not publications:
            break

        reached_window_start = False
        for pub in publications:
            if pub.published_at is not None and pub.published_at < window_start - spec.window_tolerance:
                # Публикации идут от новых к старым — как только встретили одну
                # старше окна, все следующие в этой странице тоже старше, дальше
                # не проверяем (не только эту страницу — весь источник, ниже).
                reached_window_start = True
                log.debug(
                    "публикация старше окна поиска (%s < %s), обход источника остановлен: %r",
                    pub.published_at,
                    window_start,
                    pub.title,
                )
                break
            _process_publication(session, classifier, pub, whitelisted_domains, result, now, llm_client)

        # PLAN.md Фаза 9 п.1 / docs/SPEC_stale_publications_filter.md: если ни одна
        # публикация на странице не дала машиночитаемую дату, проверка «старше окна»
        # выше вообще не срабатывает — без этой защиты обход мог бы бесконтрольно
        # углубляться в пагинацию на источниках, где разметка старых страниц не
        # содержит даты (риск многолетних публикаций, прошедших как «новые»).
        all_undated = bool(publications) and all(pub.published_at is None for pub in publications)
        if all_undated:
            log.warning(
                "источник %s: страница %d полностью без дат публикации — "
                "обход источника остановлен (не может подтвердить окно поиска)",
                spec.source_key,
                page,
            )

        if reached_window_start or all_undated or not spec.paginated:
            return True

        page += 1

    # docs/SPEC_pravo_gov_pagination_depth.md, п.1: предохранитель max_pages сработал
    # раньше, чем обход подтвердил достижение начала окна — часть окна может остаться
    # необойдённой, источник НЕ отмечается обработанным (доверстывание подхватит его на
    # следующем прогоне с тем же window_start). Естественный конец листинга (пустая
    # страница) уже вернул True выше, до этой строки.
    if page > spec.max_pages:
        log.warning(
            "источник %s: достигнут предохранитель страниц (%d) раньше начала окна "
            "поиска (%s) — источник не отмечен обработанным, доверстывание при "
            "следующем прогоне",
            spec.source_key,
            spec.max_pages,
            window_start,
        )
        return False
    return True


def _process_source_by_day(
    session: Session,
    classifier: Classifier,
    spec: SourceSpec,
    window_start: dt.datetime,
    now: dt.datetime,
    whitelisted_domains: set[str],
    result: SourceRunResult,
    llm_client: ClassifierLLMClient | None,
) -> bool:
    """docs/SPEC_pravo_gov_day_by_day.md: обход по календарным дням МСК вместо эскалации
    periodType daily->weekly->monthly (docs/SPEC_pravo_gov_pagination_depth.md) —
    диагностика 02.09 (/tmp/claude_pravo_diag.md) показала, что `periodType=weekly/
    monthly` на pravo.gov.ru — фиксированный текущий календарный период, а не окно
    "последние N дней от now", и физически не отдаёт документы старше текущей недели/
    месяца ни на какой странице. `periodType=day&date=DD.MM.YYYY` — единственный
    подтверждённый вживую способ достать произвольный день из прошлого.

    Каждый день `spec.day_plan(window_start, now)` пагинируется отдельно, со своим
    предохранителем `spec.day_max_pages`. Окно считается покрытым, только если КАЖДЫЙ
    день дошёл до естественного конца листинга (пустая страница) — если хотя бы один
    день исчерпал предохранитель, источник в целом не отмечается обработанным (весь
    диапазон дней будет повторён на следующем прогоне; дедуп по URL делает повтор
    дешёвым для уже полностью обойдённых дней)."""
    window_covered = True
    for day in spec.day_plan(window_start, now):
        date_str = day.strftime("%d.%m.%Y")
        page = 1
        day_covered = False
        while page <= spec.day_max_pages:
            publications = spec.fetch_page(page=page, period="day", date=date_str)
            if not publications:
                day_covered = True
                break
            for pub in publications:
                _process_publication(session, classifier, pub, whitelisted_domains, result, now, llm_client)
            page += 1

        if not day_covered:
            window_covered = False
            log.warning(
                "источник %s: день %s — исчерпан предохранитель страниц (%d), день не "
                "покрыт полностью, источник не будет отмечен обработанным",
                spec.source_key,
                date_str,
                spec.day_max_pages,
            )
    return window_covered


def _process_publication(
    session: Session,
    classifier: Classifier,
    pub: Publication,
    whitelisted_domains: set[str],
    result: SourceRunResult,
    now: dt.datetime,
    llm_client: ClassifierLLMClient | None,
) -> None:
    log.debug("публикация: %r (%s) %s", pub.title, pub.published_at, pub.url)

    if not is_domain_whitelisted(pub.url, whitelisted_domains):
        log.debug("  домен не в белом списке источников — пропуск")
        return

    if is_excluded_path(pub.url):
        result.excluded += 1
        log.debug("  URL — известная статичная/справочная страница, не событие — пропуск")
        return

    # docs/SPEC_news_activity_filter.md: новости о деятельности чиновников/криминал из
    # СМИ (tass/ria/rg), похожие на релевантные по ЖС-словам в заголовке, но без
    # отношения к конкретному НПА — pre-filter по заголовку/описанию, до дедупа и
    # классификации (по образцу is_excluded_path выше). Помечается фиксированным тегом
    # NOT_NPA_ACTIVITY_REASON — отличать от ручной чистки эксперта в логах/статистике.
    if is_news_activity_noise(pub.url, pub.title, pub.summary):
        result.news_activity += 1
        log.debug("  новость о деятельности, не НПА (%s) — пропуск", NOT_NPA_ACTIVITY_REASON)
        return

    # PLAN.md Фаза 9 п.2 / docs/SPEC_url_canonicalization.md: дедуп по канонизированному
    # URL (без www./схемы/шумовых query-параметров), не по сырому pub.url — иначе
    # `?index=N`-варианты того же документа проходят как разные публикации. Сигналу
    # (build_signal ниже) при этом всё равно передаётся оригинальный pub.url — эксперт
    # должен видеть реальную ссылку источника, канонизация нужна только для сравнения.
    document, created = register_document_seen(
        session, source_key=pub.source_key, doc_url=canonicalize_url(pub.url), title=pub.title
    )
    if not created:
        result.duplicates += 1
        log.debug("  уже обработана ранее (дубликат по URL) — пропуск")
        return

    # PLAN.md Фаза 9 п.2 / docs/SPEC_content_dedup.md: URL новый, но публикация может
    # быть той же новостью под другим URL (синдикация по поддоменам, зеркала на другом
    # домене) — вторичный слой дедупа по заголовку, проверяется только для новых URL
    # (не на каждом повторном обходе), чтобы не звать LLM впустую.
    recent = recent_documents_with_titles(
        session, since=now - TITLE_DEDUP_WINDOW, exclude_id=document.id
    )
    candidates = [(doc.id, doc.title, doc.signal_id) for doc in recent]
    match = find_duplicate_title(pub.title, candidates, llm_client=llm_client)
    if match is not None:
        _, matched_signal_id = match
        link_document_to_signal(session, document, signal_id=matched_signal_id)
        result.duplicates += 1
        log.debug("  совпадает по содержанию с ранее увиденной публикацией — пропуск")
        return

    trace = classifier.explain(pub)
    log.debug("  %s", trace.format())

    # docs/SPEC_no_reviews_no_stale_reminders.md, п.1: обзоры/агрегаторы (нет маркера
    # события 5.4, `detect_event_type` вернул REVIEW) не содержат конкретики по
    # отдельной новости — сигнал не создаётся, публикация просто пропускается.
    # Инцидент #296: дополнительно ловим обзорные URL/заголовки, которые
    # `detect_event_type` не распознал как REVIEW (см. `is_review_aggregate`).
    if is_review_aggregate(pub, trace.result):
        result.reviews += 1
        log.debug("  отфильтровано: обзор (без конкретики)")
        return

    signal = build_signal(session, pub, trace.result)
    if signal is not None:
        # Побочная находка при добавлении content-дедупа (docs/SPEC_content_dedup.md):
        # documents_seen.signal_id раньше нигде не проставлялся при создании сигнала —
        # без этого привязка последующих content-дублей к сигналу (find_duplicate_title
        # выше) всегда получала signal_id=None по цепочке. Линкуем здесь же.
        link_document_to_signal(session, document, signal_id=signal.id)
        result.new_signals += 1
        log.debug("  -> сигнал создан, id=%s", signal.id)
        if signal.priority in (Priority.MEDIUM, Priority.LOW):
            result.medium_low_signal_ids.append(signal.id)
    else:
        result.irrelevant += 1


_UNSET = object()  # отличает "llm_client не передан" (взять get_default_client()) от "явно None"
# Отличает "llm_priority_apply не передан" (взять config.get_settings()) от явного True/False
# (спека `docs/SPEC_llm_priority.md`, тот же приём, что и `_UNSET` выше).
_APPLY_UNSET = object()


def _refine_priorities(
    session: Session,
    signal_ids: list[int],
    llm_client: ClassifierLLMClient | None,
    *,
    apply: bool,
) -> None:
    """PLAN.md Фаза 11: второй проход LLM-приоритизации поверх MEDIUM/LOW сигналов
    текущего прогона, после основного цикла по источникам (докстринг `run_all`).
    По умолчанию (`apply=False`) — только структурированный лог "would change",
    БД не меняется (спека, раздел «Аудит»: неделя наблюдения перед `--apply`/
    `LLM_PRIORITY_APPLY=1`)."""
    for chunk in chunk_signal_ids(signal_ids):
        refinements = refine_priorities_batch(session, chunk, llm_client)
        if apply:
            apply_refinements(session, refinements)
        for refinement in refinements:
            log_refinement(refinement, applied=apply)
        session.commit()


def run_all(
    session: Session,
    *,
    classifier: Classifier | None = None,
    specs: Iterable[SourceSpec] | None = None,
    ru_proxy_url: str | None = None,
    now: dt.datetime | None = None,
    llm_client: ClassifierLLMClient | None = _UNSET,  # type: ignore[assignment]
    llm_priority_apply: bool | None = _APPLY_UNSET,  # type: ignore[assignment]
) -> list[SourceRunResult]:
    """Обходит все источники по очереди; сбой одного не прерывает обход остальных
    (AGENTS.md раздел 12). Коммитит после каждого источника — частичный сбой не
    откатывает уже обработанные.

    `llm_client` — вторичный дедуп по содержанию (docs/SPEC_content_dedup.md) И
    LLM-приоритизация (PLAN.md Фаза 11, ниже) — один и тот же клиент на оба назначения.
    По умолчанию не передан — берётся `parser.llm.get_default_client()` (`None`, если GLM
    не сконфигурирован — LLM необязателен). Явный `llm_client=None` отключает оба
    LLM-прохода (используется в тестах для детерминированности).

    После основного цикла по источникам — второй проход LLM-приоритизации
    (`docs/SPEC_llm_priority.md`) поверх MEDIUM/LOW сигналов этого прогона (собраны в
    `SourceRunResult.medium_low_signal_ids`), чанками по `parser.llm_priority.
    DEFAULT_CHUNK_SIZE`. `llm_priority_apply` по умолчанию не передан — берётся
    `config.get_settings().llm_priority_apply` (`False`, пока `LLM_PRIORITY_APPLY` не
    задан в `.env` — только лог "would change", БД не меняется)."""
    classifier = classifier or Classifier.load()
    if llm_client is _UNSET:
        llm_client = get_default_client()
    if llm_priority_apply is _APPLY_UNSET:
        llm_priority_apply = get_settings().llm_priority_apply
    specs = list(specs) if specs is not None else [
        *build_source_specs(ru_proxy_url=ru_proxy_url),
        *build_other_source_specs(),
    ]
    whitelisted_domains = all_domains()

    results = []
    for spec in specs:
        result = process_source(
            session, classifier, spec, whitelisted_domains=whitelisted_domains, now=now, llm_client=llm_client
        )
        session.commit()
        results.append(result)
        log.info(
            "источник %s: ok=%s новых=%d дублей=%d нерелевантных=%d исключено=%d обзоров=%d "
            "новостного_шума=%d",
            result.source_key,
            result.ok,
            result.new_signals,
            result.duplicates,
            result.irrelevant,
            result.excluded,
            result.reviews,
            result.news_activity,
        )

    medium_low_ids = [sid for result in results for sid in result.medium_low_signal_ids]
    if medium_low_ids:
        _refine_priorities(session, medium_low_ids, llm_client, apply=bool(llm_priority_apply))

    return results
