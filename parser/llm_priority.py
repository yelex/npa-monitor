"""LLM-приоритизация как второй этап после keywords-анализа, PLAN.md Фаза 11,
`docs/SPEC_llm_priority.md`.

Скоуп (спека, раздел «Решения», п.1): LLM-проход только для сигналов с regex-
приоритетом MEDIUM/LOW — HIGH уже надёжен (двойной гейт `parser/classifier.py::
detect_priority`), пересмотру не подлежит. `classifier.py` этим модулем не
затрагивается вовсе — только читается результат его работы (`Signal.priority`,
`Signal.categories`, `Signal.event_type`, `Signal.region`).

Известное ограничение (спека, п.2): `Signal` не хранит `summary` публикации — только
`title` (см. `parser/signals.py::build_signal`, `docs/SPEC_retroactive_signals_cleanup.md`
раздел 5, тот же вывод про шаг D). Поле в БД появится только при следующей миграции
(спека, «Комбинирование») — до тех пор `_signal_summary` всегда возвращает `None`, и
батч по факту всегда работает в режиме «title + regex-trace», без summary. Код уже
готов подхватить summary, когда/если появится источник (см. TODO там же).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Iterable, Sequence

from sqlalchemy.orm import Session

from db.catalog import ClassificationKeywords, load_classification_keywords
from db.enums import EventType, Priority, Region, SignalCategory
from db.models import Signal
from parser.llm import ClassifierLLMClient, LLMError
from parser.ru_stem import find_matches

log = logging.getLogger(__name__)

# Спека, раздел «Решения» п.2: батч 15-25 сигналов; интеграция (PLAN.md Фаза 11 п.2)
# режет прогон на чанки по 20 — середина рекомендованного диапазона.
DEFAULT_CHUNK_SIZE = 20

_PRIORITY_ORDER: dict[Priority, int] = {Priority.LOW: 0, Priority.MEDIUM: 1, Priority.HIGH: 2}


def chunk_signal_ids(signal_ids: Sequence[int], *, size: int = DEFAULT_CHUNK_SIZE) -> list[list[int]]:
    """Режет список id сигналов на чанки для батч-вызова LLM (спека, п.2)."""
    return [list(signal_ids[i : i + size]) for i in range(0, len(signal_ids), size)]


@dataclasses.dataclass(frozen=True)
class SignalPriorityContext:
    """Вход для одного элемента батча — id + title + summary (если доступен) +
    regex-trace (категории, тип события, регион, слова приоритета), спека п.2."""

    signal_id: int
    title: str
    summary: str | None
    regex_priority: Priority
    categories: tuple[SignalCategory, ...]
    event_type: EventType
    region: Region
    priority_word_matches: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class PriorityRefinement:
    """Результат комбинирования regex- и LLM-приоритета для одного сигнала
    (спека, раздел «Решения» п.4)."""

    signal_id: int
    regex_priority: Priority
    llm_priority: Priority | None  # None — LLM недоступен/невалиден для этого элемента
    llm_reason: str | None
    final_priority: Priority
    source: str  # "regex" | "llm_adjusted" — источник final_priority, для структурлога
    discrepancy: bool = False  # LLM предложил скачок >1 уровня (low<->high) — не применён

    def format(self) -> str:
        llm_part = f"llm={self.llm_priority.value}" if self.llm_priority is not None else "llm=нет ответа"
        reason_part = f"; reason={self.llm_reason!r}" if self.llm_reason else ""
        discrepancy_part = " [РАСХОЖДЕНИЕ >1 уровня, не применено]" if self.discrepancy else ""
        return (
            f"[{self.signal_id}] regex={self.regex_priority.value} {llm_part} "
            f"-> final={self.final_priority.value} источник={self.source}{reason_part}{discrepancy_part}"
        )


def log_refinement(refinement: PriorityRefinement, *, applied: bool) -> None:
    """Структурированный лог решения по приоритету — аналог `ClassificationTrace.format()`
    (спека, раздел «Решения» п.4: «источник финального приоритета — в структурированном
    логе»). Не пишет в БД — записью занимается `apply_refinements`, вызывается отдельно."""
    if refinement.source == "regex" and not refinement.discrepancy:
        return  # приоритет не менялся и расхождений не было — не засорять лог
    mode = "" if applied else " (log-only, не применено)"
    log.info("%s%s", refinement.format(), mode)


def _signal_summary(signal: Signal) -> str | None:
    """`Signal` не хранит summary публикации (см. докстринг модуля) — сейчас всегда
    `None`, вызывающий код деградирует до title+trace (спека, п.2)."""
    return None


def _load_context(
    session: Session, signal_id: int, keywords: ClassificationKeywords
) -> SignalPriorityContext | None:
    signal = session.get(Signal, signal_id)
    if signal is None:
        log.warning("сигнал [%s] не найден при LLM-приоритизации — пропуск", signal_id)
        return None
    if signal.priority == Priority.HIGH:
        # Скоуп ограничен MEDIUM/LOW (спека, п.1) — HIGH сюда попасть не должен, но
        # если попал (вызывающий код ошибся) — не пересматриваем.
        return None

    title = signal.title or ""
    priority_word_matches = find_matches(title.lower(), keywords.priority_high_words)
    categories = tuple(link.category for link in signal.categories)

    return SignalPriorityContext(
        signal_id=signal.id,
        title=title,
        summary=_signal_summary(signal),
        regex_priority=signal.priority,
        categories=categories,
        event_type=signal.event_type,
        region=signal.region,
        priority_word_matches=priority_word_matches,
    )


_PROMPT_HEADER = (
    "Ты — бизнес-аналитик, уточняющий приоритет сигналов о публикациях НПА для эксперта, "
    "который отслеживает меры поддержки ветеранов боевых действий, людей с инвалидностью "
    "и участников СВО (и их семей). Приоритет по ключевым словам уже посчитан "
    "(regex_priority) — уточни его по сути события, не по формальным маркерам.\n\n"
    "Критерии, от главного к второстепенному:\n"
    "1. Суть события: новая мера поддержки > изменение суммы/условий выплаты > "
    "изменение порядка получения без суммы > информационная публикация без акта.\n"
    "2. Денежная составляющая: конкретная цифра/процент выплаты — выше приоритет.\n"
    "3. Срок вступления в силу: уже действует/наступает скоро — выше; отдалённый или "
    "неясный срок — ниже.\n"
    "4. Уровень власти (федеральный/региональный) — только уточняющий признак, сам по "
    "себе приоритет не определяет.\n\n"
    'Для каждого сигнала определи приоритет: "high", "medium" или "low".\n'
    "Ответь СТРОГО JSON-массивом, без текста вокруг, формат:\n"
    '[{"id": <int>, "priority": "high|medium|low", "reason": "<кратко почему>"}, ...]\n\n'
    "Сигналы:\n"
)


def _format_item(ctx: SignalPriorityContext) -> str:
    summary_part = f', "summary": {ctx.summary!r}' if ctx.summary else ""
    categories = ", ".join(c.value for c in ctx.categories) or "—"
    words = ", ".join(ctx.priority_word_matches) or "—"
    return (
        f'{{"id": {ctx.signal_id}, "title": {ctx.title!r}{summary_part}, '
        f'"regex_priority": "{ctx.regex_priority.value}", "категории": "{categories}", '
        f'"тип_события": "{ctx.event_type.value}", "регион": "{ctx.region.value}", '
        f'"слова_приоритета": "{words}"}}'
    )


def _build_prompt(contexts: Sequence[SignalPriorityContext]) -> str:
    return _PROMPT_HEADER + "\n".join(_format_item(ctx) for ctx in contexts)


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _strip_code_fence(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text).strip()


def _parse_batch_response(raw: str) -> dict[int, tuple[Priority, str]] | None:
    """Парсит ответ LLM. `None` — ответ целиком не является валидным JSON-массивом
    (триггер ретрая батча, спека п.2). Построчно валидный JSON с отдельными
    некорректными элементами — эти элементы просто пропускаются (fallback на regex для
    конкретного сигнала, без ретрая всего батча)."""
    try:
        data = json.loads(_strip_code_fence(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list):
        return None

    result: dict[int, tuple[Priority, str]] = {}
    for item in data:
        if not isinstance(item, dict):
            log.warning("пропущен нестроковый элемент батч-ответа LLM: %r", item)
            continue
        try:
            signal_id = int(item["id"])
            priority = Priority(str(item["priority"]).strip().lower())
        except (KeyError, ValueError, TypeError):
            log.warning("пропущен некорректный элемент батч-ответа LLM: %r", item)
            continue
        reason = str(item.get("reason") or "").strip()
        result[signal_id] = (priority, reason)
    return result


def _call_llm(prompt: str, llm_client: ClassifierLLMClient) -> dict[int, tuple[Priority, str]]:
    """Один запрос + один ретрай батча целиком при сбое/невалидном JSON (спека п.2).
    После двух неудачных попыток — пустой результат, вызывающий код деградирует к
    regex для всех сигналов батча."""
    for attempt in (1, 2):
        try:
            raw = llm_client.complete(prompt)
        except LLMError:
            log.warning("LLM недоступен при батч-приоритизации (попытка %d/2)", attempt, exc_info=True)
            continue
        parsed = _parse_batch_response(raw)
        if parsed is not None:
            return parsed
        log.warning("LLM вернул невалидный JSON при батч-приоритизации (попытка %d/2): %r", attempt, raw)
    log.warning("батч-приоритизация LLM не удалась после ретрая — fallback на regex для всего батча")
    return {}


def _combine(regex_priority: Priority, llm_priority: Priority) -> tuple[Priority, str, bool]:
    """Спека, раздел «Решения» п.4: LLM сдвигает максимум на один уровень
    (low<->medium, medium<->high). Скачок low<->high не применяется — расхождение."""
    diff = _PRIORITY_ORDER[llm_priority] - _PRIORITY_ORDER[regex_priority]
    if diff == 0:
        return regex_priority, "regex", False
    if abs(diff) == 1:
        return llm_priority, "llm_adjusted", False
    return regex_priority, "regex", True


def _regex_only(ctx: SignalPriorityContext) -> PriorityRefinement:
    return PriorityRefinement(
        signal_id=ctx.signal_id,
        regex_priority=ctx.regex_priority,
        llm_priority=None,
        llm_reason=None,
        final_priority=ctx.regex_priority,
        source="regex",
    )


def refine_priorities_batch(
    session: Session,
    signal_ids: Sequence[int],
    llm_client: ClassifierLLMClient | None,
) -> list[PriorityRefinement]:
    """Батч-уточнение приоритета MEDIUM/LOW-сигналов (спека, раздел «Решения», образец —
    `parser/dedup.py`). `llm_client=None` (GLM не сконфигурирован) — сразу regex для
    всех сигналов, без обращения к сети. Сигналы не из скоупа (не найдены или уже HIGH)
    из результата молча выпадают (`_load_context`)."""
    keywords = load_classification_keywords()
    contexts = [ctx for sid in signal_ids if (ctx := _load_context(session, sid, keywords)) is not None]
    if not contexts:
        return []

    if llm_client is None:
        return [_regex_only(ctx) for ctx in contexts]

    parsed = _call_llm(_build_prompt(contexts), llm_client)

    refinements = []
    for ctx in contexts:
        match = parsed.get(ctx.signal_id)
        if match is None:
            refinements.append(_regex_only(ctx))
            continue
        llm_priority, reason = match
        final_priority, source, discrepancy = _combine(ctx.regex_priority, llm_priority)
        refinements.append(
            PriorityRefinement(
                signal_id=ctx.signal_id,
                regex_priority=ctx.regex_priority,
                llm_priority=llm_priority,
                llm_reason=reason or None,
                final_priority=final_priority,
                source=source,
                discrepancy=discrepancy,
            )
        )
    return refinements


def apply_refinements(session: Session, refinements: Iterable[PriorityRefinement]) -> list[PriorityRefinement]:
    """Записывает в БД только реально изменённые приоритеты (`source == "llm_adjusted"`).
    Не коммитит — коммит на совести вызывающего кода (`orchestrator.run_all`/скрипты),
    как и остальной код `db/service.py`. Возвращает применённые изменения."""
    applied = []
    for refinement in refinements:
        if refinement.source != "llm_adjusted":
            continue
        signal = session.get(Signal, refinement.signal_id)
        if signal is None:
            continue
        signal.priority = refinement.final_priority
        applied.append(refinement)
    return applied
