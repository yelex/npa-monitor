"""Bot MVP (PLAN.md Фаза 5): aiogram 3, whitelist, карточки сигналов с
inline-кнопками, FSM-сценарии, команды /today /pending /history /stats /reopen.

Запуск: python -m bot
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.orm import Session, selectinload, sessionmaker

from bot.autoupdate_client import AutoUpdateAgentClient, archive_result, list_pending_results, task_path
from config import get_settings
from db.catalog import RegionEntry, access_for_domain, all_domains, load_regions
from db.enums import Priority, Region, RejectionReason, SignalCategory, SignalStatus
from db.models import Signal, SignalCategoryLink, StatusHistory
from db.service import InvalidStatusTransition, transition_status
from db.session import init_db, make_engine, make_session_factory
from parser.fetcher import SourceUnavailable, fetch
from parser.filters import domain_of, is_domain_whitelisted

log = logging.getLogger("bot")

router = Router()

_session_factory: sessionmaker[Session] | None = None
_autoupdate_client = AutoUpdateAgentClient()


def get_session_factory() -> sessionmaker[Session]:
    """`db.session.make_session_factory` требует `engine` — здесь он собирается один раз
    из `config.get_settings().database_path` (не был передан ни разу в исходном коде
    ветки `phase4-classifier`, откуда пришёл этот модуль — исправлено при слиянии)."""
    global _session_factory
    if _session_factory is None:
        engine = make_engine(get_settings().database_path)
        init_db(engine)
        _session_factory = make_session_factory(engine)
    return _session_factory


CATEGORY_LABELS = {"veterans": "ВБД", "disabled": "Инвалиды", "svo": "СВО"}
STATUS_LABELS = {
    SignalStatus.NEW: "Новый",
    SignalStatus.IN_PROGRESS: "В работе",
    SignalStatus.POSTPONED: "Отложен",
    SignalStatus.REJECTED: "Отклонён",
    SignalStatus.SENT_TO_AGENT: "Передан агенту",
    SignalStatus.COMPLETED: "Завершён",
}
EVENT_LABELS = {
    "new_document": "Новый документ",
    "amendment": "Изменение",
    "repeal": "Отмена",
    "entry_into_force": "Вступление в силу",
    "review": "Обзор",
}
PRIORITY_LABELS = {Priority.HIGH: "🔴 Высокий", Priority.MEDIUM: "🟡 Средний", Priority.LOW: "🟢 Низкий"}
PRIORITY_ORDER = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}
REGION_LABELS = {"rf": "РФ", "moscow": "Москва", "undefined": "Не определён"}
REJECT_REASONS = {
    "r_not_target": "Не относится к целевым категориям",
    "r_dup": "Дубликат",
    "r_not_npa": "Не является НПА",
    "r_other": "Другое",
}


class NpaFlow(StatesGroup):
    ask_npa_link = State()
    ask_reject_reason = State()
    # docs/SPEC_analyst_confirm_ls_region.md: подтверждение ЖС и региона аналитиком
    # после ссылки на НПА и перед SENT_TO_AGENT — два дополнительных шага одного флоу.
    ask_categories = State()
    ask_region = State()


# --- Auth --------------------------------------------------------------------


def is_allowed(user_id: int) -> bool:
    return user_id in get_settings().allowed_user_ids


# --- Rendering ---------------------------------------------------------------


def signal_card(s: Signal, categories: list[str]) -> str:
    """Заголовок и ссылка — из реального веб-контента (листинги, Yandex Search), не
    контролируются нами: экранируем через `html.escape` перед вставкой в HTML-разметку
    Telegram (`parse_mode=HTML`, см. `main()`) — иначе `&` в URL с query-параметрами
    (например, `...html?&pdf_file=y`, реальный случай) или `<`/`>` в заголовке ломают
    разбор всего сообщения на стороне Telegram (`can't parse entities`)."""
    cats = ", ".join(CATEGORY_LABELS.get(c, c) for c in categories) or "—"
    title = html.escape(s.title or "(без названия)", quote=False)
    source_url = html.escape(s.source_url, quote=False) if s.source_url else ""
    lines = [
        f"🆔 <b>{s.id}</b> · {PRIORITY_LABELS.get(s.priority, s.priority)}",
        f"<b>{title}</b>",
        f"ЖС: {cats}",
        f"Тип: {EVENT_LABELS.get(s.event_type.value if s.event_type else '', s.event_type or '—')}",
        f"Регион: {REGION_LABELS.get(s.region.value if s.region else '', s.region or '—')}",
        f"Статус: {STATUS_LABELS.get(s.status, s.status)}",
        f"Дата: {s.created_at:%d.%m.%Y %H:%M}",
        f"🔗 {source_url}" if source_url else "",
    ]
    return "\n".join(x for x in lines if x)


def _sent_to_agent_transition(s: Signal) -> StatusHistory | None:
    """Переход в SENT_TO_AGENT происходит ровно один раз за жизнь сигнала (нет пути
    назад из SENT_TO_AGENT/COMPLETED в IN_PROGRESS, см. `db.service.ALLOWED_TRANSITIONS`)
    — берём последнюю запись на случай будущих исключений из этого правила."""
    matches = [h for h in s.history if h.to_status == SignalStatus.SENT_TO_AGENT]
    return matches[-1] if matches else None


def sent_signal_card(s: Signal, categories: list[str], transition: StatusHistory | None) -> str:
    """Карточка для /sent: в отличие от `signal_card`, ЖС/регион показывают итог
    подтверждения аналитиком (Фаза 10, `_finish_npa_flow` перезаписывает эти поля
    подтверждёнными значениями до перехода в SENT_TO_AGENT), плюс кто/когда передал и
    расхождение с классификатором (`_audit_reason`), если оно было."""
    cats = ", ".join(CATEGORY_LABELS.get(c, c) for c in categories) or "—"
    title = html.escape(s.title or "(без названия)", quote=False)
    npa_link = html.escape(s.npa_link, quote=False) if s.npa_link else "—"
    lines = [
        f"🆔 <b>{s.id}</b> · {STATUS_LABELS.get(s.status, s.status)}",
        f"<b>{title}</b>",
        f"ЖС: {cats}",
        f"Регион: {REGION_LABELS.get(s.region.value if s.region else '', s.region or '—')}",
        f"Ссылка на НПА: {npa_link}",
    ]
    if transition is not None:
        who = html.escape(str(transition.changed_by), quote=False) if transition.changed_by else "—"
        lines.append(f"Передал агенту: {who}, {transition.changed_at:%d.%m.%Y %H:%M}")
        if transition.reason and "classifier=" in transition.reason and "confirmed=" in transition.reason:
            reason = html.escape(transition.reason, quote=False)
            lines.append(f"правки аналитика: {reason}")
    return "\n".join(x for x in lines if x)


def signal_kb(sig_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Взять в работу", callback_data=f"sig:{sig_id}:work"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"sig:{sig_id}:reject"),
            InlineKeyboardButton(text="↩️ Позже", callback_data=f"sig:{sig_id}:later"),
        ]
    ])


def reject_kb(sig_id: int) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=label, callback_data=f"rej:{sig_id}:{code}")
           for code, label in REJECT_REASONS.items()]
    return InlineKeyboardMarkup(inline_keyboard=[row[:2], row[2:]])


def category_toggle_kb(sig_id: int, selected: set[str]) -> InlineKeyboardMarkup:
    """SPEC п.2: toggle ✅/⬜ по каждой ЖС (паттерн `priority_filter_kb`) + «Подтвердить»
    отдельной строкой. `selected` — предвыбор из `signal.categories` (классификатор)."""
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if code in selected else '⬜'} {label}",
            callback_data=f"catc:{sig_id}:{code}",
        )]
        for code, label in CATEGORY_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="Подтвердить", callback_data=f"catc:{sig_id}:confirm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def region_kb(sig_id: int) -> InlineKeyboardMarkup:
    """SPEC п.3: кнопки РФ/Москва/Не определён + «Другое» (свободный текст, обрабатывает
    `on_region_manual`)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=REGION_LABELS[code], callback_data=f"reg:{sig_id}:{code}")
            for code in ("rf", "moscow", "undefined")
        ],
        [InlineKeyboardButton(text="Другое", callback_data=f"reg:{sig_id}:other")],
    ])


# Порядок и коды сортировки/фильтра по приоритету в кнопках под списком сигналов
# (пользовательский запрос: «возможность сортировки Высокий/средний/низкий из
# интерфейса бота»). "all" — без фильтра, в исходном порядке команды.
PRIORITY_FILTER_OPTIONS = (("all", "Все"), ("high", "🔴"), ("medium", "🟡"), ("low", "🟢"))
_PRIORITY_BY_FILTER_CODE = {"high": Priority.HIGH, "medium": Priority.MEDIUM, "low": Priority.LOW}


def priority_filter_kb(list_kind: str, active_code: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"[{label}]" if code == active_code else label,
            callback_data=f"filter:{list_kind}:{code}",
        )
        for code, label in PRIORITY_FILTER_OPTIONS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def apply_priority_filter(signals: list[Signal], code: str) -> list[Signal]:
    """`code="all"` — без фильтра, порядок не меняется. Иначе — только сигналы с этим
    приоритетом, отсортированные (внутри одного приоритета сортировка не нужна, но
    сохраняем стабильный порядок по дате создания — предсказуемее для эксперта)."""
    if code == "all":
        return signals
    wanted = _PRIORITY_BY_FILTER_CODE[code]
    return sorted((s for s in signals if s.priority == wanted), key=lambda s: s.created_at)


# --- Команды -----------------------------------------------------------------

START_TEXT = (
    "👋 Мониторинг изменений в НПА по мерам поддержки (ВБД, инвалиды, СВО).\n\n"
    "Раз в день парсер обходит источники и присылает карточки сигналов о новых/"
    "изменённых НПА с приоритетом. По каждой карточке: ✅ взять в работу, ❌ отклонить, "
    "↩️ отложить. Взяв в работу, пришлите ссылку на полный текст НПА — бот проверит "
    "домен и доступность и передаст её агенту автообновления.\n\n"
    "Команды:\n"
    "/today — сигналы «Новый»/«Отложен» за сегодня\n"
    "/pending — всё «В работе»/«Отложен»\n"
    "/history — последние 10 из «Передан агенту»/«Завершён»/«Отклонён»\n"
    "/sent — последние 15 переданных агенту, с деталями подтверждения (ЖС, регион, "
    "ссылка на НПА, кто и когда передал)\n"
    "/stats — статистика за 7 дней\n"
    "/digest — сводка новых и отложенных сигналов по запросу\n"
    "/reopen ID — вернуть отклонённый сигнал в «Новый»\n"
    "/complete ID — отметить сигнал завершённым после проверки результата агента"
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    await message.answer(START_TEXT)


LIST_HEADERS = {
    "today": "Сигналы за сегодня",
    "pending": "В работе / отложены",
    "history": "История (последние 10)",
    "digest": "Утренняя сводка",
}


def _signals_for_kind(db: Session, kind: str) -> list[Signal]:
    """Общий подбор сигналов для команд /today /pending /history /digest и для кнопок
    фильтра по приоритету (`on_priority_filter`) — один и тот же запрос независимо от
    того, вызван он напрямую командой или пересчитан заново после нажатия кнопки."""
    if kind == "today":
        today = dt.date.today()
        signals = (
            db.query(Signal)
            .options(selectinload(Signal.categories))
            .filter(Signal.status.in_([SignalStatus.NEW, SignalStatus.POSTPONED]))
            .all()
        )
        return [s for s in signals if s.created_at.date() == today]
    if kind == "pending":
        return (
            db.query(Signal)
            .options(selectinload(Signal.categories))
            .filter(Signal.status.in_([SignalStatus.IN_PROGRESS, SignalStatus.POSTPONED]))
            .order_by(Signal.created_at)
            .all()
        )
    if kind == "history":
        return (
            db.query(Signal)
            .options(selectinload(Signal.categories))
            .filter(Signal.status.in_([
                SignalStatus.SENT_TO_AGENT, SignalStatus.COMPLETED, SignalStatus.REJECTED,
            ]))
            .order_by(Signal.updated_at.desc())
            .limit(10)
            .all()
        )
    if kind == "digest":
        return _digest_signals(db)
    if kind == "sent":
        return (
            db.query(Signal)
            .options(selectinload(Signal.categories), selectinload(Signal.history))
            .filter(Signal.status.in_([SignalStatus.SENT_TO_AGENT, SignalStatus.COMPLETED]))
            .order_by(Signal.updated_at.desc())
            .limit(15)
            .all()
        )
    raise ValueError(f"неизвестный вид списка сигналов: {kind!r}")


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    with get_session_factory()() as db:
        signals = _signals_for_kind(db, "today")
    await _send_list(message, signals, "today")


@router.message(Command("pending"))
async def cmd_pending(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    with get_session_factory()() as db:
        signals = _signals_for_kind(db, "pending")
    await _send_list(message, signals, "pending")


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    with get_session_factory()() as db:
        signals = _signals_for_kind(db, "history")
    await _send_list(message, signals, "history")


@router.message(Command("sent"))
async def cmd_sent(message: Message) -> None:
    """Что аналитик передал агенту (SENT_TO_AGENT/COMPLETED), с деталями подтверждения
    — в отличие от /today /pending /history, показывает карточки сразу (не прячет их
    за кнопкой фильтра): весь смысл команды в деталях внутри карточки, не в списке."""
    if not is_allowed(message.from_user.id):
        return
    with get_session_factory()() as db:
        signals = _signals_for_kind(db, "sent")
    if not signals:
        await message.answer("Переданных агенту сигналов нет.")
        return
    await message.answer(f"📤 Передано агенту (последние {len(signals)}):")
    for s in signals:
        cats = [c.category.value for c in s.categories] if s.categories else []
        transition = _sent_to_agent_transition(s)
        await message.answer(sent_signal_card(s, cats, transition))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    # tz-aware (UTC) — db.types.UTCDateTime требует aware datetime на входе, см. Signal.*
    week_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    with get_session_factory()() as db:
        qs = db.query(Signal).filter(Signal.created_at >= week_ago).all()
    if not qs:
        await message.answer("За неделю сигналов нет.")
        return
    by_status: dict[str, int] = {}
    for s in qs:
        label = STATUS_LABELS.get(s.status, s.status)
        by_status[label] = by_status.get(label, 0) + 1
    text = f"📊 За 7 дней: {len(qs)} сигналов\n" + "\n".join(
        f"• {k}: {v}" for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])
    )
    await message.answer(text)


@router.message(Command("reopen"))
async def cmd_reopen(message: Message, command: CommandObject) -> None:
    if not is_allowed(message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Формат: /reopen ID")
        return
    sig_id = int(command.args.strip())
    db_factory = get_session_factory()
    with db_factory() as db:
        s = db.get(Signal, sig_id)
        if not s:
            await message.answer(f"Сигнал {sig_id} не найден.")
            return
        try:
            transition_status(db, s, SignalStatus.NEW, changed_by=message.from_user.id, reason="reopen")
        except Exception as e:  # noqa: BLE001
            await message.answer(f"Нельзя: {e}")
            return
        db.commit()
    log.info("сигнал %s -> Новый (/reopen, user=%s)", sig_id, message.from_user.id)
    await message.answer(f"↩️ Сигнал {sig_id} → Новый")


@router.message(Command("complete"))
async def cmd_complete(message: Message, command: CommandObject) -> None:
    """Передан агенту -> Завершён, AGENTS.md раздел 6: «Эксперт проверил результат
    агента». Ручная команда, а не автоматический переход — AutoUpdateAgentClient
    (PLAN.md Фаза 5) пока не подключён к реальному агенту (см. AGENTS.md раздел 16
    п.8), автоматического сигнала «агент ответил» нет."""
    if not is_allowed(message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Формат: /complete ID")
        return
    sig_id = int(command.args.strip())
    db_factory = get_session_factory()
    with db_factory() as db:
        s = db.get(Signal, sig_id)
        if not s:
            await message.answer(f"Сигнал {sig_id} не найден.")
            return
        try:
            transition_status(db, s, SignalStatus.COMPLETED, changed_by=message.from_user.id)
        except Exception as e:  # noqa: BLE001
            await message.answer(f"Нельзя: {e}")
            return
        db.commit()
    log.info("сигнал %s -> Завершён (/complete, user=%s)", sig_id, message.from_user.id)
    await message.answer(f"✅ Сигнал {sig_id} → Завершён")


def _digest_signals(db: Session) -> list[Signal]:
    """Новые + отложенные, отсортированные по приоритету (раздел 10 AGENTS.md).
    Общая логика для команды /digest и автоматической рассылки (_digest_loop)."""
    signals = (
        db.query(Signal)
        .options(selectinload(Signal.categories))
        .filter(Signal.status.in_([SignalStatus.NEW, SignalStatus.POSTPONED]))
        .all()
    )
    signals.sort(key=lambda s: (PRIORITY_ORDER.get(s.priority, 9), s.created_at))
    return signals


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    """Утренняя сводка по запросу (см. также `_digest_loop` — автоматическая рассылка)."""
    if not is_allowed(message.from_user.id):
        return
    with get_session_factory()() as db:
        signals = _digest_signals(db)
    if not signals:
        await message.answer("Новых сигналов нет.")
        return
    await message.answer(
        f"📬 Утренняя сводка: {len(signals)} сигналов", reply_markup=priority_filter_kb("digest", "all")
    )
    for s in signals:
        cats = [c.category.value for c in s.categories] if s.categories else []
        await message.answer(signal_card(s, cats), reply_markup=signal_kb(s.id))


async def _send_list(message: Message, signals: list[Signal], kind: str) -> None:
    """`kind` — ключ в `LIST_HEADERS`/`_signals_for_kind`, он же префикс
    `callback_data` кнопок фильтра по приоритету (`priority_filter_kb`,
    `on_priority_filter`) — по нему кнопка знает, какой запрос повторить.

    Отправляет только заголовок с кнопками, без карточек — найдено вживую: команда
    сразу выгружала все сигналы отдельными сообщениями, даже без выбора фильтра,
    неудобно при десятках сигналов (после подключения Yandex Search,
    docs/SPEC_yandex_search_discovery.md раздел 5). Карточки показываются только по
    нажатию кнопки (`on_priority_filter`) — включая «Все», это тоже осознанный выбор,
    не выполняется автоматически."""
    header = LIST_HEADERS[kind]
    if not signals:
        await message.answer(f"{header}: пусто")
        return
    await message.answer(
        f"{header}: {len(signals)}. Выберите приоритет, чтобы показать карточки:",
        reply_markup=priority_filter_kb(kind, ""),
    )


# --- Кнопки ------------------------------------------------------------------


@router.callback_query(F.data.startswith("filter:"))
async def on_priority_filter(cb: CallbackQuery) -> None:
    """Кнопки под заголовком списка (`priority_filter_kb`) — пересчитывают тот же
    список (`_signals_for_kind`), которым он был построен изначально, с фильтром по
    приоритету. Заголовок редактируется на месте (число сигналов + подсвеченная
    активная кнопка); карточки под ним не трогаются — отправляются новым набором
    сообщений, старые из чата не удаляются (проще и надёжнее, чем частично
    редактировать/удалять уже отправленные карточки)."""
    if not is_allowed(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, kind, code = cb.data.split(":")
    with get_session_factory()() as db:
        signals = apply_priority_filter(_signals_for_kind(db, kind), code)

    header = LIST_HEADERS[kind]
    header_text = f"{header}: пусто" if not signals else f"{header}: {len(signals)}"
    await cb.message.edit_text(header_text, reply_markup=priority_filter_kb(kind, code))  # type: ignore[union-attr]
    for s in signals:
        cats = [c.category.value for c in s.categories] if s.categories else []
        await cb.message.answer(signal_card(s, cats), reply_markup=signal_kb(s.id))  # type: ignore[union-attr]
    await cb.answer()


@router.callback_query(F.data.startswith("sig:"))
async def on_signal_button(cb: CallbackQuery, state: FSMContext) -> None:
    """Найдено вживую (2026-08-20, после добавления второго эксперта в whitelist):
    двойное нажатие «✅ Взять в работу» (или гонка двух экспертов на одном сигнале)
    роняло `InvalidStatusTransition` необработанным — апдейт падал молча, эксперт не
    получал вообще никакого ответа в чате. Все переходы статуса из кнопок теперь
    оборачивают `transition_status` и отвечают понятным сообщением вместо падения."""
    if not is_allowed(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, sig_id_s, action = cb.data.split(":")
    sig_id = int(sig_id_s)
    db_factory = get_session_factory()
    with db_factory() as db:
        s = db.get(Signal, sig_id)
        if not s:
            await cb.answer("Сигнал не найден", show_alert=True)
            return
        if action == "work":
            try:
                transition_status(db, s, SignalStatus.IN_PROGRESS, changed_by=cb.from_user.id)
            except InvalidStatusTransition:
                await cb.answer(
                    f"Сигнал уже в статусе «{STATUS_LABELS.get(s.status, s.status)}» "
                    "(возможно, кто-то уже взял его в работу)",
                    show_alert=True,
                )
                return
            db.commit()
            log.info("сигнал %s -> В работе (user=%s)", sig_id, cb.from_user.id)
            await state.set_state(NpaFlow.ask_npa_link)
            await state.update_data(sig_id=sig_id)
            await cb.message.answer(  # type: ignore[union-attr]
                f"Сигнал {sig_id} в работе. Отправьте ссылку на НПА "
                "(или 'skip', если она уже в карточке)."
            )
        elif action == "reject":
            await state.set_state(NpaFlow.ask_reject_reason)
            await state.update_data(sig_id=sig_id)
            await cb.message.edit_reply_markup(reply_markup=reject_kb(sig_id))  # type: ignore[union-attr]
        elif action == "later":
            try:
                transition_status(db, s, SignalStatus.POSTPONED, changed_by=cb.from_user.id)
            except InvalidStatusTransition:
                await cb.answer(
                    f"Сигнал уже в статусе «{STATUS_LABELS.get(s.status, s.status)}»", show_alert=True
                )
                return
            db.commit()
            log.info("сигнал %s -> Отложен (user=%s)", sig_id, cb.from_user.id)
            await cb.message.answer("↩️ Отложен — вернётся в следующую сводку.")  # type: ignore[union-attr]
    await cb.answer()


@router.callback_query(F.data.startswith("rej:"))
async def on_reject_reason(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_allowed(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, sig_id_s, code = cb.data.split(":")
    sig_id = int(sig_id_s)
    reason = RejectionReason(
        {"r_not_target": "not_target_category", "r_dup": "duplicate",
         "r_not_npa": "not_npa", "r_other": "other"}[code]
    )
    db_factory = get_session_factory()
    with db_factory() as db:
        s = db.get(Signal, sig_id)
        if s:
            try:
                # rejection_reason (не reason=) — иначе db.service.transition_status
                # поднимает ValueError "Отклонение сигнала требует причины" (там
                # reason — свободный текст-заметка в аудите, rejection_reason —
                # структурированное поле Signal).
                transition_status(
                    db, s, SignalStatus.REJECTED, changed_by=cb.from_user.id, rejection_reason=reason
                )
            except InvalidStatusTransition:
                await state.clear()
                await cb.answer(
                    f"Сигнал уже в статусе «{STATUS_LABELS.get(s.status, s.status)}»", show_alert=True
                )
                return
            db.commit()
            log.info("сигнал %s -> Отклонён: %s (user=%s)", sig_id, reason.value, cb.from_user.id)
    await state.clear()
    await cb.message.edit_text(  # type: ignore[union-attr]
        f"❌ Сигнал {sig_id} отклонён: {REJECT_REASONS[code]}"
    )
    await cb.answer()


# --- FSM: ссылка на НПА -> ЖС -> регион -----------------------------------------


@router.message(NpaFlow.ask_npa_link)
async def on_npa_link(message: Message, state: FSMContext) -> None:
    """SPEC_analyst_confirm_ls_region.md решение 1: терминальная логика (DB-коммит +
    transition_status + send агенту) сюда больше не входит — переехала в
    `_finish_npa_flow`, финальный шаг после подтверждения ЖС и региона. Здесь только
    приём и проверка ссылки, дальше — саджест ЖС из классификатора (`ask_categories`)."""
    if not is_allowed(message.from_user.id):
        return
    data = await state.get_data()
    sig_id = data["sig_id"]
    text = (message.text or "").strip()

    autocheck_skipped = False
    if text.lower() == "skip":
        npa_link: str | None = None
    elif not text.startswith("http"):
        await message.answer("Пришлите ссылку на НПА (http/https) или 'skip', если она уже в карточке.")
        return
    else:
        # AGENTS.md раздел 13: белый список доменов — до сетевого запроса.
        if not is_domain_whitelisted(text, all_domains()):
            await message.answer(
                "Домен ссылки не в белом списке источников. Проверьте ссылку и отправьте ещё раз."
            )
            return
        # AGENTS.md раздел 10: «бот проверяет доступность страницы» — вне DB-сессии,
        # чтобы не держать транзакцию открытой во время сетевого запроса.
        access = access_for_domain(domain_of(text))
        if access == "unsupported":
            # Домен документирован как недоступный по сети даже через RU-прокси
            # (docs/SPEC_bot_npa_link_check.md) — принимаем на доверии, без автопроверки.
            autocheck_skipped = True
            npa_link = text
        else:
            try:
                fetch(
                    text,
                    access=access or "direct",
                    ru_proxy_url=get_settings().ru_proxy_url if access == "ru_proxy" else None,
                )
            except SourceUnavailable:
                await message.answer(
                    "Ссылка недоступна. Проверьте её и отправьте ещё раз. Или загрузите файл напрямую."
                )
                return
            npa_link = text

    with get_session_factory()() as db:
        s = db.get(Signal, sig_id)
        if not s:
            await message.answer("Сигнал исчез.")
            await state.clear()
            return
        # Предвыбор toggle-кнопок — текущие категории классификатора (SPEC п.2).
        preselected = sorted({c.category.value for c in s.categories})

    await state.update_data(npa_link=npa_link, autocheck_skipped=autocheck_skipped, categories=preselected)
    await state.set_state(NpaFlow.ask_categories)
    await message.answer(
        "Ссылка принята. Подтвердите ЖС (жизненную ситуацию):",
        reply_markup=category_toggle_kb(sig_id, set(preselected)),
    )


@router.callback_query(F.data.startswith("catc:"))
async def on_category_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    """SPEC п.2: `catc:<sig>:<code>` — toggle категории, `catc:<sig>:confirm` — переход
    к шагу региона (пустой набор блокируется alert'ом, БД не трогается до финального шага)."""
    if not is_allowed(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, sig_id_s, code = cb.data.split(":")
    sig_id = int(sig_id_s)
    data = await state.get_data()
    selected = set(data.get("categories", []))

    if code == "confirm":
        if not selected:
            await cb.answer("Выберите хотя бы одну ЖС", show_alert=True)
            return
        await state.set_state(NpaFlow.ask_region)
        await cb.message.edit_text(  # type: ignore[union-attr]
            "ЖС подтверждены. Выберите регион:", reply_markup=region_kb(sig_id)
        )
        await cb.answer()
        return

    if code in selected:
        selected.discard(code)
    else:
        selected.add(code)
    await state.update_data(categories=sorted(selected))
    await cb.message.edit_reply_markup(reply_markup=category_toggle_kb(sig_id, selected))  # type: ignore[union-attr]
    await cb.answer()


def find_region_matches(query: str) -> list[RegionEntry]:
    """SPEC п.3: поиск по `db.catalog.load_regions()` (casefold, подстрока по name/code).
    Новый `Region` на лету не создаём — источник истины справочник `data/regions.yaml`."""
    q = query.strip().casefold()
    if not q:
        return []
    return [r for r in load_regions() if q in r.name.casefold() or q in r.code.casefold()]


def _audit_reason(
    old_categories: list[str], old_region: Region, new_categories: list[str], new_region: Region
) -> str | None:
    """SPEC п.5/решение 5: расхождение классификатора с подтверждением аналитика —
    в `StatusHistory.reason`, grep-able формат `classifier=... region=... -> confirmed=...`.
    Без расхождения — `None` (запись не пишем)."""
    if sorted(old_categories) == sorted(new_categories) and old_region == new_region:
        return None
    return (
        f"classifier={','.join(sorted(old_categories)) or 'none'} region={old_region.value} -> "
        f"confirmed={','.join(sorted(new_categories)) or 'none'} region={new_region.value}"
    )


async def _finish_npa_flow(
    target: Message, state: FSMContext, sig_id: int, region: Region, *, changed_by: int
) -> None:
    """Финальный шаг флоу (SPEC решение 1): одна DB-сессия — npa_link, перезапись
    `signal_categories`/`Signal.region` подтверждёнными значениями, `transition_status`
    в SENT_TO_AGENT (с аудитом расхождения), `_autoupdate_client.send` (запись задачи в
    spool ДО commit — docs/SPEC_autoupdate_agent_contract.md раздел 3.2: если запись
    упала, переход не коммитится, аналитик видит ошибку и может повторить — `send()`
    идемпотентен, перезапись безопасна), commit, ответ аналитику, `state.clear()`.
    `target` — объект с `.answer()` (Message или `CallbackQuery.message`), гонки
    статусов — как в остальном коде (try/except)."""
    data = await state.get_data()
    categories: list[str] = data.get("categories", [])
    npa_link: str | None = data.get("npa_link")
    autocheck_skipped: bool = data.get("autocheck_skipped", False)

    with get_session_factory()() as db:
        s = db.get(Signal, sig_id)
        if not s:
            await target.answer("Сигнал исчез.")
            await state.clear()
            return
        if npa_link is not None:
            s.npa_link = npa_link

        old_categories = sorted(c.category.value for c in s.categories)
        old_region = s.region
        s.categories = [SignalCategoryLink(category=SignalCategory(c)) for c in categories]
        s.region = region
        reason = _audit_reason(old_categories, old_region, categories, region)

        try:
            transition_status(db, s, SignalStatus.SENT_TO_AGENT, changed_by=changed_by, reason=reason)
        except InvalidStatusTransition:
            await target.answer(
                f"Сигнал уже в статусе «{STATUS_LABELS.get(s.status, s.status)}» — "
                "возможно, кто-то уже отправил ссылку раньше."
            )
            await state.clear()
            return

        try:
            _autoupdate_client.send(s, s.npa_link, s.source_url, categories, region.value)
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception(
                "автообновление: не удалось записать задачу для сигнала %s — переход отменён", sig_id
            )
            await target.answer(
                "⚠️ Не удалось передать сигнал агенту автообновления (сбой записи задачи). "
                "Статус не изменён, попробуйте ещё раз."
            )
            await state.clear()
            return

        db.commit()
        log.info("передано агенту автообновления: signal=%s link=%s", sig_id, s.npa_link)

    if autocheck_skipped:
        domain = domain_of(npa_link) if npa_link else ""
        await target.answer(
            f"✅ Ссылка принята (домен {domain} не поддерживает автопроверку, "
            "проверьте вручную). Статус: Передан агенту."
        )
    else:
        await target.answer("✅ Ссылка принята. Статус: Передан агенту.")
    await state.clear()


@router.callback_query(F.data.startswith("reg:"))
async def on_region_button(cb: CallbackQuery, state: FSMContext) -> None:
    """SPEC п.3: РФ/Москва/Не определён — сразу финальный шаг; «Другое» — просит текст,
    обрабатывает `on_region_manual` в том же состоянии `ask_region`."""
    if not is_allowed(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, sig_id_s, code = cb.data.split(":")
    sig_id = int(sig_id_s)
    if code == "other":
        await cb.message.answer("Введите название региона (или часть) текстом.")  # type: ignore[union-attr]
        await cb.answer()
        return
    await _finish_npa_flow(cb.message, state, sig_id, Region(code), changed_by=cb.from_user.id)  # type: ignore[arg-type]
    await cb.answer()


@router.message(NpaFlow.ask_region)
async def on_region_manual(message: Message, state: FSMContext) -> None:
    """Свободный текст в состоянии `ask_region` — поиск по справочнику (SPEC п.3):
    1 матч принимается сразу, 0 — «не найден», 2+ — список кандидатов на уточнение."""
    if not is_allowed(message.from_user.id):
        return
    data = await state.get_data()
    sig_id = data["sig_id"]
    query = (message.text or "").strip()
    matches = find_region_matches(query)

    if not matches:
        await message.answer(
            "Регион не найден в справочнике. Попробуйте другой запрос или выберите "
            "РФ/Москву/«Не определён» кнопкой выше."
        )
        return
    if len(matches) > 1:
        listing = "\n".join(f"• {r.name} ({r.code})" for r in matches)
        await message.answer(f"Найдено несколько регионов, уточните запрос:\n{listing}")
        return

    await _finish_npa_flow(message, state, sig_id, matches[0].region, changed_by=message.from_user.id)


# --- reminder ------------------------------------------------------------------


async def remind_stale(bot: Bot) -> None:
    """Сигналы >3 дней в «В работе» → напоминание (AGENTS.md раздел 12)."""
    # tz-aware (UTC) — db.types.UTCDateTime требует aware datetime на входе.
    threshold = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    db_factory = get_session_factory()
    with db_factory() as db:
        stale = db.query(Signal).filter(
            Signal.status == SignalStatus.IN_PROGRESS, Signal.updated_at < threshold
        ).all()
    for s in stale:
        title = html.escape(s.title or "(без названия)", quote=False)
        for uid in get_settings().allowed_user_ids:
            await bot.send_message(uid, f"⏰ Сигнал {s.id} в работе больше 3 дней: {title}")


async def _reminder_loop(bot: Bot) -> None:
    while True:
        try:
            await remind_stale(bot)
        except Exception:  # noqa: BLE001
            log.exception("reminder loop error")
        await asyncio.sleep(6 * 3600)


# --- утренняя сводка (рассылка) -------------------------------------------------

DIGEST_HOUR = 8  # AGENTS.md раздел 5: ~08:00, через ~2ч после обхода парсером (06:00)


def _seconds_until_next(hour: int, *, now: dt.datetime | None = None) -> float:
    """Сколько секунд ждать до следующего наступления `hour:00` локального времени."""
    now = now or dt.datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


async def send_digest(bot: Bot) -> None:
    """Рассылает утреннюю сводку всем допущенным экспертам (AGENTS.md раздел 5).
    Логика подбора/сортировки сигналов — та же, что у команды /digest (`_digest_signals`),
    но здесь адресат — все `allowed_user_ids`, а не тот, кто вызвал команду."""
    with get_session_factory()() as db:
        signals = _digest_signals(db)

    for uid in get_settings().allowed_user_ids:
        if not signals:
            await bot.send_message(uid, "Новых сигналов нет.")
            continue
        header_text = f"📬 Утренняя сводка: {len(signals)} сигналов"
        await bot.send_message(uid, header_text, reply_markup=priority_filter_kb("digest", "all"))
        for s in signals:
            cats = [c.category.value for c in s.categories] if s.categories else []
            await bot.send_message(uid, signal_card(s, cats), reply_markup=signal_kb(s.id))


async def _digest_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(_seconds_until_next(DIGEST_HOUR))
        try:
            await send_digest(bot)
        except Exception:  # noqa: BLE001
            log.exception("digest loop error")


# --- автообновление: сверка spool при старте + карточки по результатам ----------


def _reconcile_spool_tasks(db: Session, client: AutoUpdateAgentClient) -> int:
    """docs/SPEC_autoupdate_agent_contract.md раздел 3.2: сигналы в SENT_TO_AGENT без
    файла задачи в spool (обычный путь `_finish_npa_flow` пишет задачу до commit — сюда
    можно попасть только если файл потерян/удалён вручную между запусками) -> дозапись,
    `AutoUpdateAgentClient.send` идемпотентен. Вызывается при старте бота (`main()`)."""
    spool_dir = Path(get_settings().autoupdate_spool_dir)
    signals = (
        db.query(Signal)
        .options(selectinload(Signal.categories))
        .filter(Signal.status == SignalStatus.SENT_TO_AGENT)
        .all()
    )
    written = 0
    for s in signals:
        if task_path(spool_dir, s.id).exists():
            continue
        cats = [c.category.value for c in s.categories]
        client.send(s, s.npa_link, s.source_url, cats, s.region.value)
        written += 1
    if written:
        log.info("автообновление: дозаписано %d задач(и) в spool при сверке", written)
    return written


RESULTS_SCAN_INTERVAL = 300  # 5 минут, SPEC раздел 3.6

RESULT_STATUS_LABELS = {"done": "готово", "nothing_found": "ничего не найдено", "error": "ошибка"}


def result_card(payload: dict, s: Signal) -> str:
    """Карточка по результату агента (SPEC раздел 3.4/3.6): статус, summary (+details,
    если есть). Решение о переходе в «Завершён» — за аналитиком, `/complete <id>`."""
    status = payload.get("status", "?")
    status_label = RESULT_STATUS_LABELS.get(status, status)
    title = html.escape(s.title or "(без названия)", quote=False)
    summary = html.escape(str(payload.get("summary") or "—"), quote=False)
    lines = [
        f"🤖 Агент завершил задачу по сигналу {s.id}: {status_label}",
        f"<b>{title}</b>",
        summary,
    ]
    details = payload.get("details")
    if details:
        lines.append(html.escape(str(details), quote=False))
    lines.append(f"Проверьте результат и выполните /complete {s.id}, если всё верно.")
    return "\n".join(lines)


async def scan_autoupdate_results(bot: Bot) -> int:
    """SPEC раздел 3.6: сканирует `results/*.json`, шлёт карточку аналитику, архивирует
    в `results/.processed/`. Возвращает число отправленных карточек (для тестов/логов)."""
    spool_dir = Path(get_settings().autoupdate_spool_dir)
    sent_count = 0
    for path in list_pending_results(spool_dir):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("автообновление: не удалось прочитать результат %s", path)
            continue

        signal_id = payload.get("signal_id")
        s = None
        if isinstance(signal_id, int):
            with get_session_factory()() as db:
                s = db.get(Signal, signal_id)
        if s is None:
            # SPEC раздел 3.5: неизвестный signal_id — карточку не шлём, но архивируем,
            # иначе тот же файл пересканируется и логирует warning каждые 5 минут вечно.
            log.warning(
                "автообновление: результат с неизвестным signal_id=%r (%s), пропуск",
                signal_id, path,
            )
            archive_result(path, spool_dir)
            continue

        text = result_card(payload, s)
        for uid in get_settings().allowed_user_ids:
            await bot.send_message(uid, text)
        archive_result(path, spool_dir)
        sent_count += 1
    return sent_count


async def _results_scan_loop(bot: Bot) -> None:
    while True:
        try:
            await scan_autoupdate_results(bot)
        except Exception:  # noqa: BLE001
            log.exception("results scan loop error")
        await asyncio.sleep(RESULTS_SCAN_INTERVAL)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан")
    # parse_mode="HTML" по умолчанию для всех отправок — без него `<b>...</b>` в
    # signal_card() показывался пользователю буквально, а не жирным (найдено вживую
    # по жалобе пользователя: «там есть <b></b>»). Раньше нигде не передавался ни
    # глобально, ни поштучно в message.answer()/bot.send_message().
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    # SPEC раздел 3.2: сверка spool при старте — до начала поллинга.
    with get_session_factory()() as db:
        _reconcile_spool_tasks(db, _autoupdate_client)
    reminder = asyncio.create_task(_reminder_loop(bot))
    digest = asyncio.create_task(_digest_loop(bot))
    results_scan = asyncio.create_task(_results_scan_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminder.cancel()
        digest.cancel()
        results_scan.cancel()


if __name__ == "__main__":
    asyncio.run(main())
