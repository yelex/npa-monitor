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

from db.catalog import all_domains
from db.service import register_document_seen
from parser.classifier import Classifier
from parser.dedup import canonicalize_url
from parser.fetcher import SourceUnavailable
from parser.filters import is_domain_whitelisted, is_excluded_path
from parser.models import Publication
from parser.signals import build_signal
from parser.sources import government, kremlin, mintrud, mos_ru, msupport_dszn, other, pravo_gov, sfr
from parser.state import fetch_window_start, mark_source_processed

log = logging.getLogger(__name__)

MAX_PAGES_PER_SOURCE = 5  # защита от бесконечной пагинации; окна обхода небольшие


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    source_key: str
    fetch_page: Callable[..., list[Publication]]  # fetch_page(page=N) -> [Publication, ...]
    paginated: bool = True  # False — источник без пагинации (напр. RSS), одна страница


@dataclasses.dataclass
class SourceRunResult:
    source_key: str
    ok: bool
    new_signals: int = 0
    duplicates: int = 0
    irrelevant: int = 0
    excluded: int = 0
    error: str | None = None


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
            lambda page=1: pravo_gov.fetch_documents(page=page, ru_proxy_url=ru_proxy_url),
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
) -> SourceRunResult:
    now = now or dt.datetime.now(dt.timezone.utc)
    whitelisted_domains = whitelisted_domains if whitelisted_domains is not None else all_domains()
    window_start = fetch_window_start(session, spec.source_key, now=now)

    result = SourceRunResult(source_key=spec.source_key, ok=True)
    try:
        for page in range(1, MAX_PAGES_PER_SOURCE + 1):
            publications = spec.fetch_page(page=page)
            if not publications:
                break

            reached_window_start = False
            for pub in publications:
                if pub.published_at is not None and pub.published_at < window_start:
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
                _process_publication(session, classifier, pub, whitelisted_domains, result)

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
                break
    except SourceUnavailable as exc:
        # AGENTS.md раздел 12: «Источник недоступен — 3 попытки, затем пропуск до
        # следующего цикла» — fetch() их уже сделал; здесь просто не отмечаем источник
        # обработанным, чтобы окно в следующий раз включило пропущенный период
        # (доверстывание, parser/state.py).
        log.warning("источник недоступен, пропуск до следующего цикла: %s", exc)
        return SourceRunResult(source_key=spec.source_key, ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        # Любая другая ошибка одного источника (неверная конфигурация вроде отсутствующего
        # RU_PROXY_URL, баг в парсинге конкретного адаптера и т.п.) не должна ронять весь
        # суточный прогон — обнаружено вживую: неверно настроенный RU_PROXY_URL для
        # pravo_gov прерывал обход всех источников после него. С полным traceback в лог
        # (не как SourceUnavailable — это неожиданная ошибка, а не штатный «сайт лежит»).
        log.exception("неожиданная ошибка при обходе источника %s, пропуск", spec.source_key)
        return SourceRunResult(source_key=spec.source_key, ok=False, error=str(exc))

    mark_source_processed(session, spec.source_key, success_at=now)
    return result


def _process_publication(
    session: Session,
    classifier: Classifier,
    pub: Publication,
    whitelisted_domains: set[str],
    result: SourceRunResult,
) -> None:
    log.debug("публикация: %r (%s) %s", pub.title, pub.published_at, pub.url)

    if not is_domain_whitelisted(pub.url, whitelisted_domains):
        log.debug("  домен не в белом списке источников — пропуск")
        return

    if is_excluded_path(pub.url):
        result.excluded += 1
        log.debug("  URL — известная статичная/справочная страница, не событие — пропуск")
        return

    # PLAN.md Фаза 9 п.2 / docs/SPEC_url_canonicalization.md: дедуп по канонизированному
    # URL (без www./схемы/шумовых query-параметров), не по сырому pub.url — иначе
    # `?index=N`-варианты того же документа проходят как разные публикации. Сигналу
    # (build_signal ниже) при этом всё равно передаётся оригинальный pub.url — эксперт
    # должен видеть реальную ссылку источника, канонизация нужна только для сравнения.
    _, created = register_document_seen(
        session, source_key=pub.source_key, doc_url=canonicalize_url(pub.url)
    )
    if not created:
        result.duplicates += 1
        log.debug("  уже обработана ранее (дубликат по URL) — пропуск")
        return

    trace = classifier.explain(pub)
    log.debug("  %s", trace.format())

    signal = build_signal(session, pub, trace.result)
    if signal is not None:
        result.new_signals += 1
        log.debug("  -> сигнал создан, id=%s", signal.id)
    else:
        result.irrelevant += 1


def run_all(
    session: Session,
    *,
    classifier: Classifier | None = None,
    specs: Iterable[SourceSpec] | None = None,
    ru_proxy_url: str | None = None,
    now: dt.datetime | None = None,
) -> list[SourceRunResult]:
    """Обходит все источники по очереди; сбой одного не прерывает обход остальных
    (AGENTS.md раздел 12). Коммитит после каждого источника — частичный сбой не
    откатывает уже обработанные."""
    classifier = classifier or Classifier.load()
    specs = list(specs) if specs is not None else [
        *build_source_specs(ru_proxy_url=ru_proxy_url),
        *build_other_source_specs(),
    ]
    whitelisted_domains = all_domains()

    results = []
    for spec in specs:
        result = process_source(session, classifier, spec, whitelisted_domains=whitelisted_domains, now=now)
        session.commit()
        results.append(result)
        log.info(
            "источник %s: ok=%s новых=%d дублей=%d нерелевантных=%d исключено=%d",
            result.source_key,
            result.ok,
            result.new_signals,
            result.duplicates,
            result.irrelevant,
            result.excluded,
        )
    return results
