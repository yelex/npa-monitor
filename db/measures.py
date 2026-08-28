"""Саджест меры из базы, docs/SPEC_signal_type_measure_select.md.

Пул кандидатов — снапшот `data/benefits_knowledge_base.json` (инвариант: перезаливается
одновременно с копией в npa-somas, см. спеку). Фильтр по ЖС — `source_dataset`
(значения уточнены по факту, раздел «Решение» п.2): `military`/`sber_svo` -> СВО,
`vbd`/`sber_vbd` -> ВБД, `invalid`/`sber_invalid` -> инвалиды. `tags` — вторичный
сигнал (fallback для записей с нестандартным/пустым `source_dataset`), у реальных
данных избыточен (проверено: `source_dataset` уже однозначно определяет ЖС), но
включён для устойчивости к будущим расширениям базы (раздел "НЕ входит" — единый
сервис базы, где `source_dataset` может не быть настолько чистым).

Скор — гибрид 0.6*косинус (Counter-BoW, лемматизация pymorphy3) + 0.4*подстрока в
`measure_name`/`human_readable_name`, без numpy/sklearn (спека, раздел «Решение» п.2).
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from db.catalog import load_regions
from db.enums import REGION_UNDEFINED, SignalCategory

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

# СВО-датасеты помечены и как "military" (осн. федеральный набор), и как "sber_svo"
# (доп. набор от Сбербанка) — оба относятся к ЖС "svo". Аналогично для ВБД/инвалидов.
_CATEGORY_SOURCE_DATASETS: dict[SignalCategory, frozenset[str]] = {
    SignalCategory.SVO: frozenset({"military", "sber_svo"}),
    SignalCategory.VETERANS: frozenset({"vbd", "sber_vbd"}),
    SignalCategory.DISABLED: frozenset({"invalid", "sber_invalid"}),
}

# Fallback по tags — на случай записей с source_dataset вне известных значений
# (устойчивость к расширению базы, см. docstring модуля).
_CATEGORY_TAGS: dict[SignalCategory, frozenset[str]] = {
    SignalCategory.SVO: frozenset(
        {"военный", "мобилизованный", "контрактник", "доброволец", "дети_военных"}
    ),
    SignalCategory.VETERANS: frozenset(
        {"ветеран_бд", "военнослужащие", "обслуживающие_воинские_части", "работники_в_зоне_боевых_действий"}
    ),
    SignalCategory.DISABLED: frozenset(
        {
            "инвалид", "инвалид_1_группы", "инвалид_2_группы", "инвалид_3_группы",
            "ребенок_инвалид", "военная_травма", "общее_заболевание", "радиация",
        }
    ),
}



@dataclass(frozen=True)
class MeasureRecord:
    measure_id: str
    measure_name: str
    human_readable_name: str
    row_hash: str | None
    region: str | None
    source_dataset: str | None
    tags: tuple[str, ...]
    name_haystack: str  # measure_name + human_readable_name, casefold — для подстроки
    corpus_bow: Counter  # BoW по search_corpus_lemmatized — для косинуса


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@lru_cache(maxsize=1)
def _morph():
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=4096)
def _lemma(word: str) -> str:
    return _morph().parse(word)[0].normal_form


def lemmatized_bow(text: str) -> Counter:
    """BoW по леммам pymorphy3 — для запроса аналитика (короткий текст, лемматизация
    по слову с кэшем `_lemma`). Корпус базы уже лемматизирован заранее
    (`search_corpus_lemmatized`) — для него используется `_tokenize` без повторной
    лемматизации (см. `_load_records`), чтобы не гонять морфоанализатор по 2297
    записям на каждый запрос."""
    return Counter(_lemma(w) for w in _tokenize(text))


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = a.keys() & b.keys()
    dot = sum(a[t] * b[t] for t in common)
    if dot == 0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    return dot / (norm_a * norm_b)


@lru_cache(maxsize=4)
def _load_records(path: str) -> tuple[MeasureRecord, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    records = []
    for row in raw:
        measure_id = row.get("measure_id")
        if not measure_id:
            continue  # 799 записей без measure_id исключены (спека, раздел «Решение» п.2)
        name = row.get("measure_name") or ""
        human = row.get("human_readable_name") or ""
        records.append(
            MeasureRecord(
                measure_id=measure_id,
                measure_name=name,
                human_readable_name=human,
                row_hash=row.get("row_hash"),
                region=row.get("region"),
                source_dataset=row.get("source_dataset"),
                tags=tuple(row.get("tags") or ()),
                name_haystack=f"{name.casefold()} {human.casefold()}",
                corpus_bow=Counter(_tokenize(row.get("search_corpus_lemmatized") or "")),
            )
        )
    return tuple(records)


def load_records(path: str | Path) -> tuple[MeasureRecord, ...]:
    return _load_records(str(path))


# docs/SPEC_result_edit.md §3.3: полей, которые нельзя перезаписать через overlay
# аналитика — идентификатор записи и вычисляемый при экспорте хэш содержимого.
_KB_NON_OVERRIDABLE_FIELDS = frozenset({"measure_id", "row_hash"})


@lru_cache(maxsize=4)
def _load_raw_rows(path: str) -> tuple[dict, ...]:
    return tuple(json.loads(Path(path).read_text(encoding="utf-8")))


def kb_field_names(path: str | Path) -> frozenset[str]:
    """Whitelist полей KB для overlay (§3.3, ревью №7) — множество ключей, реально
    встречающихся в снапшоте, не хардкод-список: формат карточки меры со временем
    расширяется новыми полями (AGENTS.md раздел 8), схема должна расти вместе с ним
    без правки кода."""
    fields: set[str] = set()
    for row in _load_raw_rows(str(path)):
        fields.update(row.keys())
    return frozenset(fields) - _KB_NON_OVERRIDABLE_FIELDS


def load_raw_record(measure_id: str, *, path: str | Path) -> dict | None:
    """Сырая запись KB (все поля, не только проекция `MeasureRecord`) — нужна
    write-back'у (`db/overrides.py`) для чтения текущего значения поля."""
    for row in _load_raw_rows(str(path)):
        if row.get("measure_id") == measure_id:
            return row
    return None


def invalidate_kb_cache() -> None:
    """Сбрасывает `lru_cache` сырых KB-строк (`_load_raw_rows`, за ним —
    `load_raw_record`/`kb_field_names`) — вызывать после перезаписи KB-файла на
    диске (`scripts/export_kb.py::export_kb`), иначе `apply_selection` в том же
    процессе продолжит читать замороженный снапшот и может дать ложноотрицательный
    STALE (docs/SPEC_fix_review_75af72b.md, замечание №2)."""
    _load_raw_rows.cache_clear()


def _matches_category(record: MeasureRecord, category: SignalCategory) -> bool:
    datasets = _CATEGORY_SOURCE_DATASETS.get(category, frozenset())
    if record.source_dataset in datasets:
        return True
    tags = _CATEGORY_TAGS.get(category, frozenset())
    return bool(tags & set(record.tags))


def _region_name(region: str) -> str | None:
    """Имя региона по коду сигнала через `catalog.load_regions()` (Фаза 13) — единый
    источник правды взамен захардкоженного словаря. `undefined` — спецкейс «без
    фильтра» (в базе мер такого региона нет, спека раздел «НЕ входит»)."""
    if region == REGION_UNDEFINED:
        return None
    entry = next((r for r in load_regions() if r.code == region), None)
    return entry.name if entry else None


def _matches_region(record: MeasureRecord, wanted: str | None) -> bool:
    if wanted is None:  # undefined (или незнакомый код) -> все регионы базы видны
        return True
    return record.region == wanted


def build_pool(
    categories: list[SignalCategory], region: str, *, path: str | Path
) -> list[MeasureRecord]:
    """Пул кандидатов: объединение по всем подтверждённым ЖС сигнала (сигнал может
    подтверждать несколько ЖС сразу, `category_toggle_kb`), пересечённое с регионом.
    Имя региона резолвится один раз на вызов (не на запись — 2297 записей в базе)."""
    records = load_records(path)
    cats = set(categories)
    wanted = _region_name(region)
    return [
        r for r in records
        if any(_matches_category(r, c) for c in cats) and _matches_region(r, wanted)
    ]


def score(query: str, record: MeasureRecord) -> float:
    query_bow = lemmatized_bow(query)
    cos = cosine(query_bow, record.corpus_bow)
    query_cf = query.casefold().strip()
    substring = 1.0 if query_cf and query_cf in record.name_haystack else 0.0
    return 0.6 * cos + 0.4 * substring


def rank(query: str, pool: list[MeasureRecord]) -> list[tuple[MeasureRecord, float]]:
    """Убывающий скор, при равенстве — алфавитный порядок названия (детерминированная
    пагинация "Ещё 8" — без этого порядок между запросами мог бы плавать)."""
    scored = [(r, score(query, r)) for r in pool]
    scored.sort(key=lambda pair: (-pair[1], pair[0].measure_name))
    return scored
