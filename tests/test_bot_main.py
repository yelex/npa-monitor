"""Тесты bot/main.py, PLAN.md Фаза 5.

Хендлеры вызываются напрямую (без aiogram Dispatcher/polling) с моками
Message/CallbackQuery/FSMContext — стандартный подход для юнит-тестов
aiogram-хендлеров, не требует поднимать реальный Bot.

Несколько тестов здесь — регрессии на баги, найденные при слиянии bot/main.py из
ветки phase4-classifier (см. коммиты слияния): make_session_factory() без engine,
reason= вместо rejection_reason=, naive datetime.now() против tz-aware UTCDateTime,
отсутствующие переходы Новый->Отложен/Отложен->Отклонён в db/service.py.
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.main as bot_main
from db.enums import EventType, Priority, Region, RejectionReason, SignalCategory, SignalStatus
from db.service import create_signal


@pytest.fixture(autouse=True)
def _isolated_bot_state(tmp_path, monkeypatch):
    """Своя БД и allowlist на тест — без завязки на реальный .env/module-level кэши."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111")
    bot_main.get_settings.cache_clear()
    bot_main._session_factory = None
    yield
    bot_main.get_settings.cache_clear()
    bot_main._session_factory = None


def _make_signal(**overrides) -> int:
    factory = bot_main.get_session_factory()
    with factory() as session:
        kwargs = dict(
            event_type=EventType.NEW_DOCUMENT,
            priority=Priority.HIGH,
            source_url="https://sfr.gov.ru/1",
            categories=[SignalCategory.VETERANS],
            region=Region.RF,
            title="Тестовый сигнал",
        )
        kwargs.update(overrides)
        signal = create_signal(session, **kwargs)
        session.commit()
        return signal.id


def _get_status(sig_id: int) -> SignalStatus:
    factory = bot_main.get_session_factory()
    with factory() as session:
        return session.get(bot_main.Signal, sig_id).status


def _make_in_progress_signal(**overrides) -> int:
    """on_npa_link по реальному флоу вызывается только после ✅ Взять в работу —
    сигнал уже в статусе «В работе» (иначе transition_status в SENT_TO_AGENT упал бы
    с InvalidStatusTransition: NEW -> SENT_TO_AGENT не разрешён)."""
    from db.service import transition_status

    sig_id = _make_signal(**overrides)
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.IN_PROGRESS)
        session.commit()
    return sig_id


# --- Чистые функции ---


def test_is_allowed() -> None:
    assert bot_main.is_allowed(111) is True
    assert bot_main.is_allowed(999) is False


def test_signal_card_renders_expected_fields() -> None:
    sig_id = _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        card = bot_main.signal_card(signal, ["veterans"])

    assert "Тестовый сигнал" in card
    assert "ВБД" in card
    assert "🔴 Высокий" in card
    assert "sfr.gov.ru/1" in card


def test_signal_kb_has_three_buttons_with_expected_callback_data() -> None:
    kb = bot_main.signal_kb(42)
    buttons = kb.inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["sig:42:work", "sig:42:reject", "sig:42:later"]


def test_reject_kb_has_four_reason_buttons() -> None:
    kb = bot_main.reject_kb(7)
    all_buttons = [b for row in kb.inline_keyboard for b in row]
    assert len(all_buttons) == 4
    assert all(b.callback_data.startswith("rej:7:") for b in all_buttons)


# --- Регрессии: переходы статусов ---


async def test_on_signal_button_later_postpones_new_signal() -> None:
    """Регрессия: NEW -> POSTPONED не было в ALLOWED_TRANSITIONS до фикса db/service.py."""
    sig_id = _make_signal()
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = f"sig:{sig_id}:later"
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()

    await bot_main.on_signal_button(cb, state)

    assert _get_status(sig_id) == SignalStatus.POSTPONED


async def test_on_signal_button_work_starts_npa_link_flow() -> None:
    sig_id = _make_signal()
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = f"sig:{sig_id}:work"
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()

    await bot_main.on_signal_button(cb, state)

    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS
    state.set_state.assert_awaited_once_with(bot_main.NpaFlow.ask_npa_link)


async def test_on_reject_reason_sets_rejection_reason_field() -> None:
    """Регрессия: раньше передавался `reason=`, а не `rejection_reason=` —
    transition_status падал с ValueError «требует причины»."""
    sig_id = _make_signal()
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = f"rej:{sig_id}:r_dup"
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()

    await bot_main.on_reject_reason(cb, state)

    assert _get_status(sig_id) == SignalStatus.REJECTED
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        assert signal.rejection_reason == RejectionReason.DUPLICATE


async def test_postponed_signal_can_be_rejected_via_reject_flow() -> None:
    """Регрессия: POSTPONED -> REJECTED не было в ALLOWED_TRANSITIONS до фикса."""
    sig_id = _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        from db.service import transition_status

        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.POSTPONED)
        session.commit()

    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = f"rej:{sig_id}:r_other"
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()

    await bot_main.on_reject_reason(cb, state)

    assert _get_status(sig_id) == SignalStatus.REJECTED


# --- Регрессии: tz-aware datetime сравнения ---


async def test_remind_stale_does_not_crash_on_tz_comparison() -> None:
    """Регрессия: naive datetime.now() против tz-aware UTCDateTime поднимал ValueError."""
    _make_signal()
    bot = AsyncMock()

    await bot_main.remind_stale(bot)  # не должно поднимать исключение


async def test_remind_stale_reminds_for_signal_stuck_over_3_days_in_progress() -> None:
    """AGENTS.md раздел 12: сигнал >3 дней в «В работе» -> напоминание."""
    sig_id = _make_in_progress_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        signal.updated_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4)
        session.commit()
    bot = AsyncMock()

    await bot_main.remind_stale(bot)

    bot.send_message.assert_awaited_once()
    uid, text = bot.send_message.await_args.args
    assert uid == 111
    assert str(sig_id) in text


async def test_remind_stale_skips_signal_updated_recently() -> None:
    _make_in_progress_signal()  # updated_at ~ сейчас, порог не пройден

    bot = AsyncMock()
    await bot_main.remind_stale(bot)

    bot.send_message.assert_not_awaited()


async def test_cmd_stats_reports_counts_by_status_for_real_signals() -> None:
    _make_signal()  # статус «Новый»
    _make_in_progress_signal()
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_stats(message)

    text = message.answer.await_args.args[0]
    assert "2 сигнал" in text
    assert "Новый: 1" in text
    assert "В работе: 1" in text


# --- /reopen и /complete ---


async def test_cmd_reopen_returns_rejected_signal_to_new() -> None:
    sig_id = _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        from db.enums import RejectionReason
        from db.service import transition_status

        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.REJECTED, rejection_reason=RejectionReason.OTHER)
        session.commit()

    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()
    command = MagicMock()
    command.args = str(sig_id)

    await bot_main.cmd_reopen(message, command)

    assert _get_status(sig_id) == SignalStatus.NEW


async def test_cmd_reopen_rejects_non_numeric_args() -> None:
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()
    command = MagicMock()
    command.args = "abc"

    await bot_main.cmd_reopen(message, command)

    assert "Формат" in message.answer.await_args.args[0]


async def test_cmd_complete_transitions_sent_to_agent_to_completed() -> None:
    sig_id = _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        from db.service import transition_status

        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.IN_PROGRESS)
        transition_status(session, signal, SignalStatus.SENT_TO_AGENT)
        session.commit()

    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()
    command = MagicMock()
    command.args = str(sig_id)

    await bot_main.cmd_complete(message, command)

    assert _get_status(sig_id) == SignalStatus.COMPLETED


async def test_cmd_complete_rejects_invalid_transition_from_new() -> None:
    """Новый -> Завершён не разрешён — команда должна сообщить об ошибке, не падать."""
    sig_id = _make_signal()
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()
    command = MagicMock()
    command.args = str(sig_id)

    await bot_main.cmd_complete(message, command)

    assert _get_status(sig_id) == SignalStatus.NEW
    assert "Нельзя" in message.answer.await_args.args[0]


# --- Доступ ---


# --- on_npa_link: белый список домена + проверка доступности ---


async def test_on_npa_link_skip_transitions_without_touching_link(monkeypatch) -> None:
    sig_id = _make_in_progress_signal()
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"sig_id": sig_id})
    message = MagicMock()
    message.from_user.id = 111
    message.text = "skip"
    message.answer = AsyncMock()

    await bot_main.on_npa_link(message, state)

    assert _get_status(sig_id) == SignalStatus.SENT_TO_AGENT
    factory = bot_main.get_session_factory()
    with factory() as session:
        assert session.get(bot_main.Signal, sig_id).npa_link is None


async def test_on_npa_link_rejects_non_whitelisted_domain() -> None:
    sig_id = _make_in_progress_signal()
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"sig_id": sig_id})
    message = MagicMock()
    message.from_user.id = 111
    message.text = "https://evil.example.com/npa"
    message.answer = AsyncMock()

    await bot_main.on_npa_link(message, state)

    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS  # переход не произошёл
    message.answer.assert_awaited_once()
    assert "белом списке" in message.answer.await_args.args[0]


async def test_on_npa_link_rejects_unreachable_link(monkeypatch) -> None:
    sig_id = _make_in_progress_signal()
    monkeypatch.setattr(
        bot_main,
        "fetch",
        MagicMock(side_effect=bot_main.SourceUnavailable(url="x", access="direct", last_error=Exception())),
    )
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"sig_id": sig_id})
    message = MagicMock()
    message.from_user.id = 111
    message.text = "https://sfr.gov.ru/document/1"
    message.answer = AsyncMock()

    await bot_main.on_npa_link(message, state)

    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS
    assert "недоступна" in message.answer.await_args.args[0]


async def test_on_npa_link_accepts_reachable_whitelisted_link(monkeypatch) -> None:
    sig_id = _make_in_progress_signal()
    monkeypatch.setattr(bot_main, "fetch", MagicMock(return_value=None))
    sent = MagicMock()
    monkeypatch.setattr(bot_main._autoupdate_client, "send", sent)
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"sig_id": sig_id})
    message = MagicMock()
    message.from_user.id = 111
    message.text = "https://sfr.gov.ru/document/1"
    message.answer = AsyncMock()

    await bot_main.on_npa_link(message, state)

    assert _get_status(sig_id) == SignalStatus.SENT_TO_AGENT
    factory = bot_main.get_session_factory()
    with factory() as session:
        assert session.get(bot_main.Signal, sig_id).npa_link == "https://sfr.gov.ru/document/1"
    # AGENTS.md раздел 15: «бот передаёт ссылку и measureId агенту через адаптер» —
    # адаптер (пусть и заглушка) обязан быть вызван с итоговой ссылкой и measure_id.
    sent.assert_called_once_with("https://sfr.gov.ru/document/1", None)


async def test_cmd_today_ignores_unauthorized_user() -> None:
    message = MagicMock()
    message.from_user.id = 999  # не в ALLOWED_TELEGRAM_USER_IDS
    message.answer = AsyncMock()

    await bot_main.cmd_today(message)

    message.answer.assert_not_awaited()


# --- Регрессии: DetachedInstanceError на .categories после закрытия сессии ---


async def test_cmd_today_renders_signal_with_categories_without_crashing() -> None:
    """Регрессия: db.query(Signal) без selectinload(Signal.categories) — обращение к
    .categories после закрытия сессии (_send_list) падало с DetachedInstanceError."""
    _make_signal(categories=[SignalCategory.VETERANS, SignalCategory.SVO])
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_today(message)

    assert message.answer.await_count == 2  # заголовок + карточка
    assert "ВБД" in message.answer.await_args.args[0]


async def test_cmd_pending_renders_signal_with_categories_without_crashing() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.DISABLED])
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_pending(message)

    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS
    assert "Инвалиды" in message.answer.await_args.args[0]


async def test_cmd_history_renders_signal_with_categories_without_crashing() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.SVO])
    factory = bot_main.get_session_factory()
    with factory() as session:
        from db.service import transition_status

        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.SENT_TO_AGENT)
        session.commit()
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_history(message)

    assert "СВО" in message.answer.await_args.args[0]


# --- Утренняя сводка (рассылка, PLAN.md Фаза 6) ---


def test_seconds_until_next_same_day() -> None:
    now = bot_main.dt.datetime(2026, 8, 20, 6, 0)  # раньше 08:00
    assert bot_main._seconds_until_next(8, now=now) == 2 * 3600


def test_seconds_until_next_rolls_over_to_tomorrow() -> None:
    now = bot_main.dt.datetime(2026, 8, 20, 9, 0)  # уже позже 08:00
    seconds = bot_main._seconds_until_next(8, now=now)
    assert seconds == 23 * 3600  # 08:00 следующего дня


def test_seconds_until_next_exactly_at_target_rolls_over() -> None:
    now = bot_main.dt.datetime(2026, 8, 20, 8, 0)
    seconds = bot_main._seconds_until_next(8, now=now)
    assert seconds == 24 * 3600


async def test_send_digest_reports_no_signals_when_empty() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await bot_main.send_digest(bot)

    bot.send_message.assert_awaited_once_with(111, "Новых сигналов нет.")


async def test_send_digest_sends_cards_sorted_by_priority() -> None:
    high_id = _make_signal(priority=Priority.HIGH, title="Высокий")
    low_id = _make_signal(priority=Priority.LOW, title="Низкий")
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await bot_main.send_digest(bot)

    # первый вызов — заголовок сводки, затем карточки по возрастанию PRIORITY_ORDER
    calls = bot.send_message.await_args_list
    assert calls[0].args == (111, "📬 Утренняя сводка: 2 сигналов")
    assert "Высокий" in calls[1].args[1]
    assert "Низкий" in calls[2].args[1]
    assert calls[1].kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"sig:{high_id}:work"
    assert calls[2].kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"sig:{low_id}:work"


async def test_send_digest_excludes_signals_in_progress() -> None:
    """Раздел 10 AGENTS.md: сводка — «Новый»/«Отложен», не «В работе»."""
    sig_id = _make_in_progress_signal()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await bot_main.send_digest(bot)

    bot.send_message.assert_awaited_once_with(111, "Новых сигналов нет.")
    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS  # не тронут


async def test_cmd_digest_reports_no_signals_when_empty() -> None:
    """AGENTS.md раздел 9: «Нет новых публикаций — бот отправляет «Новых сигналов нет»»."""
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_digest(message)

    message.answer.assert_awaited_once_with("Новых сигналов нет.")


async def test_postponed_signal_appears_in_next_digest() -> None:
    """AGENTS.md раздел 9: «Эксперт нажал «Позже» — сигнал появится в следующей сводке»."""
    sig_id = _make_signal()
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = f"sig:{sig_id}:later"
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    await bot_main.on_signal_button(cb, AsyncMock())
    assert _get_status(sig_id) == SignalStatus.POSTPONED

    bot = MagicMock()
    bot.send_message = AsyncMock()
    await bot_main.send_digest(bot)

    assert bot.send_message.await_args_list[0].args == (111, "📬 Утренняя сводка: 1 сигналов")
