"""Загрузчик справочников (ЖС, источники, регионы) из data/, AGENTS.md раздел 1, PLAN.md
Фаза 2.

Справочники — YAML-файлы в data/, не таблицы БД: расширение (новая ЖС/новый регион/
источник) делается правкой файла, без изменения кода парсера/классификатора (AGENTS.md
раздел 1). db.enums.SignalCategory и Region в MVP остаются фиксированными Python-
перечислениями (см. AGENTS.md раздел 16) — это осознанное ограничение MVP: набор *видов*
ЖС/регионов пока правится вместе с кодом, а вот их ключевые слова и источники — уже нет.
Загрузчик явно проверяет соответствие id/code из справочников значениям этих enum'ов и
падает с понятной ошибкой при рассинхронизации, а не молча игнорирует лишнее.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from db.enums import Region, SignalCategory

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class CatalogError(ValueError):
    """Справочник не проходит валидацию (не совпадает с enum'ами, отсутствует файл и т.п.)."""


@dataclasses.dataclass(frozen=True)
class Source:
    domain: str
    access: str  # "direct" | "ru_proxy" | "unsupported" — см. data/sources.yaml
    url: str | None = None
    note: str | None = None


@dataclasses.dataclass(frozen=True)
class LifeSituation:
    id: str
    name: str
    keywords: tuple[str, ...]
    category: SignalCategory


@dataclasses.dataclass(frozen=True)
class RegionEntry:
    code: str
    name: str
    region: Region
    sources: tuple[Source, ...]


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        raise CatalogError(f"справочник не найден: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_life_situations(path: Path | None = None) -> tuple[LifeSituation, ...]:
    raw = _load_yaml(path or DATA_DIR / "life_situations.yaml") or []
    result: list[LifeSituation] = []
    for item in raw:
        try:
            category = SignalCategory(item["id"])
        except ValueError as exc:
            raise CatalogError(
                f"life_situations.yaml: id={item['id']!r} не соответствует "
                f"db.enums.SignalCategory — добавь значение в enum или исправь id"
            ) from exc
        result.append(
            LifeSituation(
                id=item["id"],
                name=item["name"],
                keywords=tuple(item["keywords"]),
                category=category,
            )
        )
    return tuple(result)


def load_sources(path: Path | None = None) -> dict[str, tuple[Source, ...]]:
    raw = _load_yaml(path or DATA_DIR / "sources.yaml") or {}
    return {group: tuple(Source(**entry) for entry in entries) for group, entries in raw.items()}


def load_regions(path: Path | None = None) -> tuple[RegionEntry, ...]:
    raw = _load_yaml(path or DATA_DIR / "regions.yaml") or []
    result: list[RegionEntry] = []
    for item in raw:
        try:
            region = Region(item["code"])
        except ValueError as exc:
            raise CatalogError(
                f"regions.yaml: code={item['code']!r} не соответствует db.enums.Region — "
                f"добавь значение в enum или исправь code"
            ) from exc
        sources = tuple(Source(**s) for s in item.get("sources", []))
        result.append(RegionEntry(code=item["code"], name=item["name"], region=region, sources=sources))
    return tuple(result)


def all_domains(
    sources: dict[str, tuple[Source, ...]] | None = None,
    regions: tuple[RegionEntry, ...] | None = None,
) -> set[str]:
    """Полный белый список доменов (федеральные + иные + региональные), AGENTS.md раздел 13.

    Используется для проверки допустимости ссылок (публикаций и НПА), которые бот
    принимает от эксперта или обходит парсер. Включает домены с любым access, в т.ч.
    "unsupported" — они документированы как известные источники, даже если парсер их
    пока не обходит автоматически; какие домены можно обходить в parser/fetcher.py
    (direct/ru_proxy) — решает Фаза 3, не этот справочник.
    """
    sources = sources if sources is not None else load_sources()
    regions = regions if regions is not None else load_regions()

    domains = {source.domain for group in sources.values() for source in group}
    domains |= {source.domain for region in regions for source in region.sources}
    return domains
