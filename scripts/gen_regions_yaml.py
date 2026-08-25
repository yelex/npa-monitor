"""Генератор data/regions.yaml из уникальных значений region базы мер, Фаза 13
(docs/SPEC_region_expansion.md, раздел «Решение» п.2).

rf/moscow/undefined — сентинел-записи, закреплены руками (см. докстринг
data/regions.yaml): скрипт их не создаёт и не перезаписывает, регенерирует только секцию
ниже маркера `# --- Сгенерировано scripts/gen_regions_yaml.py`. Запускать при обновлении
data/benefits_knowledge_base.json — новые/переименованные регионы подхватятся, коллизии
слагов ловит assert (спека: на текущих 89 их нет, но будущие обновления базы должны
ловиться, а не молча схлопывать два разных региона в один code).

Запуск: .venv/bin/python scripts/gen_regions_yaml.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
KB_PATH = ROOT / "data" / "benefits_knowledge_base.json"
REGIONS_PATH = ROOT / "data" / "regions.yaml"

GENERATED_MARKER = "# --- Сгенерировано scripts/gen_regions_yaml.py"
HAND_AUTHORED_NAMES = {"РФ", "Москва"}  # region-значения базы, покрытые rf/moscow вручную

# Практическая транслитерация (не ГОСТ) — только для получения читаемого kebab-case code,
# однозначность не требуется (уникальность проверяется assert'ом ниже).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def slugify(name: str) -> str:
    """«Волгоградская область» -> volgogradskaya-oblast (спека п.2). Скобки/тире —
    разделители слов, не часть code; Ё/Й/Х/Щ — по таблице транслитерации выше."""
    translit = "".join(_TRANSLIT.get(ch, ch) for ch in name.lower())
    return "-".join(_WORD_RE.findall(translit))


def load_kb_region_names() -> set[str]:
    raw = json.loads(KB_PATH.read_text(encoding="utf-8"))
    return {row["region"] for row in raw if row.get("region")}


def build_generated_entries() -> list[dict]:
    names = sorted(load_kb_region_names() - HAND_AUTHORED_NAMES)

    entries = []
    seen_codes: set[str] = set()
    for name in names:
        code = slugify(name)
        assert code, f"пустой code для региона {name!r}"
        assert code not in seen_codes, f"коллизия code={code!r} для региона {name!r}"
        seen_codes.add(code)
        entries.append({"code": code, "name": name, "sources": []})
    return entries


def main() -> None:
    full_text = REGIONS_PATH.read_text(encoding="utf-8")
    marker_pos = full_text.find(GENERATED_MARKER)
    if marker_pos == -1:
        raise SystemExit(
            f"{REGIONS_PATH}: не найден маркер {GENERATED_MARKER!r} — "
            "hand-authored секция (rf/moscow/undefined) должна оканчиваться этим маркером"
        )
    marker_end = full_text.index("\n", marker_pos) + 1
    hand_authored_section = full_text[:marker_end]

    entries = build_generated_entries()
    generated_yaml = yaml.safe_dump(
        entries, allow_unicode=True, default_flow_style=False, sort_keys=False
    )

    REGIONS_PATH.write_text(hand_authored_section + "\n" + generated_yaml, encoding="utf-8")
    print(f"data/regions.yaml: сгенерировано {len(entries)} регионов (+ rf/moscow/undefined)")


if __name__ == "__main__":
    main()
