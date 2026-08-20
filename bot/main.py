"""Bot MVP (PLAN.md Фаза 5): aiogram 3, whitelist, карточки сигналов с
inline-кнопками, FSM-сценарии, команды /today /pending /history /stats /reopen.

Запуск: python -m bot
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, Dispatcher, F, Router
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

from bot.autoupdate_client import AutoUpdateAgentClient
from config import get_settings
from db.catalog import all_domains
from db.enums import Priority, RejectionReason, SignalStatus
from db.models import Signal
from db.service import transition_status
from db.session import init_db, make_engine, make_session_factory
from parser.fetcher import SourceUnavailable, fetch
from parser.filters import is_domain_whitelisted

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


# --- Auth --------------------------------------------------------------------


def is_allowed(user_id: int) -> bool:
    return user_id in get_settings().allowed_user_ids


# --- Rendering ---------------------------------------------------------------


def signal_card(s: Signal, categories: list[str]) -> str:
    cats = ", ".join(CATEGORY_LABELS.get(c, c) for c in categories) or "—"
    lines = [
        f"🆔 <b>{s.id}</b> · {PRIORITY_LABELS.get(s.priority, s.priority)}",
        f"<b>{s.title or '(без названия)'}</b>",
        f"ЖС: {cats}",
        f"Тип: {EVENT_LABELS.get(s.event_type.value if s.event_type else '', s.event_type or '—')}",
        f"Регион: {REGION_LABELS.get(s.region.value if s.region else '', s.region or '—')}",
        f"Статус: {STATUS_LABELS.get(s.status, s.status)}",
        f"Дата: {s.created_at:%d.%m.%Y %H:%M}",
        f"🔗 {s.source_url}" if s.source_url else "",
    ]
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


# --- Команды -----------------------------------------------------------------


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    today = dt.date.today()
    with get_session_factory()() as db:
        signals = (
            db.query(Signal)
            .options(selectinload(Signal.categories))
            .filter(Signal.status.in_([SignalStatus.NEW, SignalStatus.POSTPONED]))
            .all()
        )
        signals = [s for s in signals if s.created_at.date() == today]
    await _send_list(message, signals, "Сигналы за сегодня")


@router.message(Command("pending"))
async def cmd_pending(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    with get_session_factory()() as db:
        signals = (
            db.query(Signal)
            .options(selectinload(Signal.categories))
            .filter(Signal.status.in_([SignalStatus.IN_PROGRESS, SignalStatus.POSTPONED]))
            .order_by(Signal.created_at)
            .all()
        )
    await _send_list(message, signals, "В работе / отложены")


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    with get_session_factory()() as db:
        signals = (
            db.query(Signal)
            .options(selectinload(Signal.categories))
            .filter(Signal.status.in_([
                SignalStatus.SENT_TO_AGENT, SignalStatus.COMPLETED, SignalStatus.REJECTED,
            ]))
            .order_by(Signal.updated_at.desc())
            .limit(10)
            .all()
        )
    await _send_list(message, signals, "История (последние 10)")


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
        await message.answer("Формат: /reopen <id>")
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
        await message.answer("Формат: /complete <id>")
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


@router.message(Command("digest"))
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


async def cmd_digest(message: Message) -> None:
    """Утренняя сводка по запросу (см. также `_digest_loop` — автоматическая рассылка)."""
    if not is_allowed(message.from_user.id):
        return
    with get_session_factory()() as db:
        signals = _digest_signals(db)
    if not signals:
        await message.answer("Новых сигналов нет.")
        return
    await message.answer(f"📬 Утренняя сводка: {len(signals)} сигналов")
    for s in signals:
        cats = [c.category.value for c in s.categories] if s.categories else []
        await message.answer(signal_card(s, cats), reply_markup=signal_kb(s.id))


async def _send_list(message: Message, signals: list[Signal], header: str) -> None:
    if not signals:
        await message.answer(f"{header}: пусто")
        return
    await message.answer(f"{header}: {len(signals)}")
    for s in signals:
        cats = [c.category.value for c in s.categories] if s.categories else []
        await message.answer(signal_card(s, cats), reply_markup=signal_kb(s.id))


# --- Кнопки ------------------------------------------------------------------


@router.callback_query(F.data.startswith("sig:"))
async def on_signal_button(cb: CallbackQuery, state: FSMContext) -> None:
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
            transition_status(db, s, SignalStatus.IN_PROGRESS, changed_by=cb.from_user.id)
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
            transition_status(db, s, SignalStatus.POSTPONED, changed_by=cb.from_user.id)
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
            # rejection_reason (не reason=) — иначе db.service.transition_status поднимает
            # ValueError "Отклонение сигнала требует причины" (там reason — свободный
            # текст-заметка в аудите, rejection_reason — структурированное поле Signal).
            transition_status(
                db, s, SignalStatus.REJECTED, changed_by=cb.from_user.id, rejection_reason=reason
            )
            db.commit()
            log.info("сигнал %s -> Отклонён: %s (user=%s)", sig_id, reason.value, cb.from_user.id)
    await state.clear()
    await cb.message.edit_text(  # type: ignore[union-attr]
        f"❌ Сигнал {sig_id} отклонён: {REJECT_REASONS[code]}"
    )
    await cb.answer()


# --- FSM: ссылка на НПА --------------------------------------------------------


@router.message(NpaFlow.ask_npa_link)
async def on_npa_link(message: Message, state: FSMContext) -> None:
    if not is_allowed(message.from_user.id):
        return
    data = await state.get_data()
    sig_id = data["sig_id"]
    text = (message.text or "").strip()

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
        try:
            fetch(text, access="direct")
        except SourceUnavailable:
            await message.answer(
                "Ссылка недоступна. Проверьте её и отправьте ещё раз. Или загрузите файл напрямую."
            )
            return
        npa_link = text

    db_factory = get_session_factory()
    with db_factory() as db:
        s = db.get(Signal, sig_id)
        if not s:
            await message.answer("Сигнал исчез.")
            await state.clear()
            return
        if npa_link is not None:
            s.npa_link = npa_link
        transition_status(db, s, SignalStatus.SENT_TO_AGENT, changed_by=message.from_user.id)
        db.commit()
        _autoupdate_client.send(s.npa_link, s.measure_id)
        log.info("передано агенту автообновления: signal=%s link=%s", sig_id, s.npa_link)

    await message.answer("✅ Ссылка принята. Статус: Передан агенту.")
    await state.clear()


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
        for uid in get_settings().allowed_user_ids:
            await bot.send_message(uid, f"⏰ Сигнал {s.id} в работе больше 3 дней: {s.title}")


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
        await bot.send_message(uid, f"📬 Утренняя сводка: {len(signals)} сигналов")
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


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан")
    bot = Bot(settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    reminder = asyncio.create_task(_reminder_loop(bot))
    digest = asyncio.create_task(_digest_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminder.cancel()
        digest.cancel()


if __name__ == "__main__":
    asyncio.run(main())
