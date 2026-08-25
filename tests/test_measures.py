"""docs/SPEC_signal_type_measure_select.md: пул кандидатов (фильтр ЖС+регион,
исключение measure_id=null) и гибридный скор (косинус pymorphy3 + подстрока)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from db.catalog import load_regions
from db.enums import REGION_MOSCOW, REGION_RF, REGION_UNDEFINED, SignalCategory
from db.measures import (
    _matches_region,
    _region_name,
    build_pool,
    cosine,
    lemmatized_bow,
    load_records,
    rank,
    score,
)

_KB_PATH = Path(__file__).resolve().parent.parent / "data" / "benefits_knowledge_base.json"

_ROWS = [
    {
        "measure_id": "00_svo_1",
        "measure_name": "Выплата при заключении контракта",
        "human_readable_name": "Выплата за контракт на военную службу",
        "region": "РФ",
        "source_dataset": "military",
        "tags": ["мобилизованный", "контрактник"],
        "row_hash": "hash-svo-1",
        "search_corpus_lemmatized": "выплата при заключение контракт военный служба",
    },
    {
        "measure_id": "77_svo_2",
        "measure_name": "Единовременная выплата контрактнику Москвы",
        "human_readable_name": "Доплата контрактнику от города",
        "region": "Москва",
        "source_dataset": "military",
        "tags": ["контрактник"],
        "row_hash": "hash-svo-2",
        "search_corpus_lemmatized": "единовременный выплата контрактник москва город доплата",
    },
    {
        # source_dataset vbd в реальной базе целиком без measure_id (см. SPEC) —
        # фикстура с непустым id имитирует гипотетическую будущую запись, чтобы
        # проверить фильтр по ЖС "veterans" отдельно от факта его текущей пустоты.
        "measure_id": "00_vbd_1",
        "measure_name": "Ежемесячная выплата ветерану боевых действий",
        "human_readable_name": "Выплата ветерану БД",
        "region": "РФ",
        "source_dataset": "vbd",
        "tags": ["ветеран_бд"],
        "row_hash": "hash-vbd-1",
        "search_corpus_lemmatized": "ежемесячный выплата ветеран боевой действие",
    },
    {
        "measure_id": "00_inv_1",
        "measure_name": "Компенсация инвалиду 1 группы",
        "human_readable_name": "Компенсация инвалидам",
        "region": "РФ",
        "source_dataset": "invalid",
        "tags": ["инвалид", "инвалид_1_группы"],
        "row_hash": "hash-inv-1",
        "search_corpus_lemmatized": "компенсация инвалид первый группа",
    },
    {
        # measure_id пустой -> должен быть исключён из пула независимо от остальных полей.
        "measure_id": None,
        "measure_name": "Мера без measure_id",
        "human_readable_name": "Мера без id",
        "region": "РФ",
        "source_dataset": "military",
        "tags": ["контрактник"],
        "row_hash": "hash-null",
        "search_corpus_lemmatized": "мера без measure id",
    },
]


@pytest.fixture
def kb_path(tmp_path) -> str:
    path = tmp_path / "kb.json"
    path.write_text(json.dumps(_ROWS, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_load_records_excludes_null_measure_id(kb_path) -> None:
    records = load_records(kb_path)
    assert len(records) == 4
    assert all(r.measure_id is not None for r in records)


def test_build_pool_filters_by_category_source_dataset(kb_path) -> None:
    svo_pool = build_pool([SignalCategory.SVO], REGION_UNDEFINED, path=kb_path)
    assert {r.measure_id for r in svo_pool} == {"00_svo_1", "77_svo_2"}

    veterans_pool = build_pool([SignalCategory.VETERANS], REGION_UNDEFINED, path=kb_path)
    assert {r.measure_id for r in veterans_pool} == {"00_vbd_1"}

    disabled_pool = build_pool([SignalCategory.DISABLED], REGION_UNDEFINED, path=kb_path)
    assert {r.measure_id for r in disabled_pool} == {"00_inv_1"}


def test_build_pool_unions_multiple_categories(kb_path) -> None:
    pool = build_pool([SignalCategory.SVO, SignalCategory.DISABLED], REGION_UNDEFINED, path=kb_path)
    assert {r.measure_id for r in pool} == {"00_svo_1", "77_svo_2", "00_inv_1"}


def test_build_pool_filters_by_region_moscow(kb_path) -> None:
    pool = build_pool([SignalCategory.SVO], REGION_MOSCOW, path=kb_path)
    assert {r.measure_id for r in pool} == {"77_svo_2"}


def test_build_pool_filters_by_region_rf(kb_path) -> None:
    pool = build_pool([SignalCategory.SVO], REGION_RF, path=kb_path)
    assert {r.measure_id for r in pool} == {"00_svo_1"}


def test_build_pool_undefined_region_returns_all_regions(kb_path) -> None:
    pool = build_pool([SignalCategory.SVO], REGION_UNDEFINED, path=kb_path)
    assert {r.measure_id for r in pool} == {"00_svo_1", "77_svo_2"}


def test_cosine_identical_bow_is_one() -> None:
    bow = lemmatized_bow("выплата контракт")
    assert cosine(bow, bow) == pytest.approx(1.0)


def test_cosine_disjoint_bow_is_zero() -> None:
    a = lemmatized_bow("выплата контракт")
    b = lemmatized_bow("совершенно другой текст")
    assert cosine(a, b) == 0.0


def test_cosine_empty_bow_is_zero() -> None:
    assert cosine(lemmatized_bow(""), lemmatized_bow("выплата")) == 0.0


def test_lemmatized_bow_normalizes_word_forms() -> None:
    # "контракта" (родительный падеж) -> лемма "контракт", как в фикстуре корпуса.
    bow = lemmatized_bow("выплата за контракта")
    assert bow["контракт"] == 1


def test_score_substring_boosts_exact_name_match(kb_path) -> None:
    pool = build_pool([SignalCategory.SVO], REGION_UNDEFINED, path=kb_path)
    record = next(r for r in pool if r.measure_id == "00_svo_1")
    with_substring = score("выплата при заключении контракта", record)
    without_substring = score("совершенно не совпадающий запрос", record)
    assert with_substring > without_substring
    assert with_substring > 0.4  # хотя бы вклад подстроки (0.4 * 1.0) присутствует


def test_rank_orders_by_descending_score(kb_path) -> None:
    pool = build_pool([SignalCategory.SVO], REGION_UNDEFINED, path=kb_path)
    ranked = rank("выплата контракт военная служба", pool)
    scores = [s for _r, s in ranked]
    assert scores == sorted(scores, reverse=True)
    # запись про федеральный контракт по военной службе должна обойти
    # московскую доплату — корпус ближе по смыслу к запросу.
    assert ranked[0][0].measure_id == "00_svo_1"


def test_rank_returns_empty_for_empty_pool(kb_path) -> None:
    pool = build_pool([SignalCategory.VETERANS], REGION_MOSCOW, path=kb_path)
    assert pool == []
    assert rank("любой запрос", pool) == []


def test_region_name_resolves_via_catalog() -> None:
    """Фаза 13: `_region_name` берёт имя региона через `catalog.load_regions()` по коду
    — не захардкоженный словарь (SPEC_region_expansion.md раздел «Решение» п.4)."""
    assert _region_name(REGION_MOSCOW) == "Москва"
    assert _region_name(REGION_RF) == "РФ"
    assert _region_name("volgogradskaya-oblast") == "Волгоградская область"


def test_region_name_undefined_is_none_special_case() -> None:
    """undefined — спецкейс «без фильтра», в базе мер такого региона нет (раздел
    «НЕ входит»), а не код для поиска по catalog."""
    assert _region_name(REGION_UNDEFINED) is None


def test_matches_region_wanted_none_accepts_any_record(kb_path) -> None:
    """`wanted=None` — веха вызывающего кода "без фильтра" (undefined или неизвестный
    код, см. `_region_name`), а не свойство конкретной записи — годится любая запись."""
    any_record = load_records(kb_path)[0]
    assert _matches_region(any_record, None) is True


def test_matches_region_filters_by_resolved_name(kb_path) -> None:
    record_moscow = next(r for r in load_records(kb_path) if r.measure_id == "77_svo_2")
    record_rf = next(r for r in load_records(kb_path) if r.measure_id == "00_svo_1")

    assert _matches_region(record_moscow, "Москва") is True
    assert _matches_region(record_rf, "Москва") is False


def test_all_kb_regions_have_regions_yaml_entry() -> None:
    """Guard-тест Фазы 13 (docs/SPEC_region_expansion.md, раздел «Решение» п.4): каждое
    уникальное значение region снапшота базы мер должно иметь запись в data/regions.yaml
    (по имени) — ловит рассинхрон, если база обновится новым регионом без обновления
    справочника (scripts/gen_regions_yaml.py не был перезапущен)."""
    raw = json.loads(_KB_PATH.read_text(encoding="utf-8"))
    kb_region_names = {row["region"] for row in raw if row.get("region")}
    yaml_region_names = {r.name for r in load_regions()}

    missing = kb_region_names - yaml_region_names
    assert not missing, f"регионы базы мер без записи в data/regions.yaml: {sorted(missing)}"
