"""Тесты parser/orchestrator.py, PLAN.md Фаза 6 — сквозной прогон: листинг источника →
дедуп → классификация → сигнал. `fetch_page` мокается (без реальной сети), БД и
классификатор — настоящие (AGENTS.md раздел 5 keyword-справочники)."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from db.models import Signal
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import Classifier
from parser.fetcher import SourceUnavailable
from parser.models import Publication
from parser.orchestrator import SourceSpec, process_source, run_all


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


@pytest.fixture
def classifier() -> Classifier:
    return Classifier.load()


def _pub(source_key: str, title: str, url: str, published_at: dt.datetime | None = None) -> Publication:
    return Publication(source_key=source_key, title=title, url=url, published_at=published_at)


NOW = dt.datetime(2026, 8, 20, 6, 0, tzinfo=dt.timezone.utc)


def test_process_source_creates_signal_for_relevant_publication(
    session: Session, classifier: Classifier
) -> None:
    publications = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "постановление ветеран боевых действий выплата новый",
            "https://sfr.gov.ru/press_center/news/1",
            published_at=NOW,
        )
    ]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.ok is True
    assert result.new_signals == 1
    assert session.query(Signal).count() == 1
    signal = session.query(Signal).one()
    assert signal.source_url == "https://sfr.gov.ru/press_center/news/1"


def test_process_source_skips_irrelevant_publication(session: Session, classifier: Classifier) -> None:
    publications = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "постановление о субсидиях сельхозпроизводителям",
            "https://sfr.gov.ru/n/2",
            published_at=NOW,
        )
    ]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 0
    assert result.irrelevant == 1
    assert session.query(Signal).count() == 0


def test_process_source_dedups_on_second_run(session: Session, classifier: Classifier) -> None:
    publications = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "постановление ветеран боевых действий выплата",
            "https://sfr.gov.ru/n/3",
            published_at=NOW,
        )
    ]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    process_source(session, classifier, spec, now=NOW)
    session.commit()
    second = process_source(session, classifier, spec, now=NOW + dt.timedelta(hours=1))
    session.commit()

    assert second.duplicates == 1
    assert second.new_signals == 0
    assert session.query(Signal).count() == 1  # не создан повторно


def test_process_source_rejects_non_whitelisted_domain(session: Session, classifier: Classifier) -> None:
    publications = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "постановление ветеран боевых действий выплата",
            "https://evil.example.com/n/1",
            published_at=NOW,
        )
    ]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 0
    assert result.duplicates == 0
    assert session.query(Signal).count() == 0


def test_process_source_stops_pagination_at_window_start(session: Session, classifier: Classifier) -> None:
    """Публикации старше окна поиска не обрабатываются, обход дальше не идёт."""
    from parser.state import mark_source_processed

    window_start = NOW - dt.timedelta(hours=24)
    mark_source_processed(session, "sfr.gov.ru/press_center/news", success_at=window_start)
    session.commit()

    fresh = _pub(
        "sfr.gov.ru/press_center/news",
        "постановление ветеран боевых действий выплата",
        "https://sfr.gov.ru/n/fresh",
        published_at=NOW,
    )
    stale = _pub(
        "sfr.gov.ru/press_center/news",
        "постановление ветеран боевых действий выплата",
        "https://sfr.gov.ru/n/stale",
        published_at=window_start - dt.timedelta(hours=1),
    )
    call_count = 0

    def fetch_page(page: int = 1) -> list[Publication]:
        nonlocal call_count
        call_count += 1
        return [fresh, stale] if page == 1 else [fresh]  # страница 2 не должна запроситься

    spec = SourceSpec("sfr.gov.ru/press_center/news", fetch_page)

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 1  # только fresh
    assert call_count == 1  # остановились на первой странице


def test_process_source_stops_scanning_page_at_first_stale_item(
    session: Session, classifier: Classifier
) -> None:
    """Публикации в странице идут от новых к старым — первая же старше окна должна
    остановить проверку остальных элементов ТОЙ ЖЕ страницы, не только пагинацию."""
    from parser.state import mark_source_processed

    window_start = NOW - dt.timedelta(hours=24)
    mark_source_processed(session, "sfr.gov.ru/press_center/news", success_at=window_start)
    session.commit()

    fresh = _pub(
        "sfr.gov.ru/press_center/news",
        "постановление ветеран боевых действий выплата",
        "https://sfr.gov.ru/n/fresh",
        published_at=NOW,
    )
    stale = _pub(
        "sfr.gov.ru/press_center/news",
        "постановление ветеран боевых действий выплата",
        "https://sfr.gov.ru/n/stale",
        published_at=window_start - dt.timedelta(hours=1),
    )
    # ещё одна "свежая" публикация ПОСЛЕ старой в том же списке — реалистичный листинг
    # так никогда не выдаст (сортировка по дате), но так тест ловит именно ошибку
    # "continue вместо break": будь она, эта публикация тоже создала бы сигнал.
    fresh_after_stale = _pub(
        "sfr.gov.ru/press_center/news",
        "постановление ветеран боевых действий выплата",
        "https://sfr.gov.ru/n/fresh-after-stale",
        published_at=NOW,
    )

    spec = SourceSpec(
        "sfr.gov.ru/press_center/news", lambda page=1: [fresh, stale, fresh_after_stale] if page == 1 else []
    )

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 1  # только fresh, не fresh_after_stale
    leftover = session.query(Signal).filter(Signal.source_url == "https://sfr.gov.ru/n/fresh-after-stale")
    assert leftover.count() == 0


def test_process_source_excludes_known_static_path_without_creating_signal(
    session: Session, classifier: Classifier
) -> None:
    """PLAN.md Фаза 9 п.1 / docs/SPEC_stale_publications_filter.md: sfr.gov.ru/branches/*/info/
    — статичные справочные страницы, не публикации о событии, не должны создавать сигнал
    даже при релевантном тексте заголовка."""
    publications = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "постановление ветеран боевых действий выплата",
            "https://sfr.gov.ru/branches/77/info/~2026/08/20/1?info_category=3",
            published_at=NOW,
        )
    ]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.excluded == 1
    assert result.new_signals == 0
    assert session.query(Signal).count() == 0


def test_process_source_stops_pagination_when_page_fully_undated(
    session: Session, classifier: Classifier
) -> None:
    """Раньше отсутствие даты (`published_at is None`) полностью обходило проверку окна
    — публикация без даты не останавливала пагинацию и обрабатывалась как обычная. Если
    вся страница без дат, источник не может подтвердить, что дальше страницы попадают в
    окно — обход останавливается (docs/SPEC_stale_publications_filter.md)."""
    undated = _pub(
        "kremlin.ru/acts/news",
        "постановление ветеран боевых действий выплата",
        "https://kremlin.ru/acts/news/1",
        published_at=None,
    )
    call_count = 0

    def fetch_page(page: int = 1) -> list[Publication]:
        nonlocal call_count
        call_count += 1
        return [undated]  # каждая "страница" без дат — не должна запрашиваться повторно

    spec = SourceSpec("kremlin.ru/acts/news", fetch_page)

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert call_count == 1  # остановились на первой же полностью недатированной странице
    assert result.new_signals == 1  # сама публикация на этой странице всё же обработана


def test_process_source_marks_unavailable_source_without_processing(
    session: Session, classifier: Classifier
) -> None:
    def fetch_page(page: int = 1):
        raise SourceUnavailable(
            url="https://sfr.gov.ru/", access="direct", last_error=Exception("boom")
        )

    spec = SourceSpec("sfr.gov.ru/press_center/news", fetch_page)

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.ok is False
    assert result.error is not None


def test_process_source_catches_unexpected_errors_not_just_source_unavailable(
    session: Session, classifier: Classifier
) -> None:
    """Регрессия: неверная конфигурация (напр. отсутствующий RU_PROXY_URL — ValueError
    из parser/fetcher.py, не SourceUnavailable) роняла весь суточный прогон целиком,
    обнаружено вживую при первом прогоне `python -m parser`."""

    def fetch_page(page: int = 1):
        raise ValueError("access='ru_proxy' требует RU_PROXY_URL")

    spec = SourceSpec("publication.pravo.gov.ru", fetch_page)

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.ok is False
    assert "RU_PROXY_URL" in result.error


def test_run_all_continues_after_one_source_fails(session: Session) -> None:
    def failing(page: int = 1):
        raise SourceUnavailable(url="x", access="direct", last_error=Exception())

    def working(page: int = 1) -> list[Publication]:
        if page != 1:
            return []
        return [
            _pub(
                "mintrud.gov.ru/docs",
                "приказ инвалид пособие",
                "https://mintrud.gov.ru/docs/1",
                published_at=NOW,
            )
        ]

    specs = [
        SourceSpec("sfr.gov.ru/press_center/news", failing),
        SourceSpec("mintrud.gov.ru/docs", working),
    ]

    results = run_all(session, specs=specs, now=NOW)

    assert {r.source_key: r.ok for r in results} == {
        "sfr.gov.ru/press_center/news": False,
        "mintrud.gov.ru/docs": True,
    }
    assert session.query(Signal).count() == 1


def test_non_paginated_spec_fetches_only_once(session: Session, classifier: Classifier) -> None:
    call_count = 0

    def fetch_page(page: int = 1) -> list[Publication]:
        nonlocal call_count
        call_count += 1
        return [_pub("tass.ru", "участник СВО компенсация", "https://tass.ru/n/1", published_at=NOW)]

    spec = SourceSpec("tass.ru", fetch_page, paginated=False)

    process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert call_count == 1
