"""Тесты parser/orchestrator.py, PLAN.md Фаза 6 — сквозной прогон: листинг источника →
дедуп → классификация → сигнал. `fetch_page` мокается (без реальной сети), БД и
классификатор — настоящие (AGENTS.md раздел 5 keyword-справочники)."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from db.enums import RejectionReason, SignalStatus
from db.models import DocumentSeen, Signal, SourceState
from db.service import transition_status
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import Classifier
from parser.fetcher import SourceUnavailable
from parser.llm import LLMError
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
            "постановление ветеран боевых действий выплата новый принят",
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


def test_process_source_skips_review_without_event_marker(session: Session, classifier: Classifier) -> None:
    # docs/SPEC_no_reviews_no_stale_reminders.md, п.1: релевантная (ЖС + тематический
    # блок совпали), но без маркера события 5.4 — обзор/агрегатор, не конкретная
    # новость. Сигнал не создаётся, публикация не копится в БД как NEW.
    publications = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "обзор мер поддержки ветеранов боевых действий: выплаты и льготы",
            "https://sfr.gov.ru/n/review-1",
            published_at=NOW,
        )
    ]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 0
    assert result.reviews == 1
    assert result.irrelevant == 0
    assert session.query(Signal).count() == 0


def test_process_source_creates_signal_for_review_with_event_marker(
    session: Session, classifier: Classifier
) -> None:
    # Тот же текст, что и выше, но с маркером события ("принят") — уже не обзор,
    # сигнал создаётся как обычно.
    publications = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "постановление о мерах поддержки ветеранов боевых действий принято",
            "https://sfr.gov.ru/n/event-1",
            published_at=NOW,
        )
    ]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 1
    assert result.reviews == 0
    assert session.query(Signal).count() == 1


def test_process_source_dedups_on_second_run(session: Session, classifier: Classifier) -> None:
    publications = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "постановление ветеран боевых действий выплата принят",
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


def test_process_source_dedups_by_canonicalized_url_across_pages(
    session: Session, classifier: Classifier
) -> None:
    # PLAN.md Фаза 9 п.2 / docs/SPEC_url_canonicalization.md: та же публикация под
    # `?index=N`-вариантом URL (pravo.gov.ru) не должна создавать второй сигнал, даже
    # если сырой URL отличается.
    page1 = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "постановление ветеран боевых действий выплата принят",
            "http://sfr.gov.ru/n/4?index=9",
            published_at=NOW,
        )
    ]
    page2 = [
        _pub(
            "sfr.gov.ru/press_center/news",
            "постановление ветеран боевых действий выплата принят",
            "https://www.sfr.gov.ru/n/4?index=10",
            published_at=NOW,
        )
    ]
    pages = {1: page1, 2: page2}
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: pages.get(page, []))

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 1
    assert result.duplicates == 1
    assert session.query(Signal).count() == 1
    # Эксперту в карточке по-прежнему виден оригинальный URL первой встреченной страницы,
    # не канонизированный — канонизация только для внутреннего сравнения дублей.
    signal = session.query(Signal).one()
    assert signal.source_url == "http://sfr.gov.ru/n/4?index=9"


class _StubLLMClient:
    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return self._answer


class _FailingLLMClient:
    def complete(self, prompt: str) -> str:
        raise LLMError("недоступен")


def test_process_source_dedups_by_exact_title_across_different_urls(
    session: Session, classifier: Classifier
) -> None:
    # PLAN.md Фаза 9 п.2 / docs/SPEC_content_dedup.md: та же публикация под другим
    # URL/поддоменом (не схлопывается canonicalize_url) с дословно тем же заголовком —
    # второй сигнал не создаётся, даже без LLM (точное совпадение заголовка).
    title = "постановление ветеран боевых действий выплата принят"
    page1 = [_pub("sfr.gov.ru/press_center/news", title, "https://a.sfr.gov.ru/n/5", published_at=NOW)]
    page2 = [_pub("sfr.gov.ru/press_center/news", title, "https://b.sfr.gov.ru/n/6", published_at=NOW)]
    pages = {1: page1, 2: page2}
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: pages.get(page, []))

    result = process_source(session, classifier, spec, now=NOW, llm_client=None)
    session.commit()

    assert result.new_signals == 1
    assert result.duplicates == 1
    assert session.query(Signal).count() == 1
    documents = session.query(DocumentSeen).order_by(DocumentSeen.id).all()
    assert len(documents) == 2
    signal_id = session.query(Signal).one().id
    assert documents[0].signal_id == signal_id
    assert documents[1].signal_id == signal_id  # второй документ привязан к тому же сигналу


def test_process_source_dedups_paraphrased_title_via_llm(session: Session, classifier: Classifier) -> None:
    original = "постановление ветеран боевых действий новая выплата принят"
    paraphrased = "новая выплата ветеранам боевых действий постановление принят"
    page1 = [_pub("sfr.gov.ru/press_center/news", original, "https://a.sfr.gov.ru/n/7", published_at=NOW)]
    page2 = [
        _pub("sfr.gov.ru/press_center/news", paraphrased, "https://b.sfr.gov.ru/n/8", published_at=NOW)
    ]
    pages = {1: page1, 2: page2}
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: pages.get(page, []))
    llm = _StubLLMClient("ДА")

    result = process_source(session, classifier, spec, now=NOW, llm_client=llm)
    session.commit()

    assert result.new_signals == 1
    assert result.duplicates == 1
    assert session.query(Signal).count() == 1
    assert llm.calls == 1


def test_process_source_creates_second_signal_when_llm_unavailable_for_paraphrased_title(
    session: Session, classifier: Classifier
) -> None:
    # Без LLM (не сконфигурирован/недоступен) переформулированный заголовок не считается
    # дублем — деградация до точной нормализации, не полный отказ от дедупа.
    original = "постановление ветеран боевых действий новая выплата принят"
    paraphrased = "новая выплата ветеранам боевых действий постановление принят"
    page1 = [_pub("sfr.gov.ru/press_center/news", original, "https://a.sfr.gov.ru/n/9", published_at=NOW)]
    page2 = [
        _pub("sfr.gov.ru/press_center/news", paraphrased, "https://b.sfr.gov.ru/n/10", published_at=NOW)
    ]
    pages = {1: page1, 2: page2}
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: pages.get(page, []))

    result = process_source(session, classifier, spec, now=NOW, llm_client=_FailingLLMClient())
    session.commit()

    assert result.new_signals == 2
    assert result.duplicates == 0
    assert session.query(Signal).count() == 2


def test_process_source_does_not_recreate_signal_for_url_rejected_earlier(
    session: Session, classifier: Classifier
) -> None:
    # PLAN.md Фаза 9 п.3 / docs/SPEC_no_recreate_after_rejection.md: расследование
    # дампа не нашло реального случая пересоздания сигнала по тому же URL после
    # отклонения (единственный воспроизводимый механизм — рансующаяся дата в пути
    # sfr.gov.ru/branches/*/info/, уже закрыт исключением пути целиком, Фаза 9 п.1).
    # Этот тест фиксирует явным контрактом то, что раньше было верно случайно:
    # documents_seen не смотрит на Signal.status — отклонённый URL не создаёт новый
    # сигнал при повторном обходе, не только «просто дубликат».
    title = "постановление ветеран боевых действий выплата принят"
    url = "https://sfr.gov.ru/n/rejected-1"
    publications = [_pub("sfr.gov.ru/press_center/news", title, url, published_at=NOW)]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    first = process_source(session, classifier, spec, now=NOW, llm_client=None)
    session.commit()
    assert first.new_signals == 1
    signal = session.query(Signal).one()

    transition_status(
        session,
        signal,
        SignalStatus.REJECTED,
        changed_by="expert1",
        rejection_reason=RejectionReason.NOT_TARGET_CATEGORY,
    )
    session.commit()

    second = process_source(session, classifier, spec, now=NOW + dt.timedelta(days=1), llm_client=None)
    session.commit()

    assert second.new_signals == 0
    assert second.duplicates == 1
    assert session.query(Signal).count() == 1  # не пересоздан
    assert session.query(Signal).one().status == SignalStatus.REJECTED  # статус не тронут


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
        "постановление ветеран боевых действий выплата принят",
        "https://sfr.gov.ru/n/fresh",
        published_at=NOW,
    )
    stale = _pub(
        "sfr.gov.ru/press_center/news",
        "постановление ветеран боевых действий выплата принят",
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
        "постановление ветеран боевых действий выплата принят",
        "https://sfr.gov.ru/n/fresh",
        published_at=NOW,
    )
    stale = _pub(
        "sfr.gov.ru/press_center/news",
        "постановление ветеран боевых действий выплата принят",
        "https://sfr.gov.ru/n/stale",
        published_at=window_start - dt.timedelta(hours=1),
    )
    # ещё одна "свежая" публикация ПОСЛЕ старой в том же списке — реалистичный листинг
    # так никогда не выдаст (сортировка по дате), но так тест ловит именно ошибку
    # "continue вместо break": будь она, эта публикация тоже создала бы сигнал.
    fresh_after_stale = _pub(
        "sfr.gov.ru/press_center/news",
        "постановление ветеран боевых действий выплата принят",
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
        "постановление ветеран боевых действий выплата принят",
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
                "приказ инвалид пособие принят",
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


# docs/SPEC_pravo_gov_pagination_depth.md: предохранитель по страницам (max_pages
# per-source), адаптивный period, фолбэк daily->weekly, источник не отмечается
# обработанным, пока обход не подтвердит покрытие окна.


def test_process_source_uses_per_source_max_pages_override(session: Session, classifier: Classifier) -> None:
    """Источник с повышенным `max_pages` (как pravo_gov) должен обойти больше страниц,
    чем дефолтный предохранитель в 5, прежде чем остановиться."""
    call_count = 0

    def fetch_page(page: int = 1) -> list[Publication]:
        nonlocal call_count
        call_count += 1
        if page > 8:
            return []
        return [
            _pub(
                "publication.pravo.gov.ru",
                f"постановление №{page} ветеран боевых действий выплата принят",
                f"http://publication.pravo.gov.ru/document/{page}",
                published_at=NOW,
            )
        ]

    spec = SourceSpec("publication.pravo.gov.ru", fetch_page, max_pages=20)

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert call_count == 9  # страницы 1..8 с данными + пустая 9-я
    assert result.new_signals == 8
    assert session.get(SourceState, "publication.pravo.gov.ru") is not None  # окно покрыто


def test_process_source_does_not_mark_processed_when_max_pages_safety_valve_hit(
    session: Session, classifier: Classifier
) -> None:
    """Если весь `max_pages` исчерпан, а начало окна поиска так и не встретилось (везде
    свежие даты) — источник не подтвердил покрытие окна и не должен отмечаться
    обработанным, чтобы доверстывание подхватило остаток на следующем прогоне."""

    def fetch_page(page: int = 1) -> list[Publication]:
        return [
            _pub(
                "publication.pravo.gov.ru",
                f"постановление №{page} ветеран боевых действий выплата принят",
                f"http://publication.pravo.gov.ru/document/{page}",
                published_at=NOW,
            )
        ]

    spec = SourceSpec("publication.pravo.gov.ru", fetch_page, max_pages=3)

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 3  # публикации всё же обработаны
    assert session.get(SourceState, "publication.pravo.gov.ru") is None  # но не отмечен обработанным


def test_process_source_adaptive_period_passed_to_fetch_page(session: Session, classifier: Classifier) -> None:
    """`resolve_period` вызывается один раз на весь прогон, и его результат передаётся в
    `fetch_page` как kwarg `period` на каждой странице."""
    calls: list[tuple[int, str]] = []

    def fetch_page(page: int = 1, period: str = "daily") -> list[Publication]:
        calls.append((page, period))
        if page == 1:
            return [
                _pub(
                    "publication.pravo.gov.ru",
                    "постановление ветеран боевых действий выплата принят",
                    "http://publication.pravo.gov.ru/document/1",
                    published_at=NOW,
                )
            ]
        return []

    spec = SourceSpec(
        "publication.pravo.gov.ru",
        fetch_page,
        resolve_period=lambda window_start, now: "weekly",
    )

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 1
    assert calls == [(1, "weekly"), (2, "weekly")]


def test_process_source_falls_back_to_weekly_when_daily_page_one_empty(
    session: Session, classifier: Classifier
) -> None:
    """docs/SPEC_pravo_gov_pagination_depth.md, п.3: `daily` пуст на первой странице —
    источник пробует `weekly` вместо того, чтобы молча решить, что публикаций нет."""
    calls: list[tuple[int, str]] = []

    def fetch_page(page: int = 1, period: str = "daily") -> list[Publication]:
        calls.append((page, period))
        if period == "daily":
            return []
        if page == 1:
            return [
                _pub(
                    "publication.pravo.gov.ru",
                    "постановление ветеран боевых действий выплата принят",
                    "http://publication.pravo.gov.ru/document/1",
                    published_at=NOW,
                )
            ]
        return []

    spec = SourceSpec(
        "publication.pravo.gov.ru",
        fetch_page,
        resolve_period=lambda window_start, now: "daily",
        period_fallback={"daily": "weekly"},
    )

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 1
    assert calls == [(1, "daily"), (1, "weekly"), (2, "weekly")]
    assert session.get(SourceState, "publication.pravo.gov.ru") is not None  # окно покрыто


def test_process_source_still_empty_after_period_fallback_marks_processed(
    session: Session, classifier: Classifier
) -> None:
    """Если и `daily`, и фолбэк `weekly` пусты на первой странице — публикаций
    действительно нет в окне, источник отмечается обработанным как обычно."""

    def fetch_page(page: int = 1, period: str = "daily") -> list[Publication]:
        return []

    spec = SourceSpec(
        "publication.pravo.gov.ru",
        fetch_page,
        resolve_period=lambda window_start, now: "daily",
        period_fallback={"daily": "weekly"},
    )

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 0
    assert session.get(SourceState, "publication.pravo.gov.ru") is not None


def test_process_source_recomputes_max_pages_on_period_fallback(
    session: Session, classifier: Classifier
) -> None:
    """docs/SPEC_pravo_gov_pagination_depth.md, ревью п.2: `max_pages` — теперь per-period
    (`max_pages_by_period`). Фолбэк `daily` -> `weekly` должен переключить не только
    `period`, но и допустимый потолок страниц — иначе маленький бюджет `daily`
    (актуальный для узкого дневного окна) обрывает уже идущий `weekly`-обход, для
    которого выделен больший бюджет, раньше настоящего конца листинга."""
    calls: list[tuple[int, str]] = []

    def fetch_page(page: int = 1, period: str = "daily") -> list[Publication]:
        calls.append((page, period))
        if period == "daily":
            return []  # daily пуст -> фолбэк на weekly
        if page <= 4:
            return [
                _pub(
                    "publication.pravo.gov.ru",
                    f"постановление №{page} ветеран боевых действий выплата принят",
                    f"http://publication.pravo.gov.ru/document/{page}",
                    published_at=NOW,
                )
            ]
        return []

    spec = SourceSpec(
        "publication.pravo.gov.ru",
        fetch_page,
        max_pages_by_period={"daily": 2, "weekly": 10},
        resolve_period=lambda window_start, now: "daily",
        period_fallback={"daily": "weekly"},
    )

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    # Если бы потолок не пересчитывался при фолбэке, обход остановился бы на странице 2
    # (бюджет daily) вместо естественного конца листинга на странице 5 (weekly).
    assert result.new_signals == 4
    assert calls == [(1, "daily"), (1, "weekly"), (2, "weekly"), (3, "weekly"), (4, "weekly"), (5, "weekly")]
    assert session.get(SourceState, "publication.pravo.gov.ru") is not None  # окно покрыто


def test_process_source_window_tolerance_allows_slightly_stale_items(
    session: Session, classifier: Classifier
) -> None:
    """docs/SPEC_pravo_gov_pagination_depth.md, п.1: допуск на неидеальную сортировку
    листинга — публикация чуть старше окна (в пределах `window_tolerance`) всё ещё
    обрабатывается, останов срабатывает только за пределами допуска."""
    from parser.state import mark_source_processed

    window_start = NOW - dt.timedelta(hours=24)
    mark_source_processed(session, "publication.pravo.gov.ru", success_at=window_start)
    session.commit()

    within_tolerance = _pub(
        "publication.pravo.gov.ru",
        "постановление ветеран боевых действий выплата принят",
        "http://publication.pravo.gov.ru/document/within",
        published_at=window_start - dt.timedelta(hours=12),  # старше окна, но в допуске (1 день)
    )
    beyond_tolerance = _pub(
        "publication.pravo.gov.ru",
        "постановление ветеран боевых действий выплата принят",
        "http://publication.pravo.gov.ru/document/beyond",
        published_at=window_start - dt.timedelta(days=1, hours=1),  # уже за пределами допуска
    )

    spec = SourceSpec(
        "publication.pravo.gov.ru",
        lambda page=1: [within_tolerance, beyond_tolerance] if page == 1 else [],
        window_tolerance=dt.timedelta(days=1),
    )

    result = process_source(session, classifier, spec, now=NOW)
    session.commit()

    assert result.new_signals == 1  # within_tolerance обработан, beyond_tolerance — нет
