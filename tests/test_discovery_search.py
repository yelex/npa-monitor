"""Тесты parser/discovery_search.py.

`search_yandex` — на реальном формате ответа Yandex Cloud Search API v2 (base64+XML),
фрагмент структуры снят вживую 2026-08-20 при проверке спеки (см.
docs/SPEC_yandex_search_discovery.md, раздел 4а) — HTTP замокан (`httpx.MockTransport`),
без реальной сети. `run_discovery_search` — сквозной прогон (поиск замокан на уровне
`search_yandex`, БД и классификатор настоящие), по тому же паттерну, что и
tests/test_orchestrator.py.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
from xml.sax.saxutils import escape

import httpx
import pytest
from sqlalchemy.orm import Session

from db.catalog import LifeSituation
from db.enums import SignalCategory
from db.models import DocumentSeen, Signal, SourceState
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import Classifier
from parser.discovery_search import (
    MAX_QUERY_TEXT_LENGTH,
    DiscoverySearchResult,
    YandexSearchUnavailable,
    _bounded_keywords_query,
    _canonical_url,
    _fetch_full_title,
    build_query_text,
    run_daily_discovery,
    run_discovery_search,
    search_yandex,
)
from parser.fetcher import FetchResult, SourceUnavailable
from parser.models import Publication


def _yandex_xml_response(docs: list[tuple[str, str, str]]) -> bytes:
    """docs: [(url, title, passage_text), ...] — минимальный реальный каркас ответа."""
    doc_xml = "".join(
        f"""
        <doc>
          <url>{escape(url)}</url>
          <title>{escape(title)}</title>
          <passages><passage>{escape(passage)}</passage></passages>
        </doc>
        """
        for url, title, passage in docs
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <yandexsearch version="1.0">
      <response date="20260820T110441">
        <results>
          <grouping>
            <group>{doc_xml}</group>
          </grouping>
        </results>
      </response>
    </yandexsearch>""".encode()


def _yandex_error_response() -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <yandexsearch version="1.0">
      <response date="20260820T110544">
        <error code="15">{escape("Искомое не найдено")}</error>
      </response>
    </yandexsearch>""".encode()


def _mock_search_transport(
    monkeypatch: pytest.MonkeyPatch, xml_bytes: bytes, *, capture: list | None = None
):
    real_client_cls = httpx.Client
    raw_data = base64.b64encode(xml_bytes).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(200, json={"rawData": raw_data})

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler))
    )


def test_build_query_text_includes_site_filter_and_date_operator() -> None:
    query_text = build_query_text(
        "постановление выплаты",
        ["docs.cntd.ru", "mos.ru"],
        dt.date(2025, 12, 1),
        dt.date(2025, 12, 31),
    )

    assert "(site:docs.cntd.ru | site:mos.ru)" in query_text
    assert "постановление выплаты" in query_text
    assert "date:20251201..20251231" in query_text


def test_search_yandex_parses_docs_and_strips_hlword_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    xml_bytes = _yandex_xml_response(
        [
            (
                "https://docs.cntd.ru/document/1314770295",
                "Об установлении размеров <hlword>социальных</hlword> выплат",
                "Постановление <hlword>Правительства</hlword> Москвы от 09.12.2025",
            )
        ]
    )
    _mock_search_transport(monkeypatch, xml_bytes)

    publications = search_yandex("тест", api_key="key", folder_id="folder")

    assert len(publications) == 1
    pub = publications[0]
    assert pub.url == "https://docs.cntd.ru/document/1314770295"
    assert pub.title == "Об установлении размеров социальных выплат"
    assert pub.summary == "Постановление Правительства Москвы от 09.12.2025"
    assert pub.published_at is None
    assert pub.source_key == "yandex_search"


def test_search_yandex_returns_empty_list_on_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_search_transport(monkeypatch, _yandex_error_response())

    publications = search_yandex("тест date:20990101..20990110", api_key="key", folder_id="folder")

    assert publications == []


def test_search_yandex_sends_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """`groupSpec.groupMode=GROUP_MODE_FLAT` (не группировать по домену) и `query.page`
    (не top-level `page`) — SPEC раздел 4б: с дефолтной группировкой по домену и без
    `groupsOnPage` выдача обрезалась до 10 документов независимо от запрошенного
    количества; `resultsCount`/top-level `page` из первой версии молча игнорировались
    сервером — этого поля нет в реальной схеме (`search_service.proto`)."""
    captured: list[httpx.Request] = []
    _mock_search_transport(monkeypatch, _yandex_xml_response([]), capture=captured)

    search_yandex("постановление", api_key="secret-key", folder_id="folder-id", results_count=10, page=2)

    assert len(captured) == 1
    request = captured[0]
    assert request.headers["Authorization"] == "Api-Key secret-key"

    payload = json.loads(request.content)
    assert payload["query"]["queryText"] == "постановление"
    assert payload["query"]["page"] == 2
    assert payload["folderId"] == "folder-id"
    assert payload["groupSpec"] == {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": 10}
    assert "resultsCount" not in payload


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


@pytest.fixture(autouse=True)
def _stub_full_title_fetch(monkeypatch: pytest.MonkeyPatch):
    """По умолчанию не ходим в сеть за полным заголовком страницы (`_fetch_full_title`)
    — тесты не должны зависеть от реальных сайтов. Кто хочет проверить именно эту
    логику — переопределяет мок явно (см. тесты `_fetch_full_title`/`run_discovery_
    search` ниже)."""
    monkeypatch.setattr("parser.discovery_search._fetch_full_title", lambda url: None)


def _pub(url: str, title: str, summary: str | None = None) -> Publication:
    return Publication(source_key="yandex_search", title=title, url=url, published_at=None, summary=summary)


def test_run_discovery_search_creates_signal_for_relevant_publication(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    publications = [
        _pub(
            "https://docs.cntd.ru/document/1314770295",
            "постановление ветеран боевых действий выплата новый",
        )
    ]
    monkeypatch.setattr(
        "parser.discovery_search.search_yandex", lambda *a, **kw: publications
    )

    result = run_discovery_search(
        session,
        classifier,
        query="выплата",
        date_from=dt.date(2025, 12, 1),
        date_to=dt.date(2025, 12, 31),
        api_key="key",
        folder_id="folder",
        domains={"docs.cntd.ru"},
    )
    session.commit()

    assert result == DiscoverySearchResult(found=1, duplicates=0, irrelevant=0, new_signals=1)
    assert session.query(Signal).count() == 1
    assert session.query(Signal).one().source_url == "https://docs.cntd.ru/document/1314770295"


def test_run_discovery_search_skips_irrelevant_publication(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    publications = [
        _pub("https://docs.cntd.ru/document/2", "постановление о субсидиях сельхозпроизводителям")
    ]
    monkeypatch.setattr("parser.discovery_search.search_yandex", lambda *a, **kw: publications)

    result = run_discovery_search(
        session,
        classifier,
        query="субсидии",
        date_from=dt.date(2025, 12, 1),
        date_to=dt.date(2025, 12, 31),
        api_key="key",
        folder_id="folder",
        domains={"docs.cntd.ru"},
    )
    session.commit()

    assert result.new_signals == 0
    assert result.irrelevant == 1
    assert session.query(Signal).count() == 0


def test_run_discovery_search_skips_non_whitelisted_domain(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    publications = [
        _pub("https://not-in-whitelist.example/doc", "постановление ветеран боевых действий выплата")
    ]
    monkeypatch.setattr("parser.discovery_search.search_yandex", lambda *a, **kw: publications)

    result = run_discovery_search(
        session,
        classifier,
        query="выплата",
        date_from=dt.date(2025, 12, 1),
        date_to=dt.date(2025, 12, 31),
        api_key="key",
        folder_id="folder",
        domains={"docs.cntd.ru"},
    )
    session.commit()

    assert result.found == 1
    assert result.new_signals == 0
    assert session.query(Signal).count() == 0


def test_run_discovery_search_dedups_against_existing_document_seen(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    publications = [
        _pub("https://docs.cntd.ru/document/1314770295", "постановление ветеран боевых действий выплата")
    ]
    monkeypatch.setattr("parser.discovery_search.search_yandex", lambda *a, **kw: publications)

    kwargs = dict(
        query="выплата",
        date_from=dt.date(2025, 12, 1),
        date_to=dt.date(2025, 12, 31),
        api_key="key",
        folder_id="folder",
        domains={"docs.cntd.ru"},
    )

    first = run_discovery_search(session, classifier, **kwargs)
    session.commit()
    second = run_discovery_search(session, classifier, **kwargs)
    session.commit()

    assert first.new_signals == 1
    assert second.new_signals == 0
    assert second.duplicates == 1
    assert session.query(Signal).count() == 1
    assert session.query(DocumentSeen).count() == 1


def test_run_discovery_search_dry_run_does_not_write_to_db(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    publications = [
        _pub("https://docs.cntd.ru/document/1314770295", "постановление ветеран боевых действий выплата")
    ]
    monkeypatch.setattr("parser.discovery_search.search_yandex", lambda *a, **kw: publications)

    result = run_discovery_search(
        session,
        classifier,
        query="выплата",
        date_from=dt.date(2025, 12, 1),
        date_to=dt.date(2025, 12, 31),
        api_key="key",
        folder_id="folder",
        domains={"docs.cntd.ru"},
        dry_run=True,
    )
    session.commit()

    assert result.new_signals == 1
    assert session.query(Signal).count() == 0
    assert session.query(DocumentSeen).count() == 0


def test_run_discovery_search_paginates_until_short_page(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    """`max_pages` — верхняя граница, но останавливается раньше, если страница пришла
    короче `results_count` (SPEC раздел 4б: пагинация через `query.page`, не бесконечная
    — каждая страница это отдельный платный вызов)."""
    title = "постановление ветеран боевых действий выплата"
    pages = {
        0: [_pub(f"https://docs.cntd.ru/document/{i}", title) for i in range(2)],
        1: [_pub("https://docs.cntd.ru/document/last", title)],
    }
    seen_pages: list[int] = []

    def fake_search_yandex(query_text, *, api_key, folder_id, results_count, page=0, source_key=None):
        seen_pages.append(page)
        return pages.get(page, [])

    monkeypatch.setattr("parser.discovery_search.search_yandex", fake_search_yandex)

    result = run_discovery_search(
        session,
        classifier,
        query="выплата",
        date_from=dt.date(2025, 12, 1),
        date_to=dt.date(2025, 12, 31),
        api_key="key",
        folder_id="folder",
        domains={"docs.cntd.ru"},
        results_count=2,
        max_pages=5,
    )
    session.commit()

    assert seen_pages == [0, 1]  # страница 1 короче results_count=2 -> остановка, страница 2 не запрошена
    assert result.found == 3
    assert result.new_signals == 3
    assert session.query(Signal).count() == 3


def test_bounded_keywords_query_returns_full_text_when_it_fits() -> None:
    query = _bounded_keywords_query(["инвалид", "льгота"], reserved_length=0)

    assert query == "инвалид льгота"


def test_bounded_keywords_query_trims_when_over_budget() -> None:
    keywords = ["ветеран боевых действий", "инвалид", "участник СВО"]
    reserved_length = MAX_QUERY_TEXT_LENGTH - len(keywords[0]) - 1  # влезает только первое слово

    query = _bounded_keywords_query(keywords, reserved_length=reserved_length)

    assert query == keywords[0]


_VETERANS = LifeSituation(
    id="veterans", name="ВБД", keywords=("ветеран боевых действий",), category=SignalCategory.VETERANS
)
_DISABLED = LifeSituation(
    id="disabled", name="Инвалиды", keywords=("инвалид",), category=SignalCategory.DISABLED
)


def test_run_daily_discovery_creates_signal_with_per_situation_source_key(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    def fake_search_yandex(query_text, *, source_key, **kw):
        return [
            Publication(
                source_key=source_key,
                title="ветеран боевых действий выплата льгота новый",
                url="https://docs.cntd.ru/document/1",
                published_at=None,
            )
        ]

    monkeypatch.setattr("parser.discovery_search.search_yandex", fake_search_yandex)

    results = run_daily_discovery(
        session,
        classifier,
        api_key="key",
        folder_id="folder",
        life_situations=[_VETERANS],
        domains={"docs.cntd.ru"},
        now=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
    )

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].result.new_signals == 1
    assert session.query(Signal).count() == 1
    document = session.query(DocumentSeen).one()
    assert document.source_key == "yandex_search:veterans"
    state = session.get(SourceState, "yandex_search:veterans")
    assert state is not None and state.last_success_at is not None


def test_run_daily_discovery_uses_catchup_window_from_previous_run(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    captured_queries: list[str] = []

    def fake_search_yandex(query_text, *, source_key, **kw):
        captured_queries.append(query_text)
        return []

    monkeypatch.setattr("parser.discovery_search.search_yandex", fake_search_yandex)

    first_now = dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc)
    run_daily_discovery(
        session, classifier, api_key="key", folder_id="folder",
        life_situations=[_VETERANS], domains={"docs.cntd.ru"}, now=first_now,
    )

    second_now = dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc)
    run_daily_discovery(
        session, classifier, api_key="key", folder_id="folder",
        life_situations=[_VETERANS], domains={"docs.cntd.ru"}, now=second_now,
    )

    # Второй прогон ищет с даты последнего успешного (10.08), не заново с "7 дней назад
    # от второго прогона" (13.08) — иначе он бы потерял 10-13 августа (доверстывание).
    assert "date:20260810..20260820" in captured_queries[1]


def test_run_daily_discovery_isolates_failure_of_one_situation(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    def fake_search_yandex(query_text, *, source_key, **kw):
        if source_key == "yandex_search:veterans":
            raise YandexSearchUnavailable("сеть недоступна")
        return [_pub("https://docs.cntd.ru/document/2", "инвалид выплата льгота новый")]

    monkeypatch.setattr("parser.discovery_search.search_yandex", fake_search_yandex)

    results = run_daily_discovery(
        session,
        classifier,
        api_key="key",
        folder_id="folder",
        life_situations=[_VETERANS, _DISABLED],
        domains={"docs.cntd.ru"},
        now=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
    )

    by_key = {r.source_key: r for r in results}
    assert by_key["yandex_search:veterans"].ok is False
    assert by_key["yandex_search:disabled"].ok is True
    assert session.get(SourceState, "yandex_search:veterans") is None  # сбой -> окно не сдвинулось
    assert session.get(SourceState, "yandex_search:disabled") is not None
    assert session.query(Signal).count() == 1


# --- Канонизация URL (дедуп постраничных вариантов одного документа) ---


def test_canonical_url_strips_page_param_keeps_other_query_params() -> None:
    url = "https://minjust.consultant.ru/special/documents/document/60711?items=1&page=8"

    canonical = _canonical_url(url)

    assert canonical == "https://minjust.consultant.ru/special/documents/document/60711?items=1"


def test_canonical_url_different_page_values_collapse_to_same_url() -> None:
    urls = [
        "https://minjust.consultant.ru/special/documents/document/60711?items=1&page=2",
        "https://minjust.consultant.ru/special/documents/document/60711?items=1&page=8",
        "https://minjust.consultant.ru/special/documents/document/60711?items=1&page=12",
    ]

    assert len({_canonical_url(u) for u in urls}) == 1


def test_search_yandex_returns_canonicalized_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регрессия, найденная вживую 2026-08-20 (жалоба пользователя): один и тот же
    документ на `minjust.consultant.ru` индексируется Яндексом отдельным URL на каждую
    страницу многостраничного просмотрщика — без канонизации это создавало по сигналу
    на каждую страницу одного и того же приказа."""
    xml_bytes = _yandex_xml_response(
        [
            (
                "https://minjust.consultant.ru/special/documents/document/60711?items=1&page=2",
                "ПРИКАЗ Минтруда РФ",
                "текст",
            )
        ]
    )
    _mock_search_transport(monkeypatch, xml_bytes)

    publications = search_yandex("тест", api_key="key", folder_id="folder")

    assert publications[0].url == "https://minjust.consultant.ru/special/documents/document/60711?items=1"


def test_run_discovery_search_dedups_pagination_variants_of_same_document(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    xml_bytes = _yandex_xml_response(
        [
            (
                "https://minjust.consultant.ru/special/documents/document/60711?items=1&page=2",
                "ПРИКАЗ Минтруда РФ ветеран боевых действий выплата",
                "текст",
            ),
            (
                "https://minjust.consultant.ru/special/documents/document/60711?items=1&page=8",
                "ПРИКАЗ Минтруда РФ ветеран боевых действий выплата",
                "текст",
            ),
        ]
    )
    _mock_search_transport(monkeypatch, xml_bytes)

    result = run_discovery_search(
        session,
        classifier,
        query="выплата",
        date_from=dt.date(2025, 12, 1),
        date_to=dt.date(2025, 12, 31),
        api_key="key",
        folder_id="folder",
        domains={"minjust.consultant.ru"},
    )
    session.commit()

    assert result.found == 2
    assert result.duplicates == 1
    assert result.new_signals == 1
    assert session.query(Signal).count() == 1


# --- Полный заголовок страницы (вместо обрезанного сниппета Яндекса) ---


def test_fetch_full_title_returns_title_text(monkeypatch: pytest.MonkeyPatch) -> None:
    html = "<html><head><title>Полный заголовок документа без обрезания</title></head><body></body></html>"
    monkeypatch.setattr(
        "parser.discovery_search.fetch",
        lambda url, *, access: FetchResult(
            url=url, status_code=200, text=html, content_type="text/html; charset=utf-8"
        ),
    )

    title = _fetch_full_title("https://docs.cntd.ru/document/1")

    assert title == "Полный заголовок документа без обрезания"


def test_fetch_full_title_returns_none_for_non_html_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "parser.discovery_search.fetch",
        lambda url, *, access: FetchResult(
            url=url, status_code=200, text="%PDF-1.4...", content_type="application/pdf"
        ),
    )

    assert _fetch_full_title("https://sfr.gov.ru/doc.pdf") is None


def test_fetch_full_title_returns_none_when_source_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_unavailable(url, *, access):
        raise SourceUnavailable(url=url, access=access, last_error=Exception("boom"))

    monkeypatch.setattr("parser.discovery_search.fetch", raise_unavailable)

    assert _fetch_full_title("https://docs.cntd.ru/document/1") is None


def test_run_discovery_search_replaces_snippet_title_with_full_page_title(
    monkeypatch: pytest.MonkeyPatch, session: Session, classifier: Classifier
) -> None:
    publications = [
        _pub(
            "https://docs.cntd.ru/document/1314770295",
            "постановление ветеран боевых действий выплата новый...",
        )
    ]
    monkeypatch.setattr("parser.discovery_search.search_yandex", lambda *a, **kw: publications)
    monkeypatch.setattr(
        "parser.discovery_search._fetch_full_title",
        lambda url: "Полное название документа без многоточия в конце",
    )

    run_discovery_search(
        session,
        classifier,
        query="выплата",
        date_from=dt.date(2025, 12, 1),
        date_to=dt.date(2025, 12, 31),
        api_key="key",
        folder_id="folder",
        domains={"docs.cntd.ru"},
    )
    session.commit()

    signal = session.query(Signal).one()
    assert signal.title == "Полное название документа без многоточия в конце"
