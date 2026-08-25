"""Тесты загрузчика справочников, PLAN.md Фаза 2."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from db.catalog import (
    CatalogError,
    Source,
    access_for_domain,
    all_domains,
    load_classification_keywords,
    load_life_situations,
    load_regions,
    load_sources,
)
from db.enums import EventType, SignalCategory


def test_load_life_situations_default_covers_all_categories() -> None:
    situations = load_life_situations()

    assert {s.category for s in situations} == set(SignalCategory)
    assert all(s.keywords for s in situations)


def test_load_regions_default_covers_full_catalog() -> None:
    """Фаза 13 (docs/SPEC_region_expansion.md): справочник расширен до всех 89 регионов
    + сентинелы rf/moscow/undefined — не только MVP-периметр Москва+РФ."""
    regions = load_regions()
    codes = {r.code for r in regions}

    assert {"rf", "moscow", "undefined"} <= codes
    assert len(regions) >= 89

    moscow = next(r for r in regions if r.code == "moscow")
    assert moscow.sources
    assert all(isinstance(s, Source) for s in moscow.sources)


def test_load_sources_default_has_federal_group() -> None:
    sources = load_sources()

    assert "federal" in sources
    domains = {s.domain for s in sources["federal"]}
    assert domains == {
        "publication.pravo.gov.ru",
        "kremlin.ru",
        "government.ru",
        "sfr.gov.ru",
        "mintrud.gov.ru",
    }


def test_all_domains_merges_federal_other_and_regional() -> None:
    domains = all_domains()

    assert "sfr.gov.ru" in domains  # federal
    assert "garant.ru" in domains  # other
    assert "mos.ru" in domains  # regional (Москва)


def test_access_for_domain_returns_access_by_exact_or_subdomain_match() -> None:
    assert access_for_domain("sfr.gov.ru") == "direct"
    assert access_for_domain("www.sfr.gov.ru") == "direct"  # поддомен
    assert access_for_domain("kremlin.ru") == "ru_proxy"
    assert access_for_domain("docs.cntd.ru") == "unsupported"


def test_access_for_domain_returns_none_for_unknown_domain() -> None:
    assert access_for_domain("evil.example.com") is None


def test_new_life_situation_picked_up_without_code_change(tmp_path: Path) -> None:
    """Требование AGENTS.md раздел 1: новая ЖС — правка справочника, не кода.

    Ограничение: новый id должен уже существовать как значение SignalCategory (MVP,
    см. докстринг db/catalog.py) — тест демонстрирует happy path (id, который уже есть
    в enum, подхватывается из файла без каких-либо изменений в db/catalog.py), а не то,
    что можно завести совершенно новую категорию только правкой YAML.
    """
    custom_path = tmp_path / "life_situations.yaml"
    custom_path.write_text(
        textwrap.dedent(
            """
            - id: veterans
              name: Тестовое переопределение названия ЖС
              keywords:
                - тестовое ключевое слово
            """
        ),
        encoding="utf-8",
    )

    situations = load_life_situations(custom_path)

    assert len(situations) == 1
    assert situations[0].name == "Тестовое переопределение названия ЖС"
    assert situations[0].category == SignalCategory.VETERANS


def test_new_region_picked_up_without_code_change(tmp_path: Path) -> None:
    """Фаза 13 (docs/SPEC_region_expansion.md): в отличие от ЖС, регион — просто строка
    (`RegionEntry.code`, без enum), поэтому совершенно новый субъект РФ (не только новый
    источник для уже существующего code) тоже подхватывается из YAML без правки кода —
    именно это ограничение MVP снимает Фаза 13 (AGENTS.md раздел 16 п.12)."""
    custom_path = tmp_path / "regions.yaml"
    custom_path.write_text(
        textwrap.dedent(
            """
            - code: novy-region
              name: Новый регион
              sources:
                - domain: newly-added-source.example.ru
                  url: https://newly-added-source.example.ru/docs/
                  access: direct
            """
        ),
        encoding="utf-8",
    )

    regions = load_regions(custom_path)

    assert len(regions) == 1
    assert regions[0].code == "novy-region"
    assert [s.domain for s in regions[0].sources] == ["newly-added-source.example.ru"]


def test_unknown_life_situation_id_raises_catalog_error(tmp_path: Path) -> None:
    custom_path = tmp_path / "life_situations.yaml"
    custom_path.write_text(
        textwrap.dedent(
            """
            - id: unknown_category
              name: Несуществующая ЖС
              keywords: [что-то]
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="unknown_category"):
        load_life_situations(custom_path)


def test_duplicate_region_code_raises_catalog_error(tmp_path: Path) -> None:
    """Фаза 13: `load_regions()` больше не сверяет code с enum'ом (см.
    test_new_region_picked_up_without_code_change), но по-прежнему требует уникальности
    code — иначе `find_region_matches`/сигналы могли бы молча схлопнуть два разных
    региона в один."""
    custom_path = tmp_path / "regions.yaml"
    custom_path.write_text(
        textwrap.dedent(
            """
            - code: dup
              name: Первый
              sources: []
            - code: dup
              name: Второй
              sources: []
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="dup"):
        load_regions(custom_path)


def test_missing_catalog_file_raises_catalog_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="не найден"):
        load_life_situations(tmp_path / "does_not_exist.yaml")


def test_load_classification_keywords_default_covers_expected_groups() -> None:
    keywords = load_classification_keywords()

    assert "выплата" in keywords.topic_block
    assert "постановление" in keywords.document_markers
    assert "новый" in keywords.priority_high_words
    assert set(keywords.event_type_markers) == {
        EventType.REPEAL,
        EventType.ENTRY_INTO_FORCE,
        EventType.AMENDMENT,
        EventType.NEW_DOCUMENT,
    }
    assert "утратил силу" in keywords.event_type_markers[EventType.REPEAL]


def test_unknown_event_type_marker_key_raises_catalog_error(tmp_path: Path) -> None:
    custom_path = tmp_path / "keywords.yaml"
    custom_path.write_text(
        textwrap.dedent(
            """
            event_type_markers:
              unknown_event: [что-то]
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="unknown_event"):
        load_classification_keywords(custom_path)
