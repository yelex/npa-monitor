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
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.main as bot_main
from bot.autoupdate_client import AutoUpdateAgentClient
from db.enums import EventType, Priority, Region, RejectionReason, SignalCategory, SignalStatus
from db.service import create_signal, transition_status


@pytest.fixture(autouse=True)
def _isolated_bot_state(tmp_path, monkeypatch):
    """Своя БД и allowlist на тест — без завязки на реальный .env/module-level кэши."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111")
    monkeypatch.setenv("AUTOUPDATE_SPOOL_DIR", str(tmp_path / "autoupdate_spool"))
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


# --- Флоу ссылка на НПА -> ЖС -> регион: FakeState + шаговые helper'ы ---
#
# docs/SPEC_analyst_confirm_ls_region.md: флоу теперь трёхшаговый (on_npa_link
# переводит в ask_categories, а не сразу в SENT_TO_AGENT), каждый следующий шаг читает
# данные, записанные предыдущим (`state.update_data(categories=..., npa_link=...)`).
# AsyncMock с фиксированным `get_data(return_value=...)` (как было в тестах до Фазы 10)
# для такой цепочки не годится — не отражает изменения между шагами. FakeState — минимальная
# реализация протокола FSMContext (get_data/update_data/set_state/clear) поверх обычного dict.


class FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self._data = dict(data or {})
        self.current_state = None
        self.get_data = AsyncMock(side_effect=self._get_data)
        self.update_data = AsyncMock(side_effect=self._update_data)
        self.set_state = AsyncMock(side_effect=self._set_state)
        self.clear = AsyncMock(side_effect=self._clear)

    async def _get_data(self) -> dict:
        return dict(self._data)

    async def _update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def _set_state(self, state) -> None:
        self.current_state = state

    async def _clear(self) -> None:
        self._data = {}
        self.current_state = None


async def _send_npa_link(sig_id: int, state: FakeState, text: str, *, user_id: int = 111) -> MagicMock:
    message = MagicMock()
    message.from_user.id = user_id
    message.text = text
    message.answer = AsyncMock()
    await bot_main.on_npa_link(message, state)
    return message


async def _toggle_category(sig_id: int, state: FakeState, code: str, *, user_id: int = 111) -> MagicMock:
    cb = MagicMock()
    cb.from_user.id = user_id
    cb.data = f"catc:{sig_id}:{code}"
    cb.message.edit_reply_markup = AsyncMock()
    cb.answer = AsyncMock()
    await bot_main.on_category_toggle(cb, state)
    return cb


async def _confirm_categories(sig_id: int, state: FakeState, *, user_id: int = 111) -> MagicMock:
    cb = MagicMock()
    cb.from_user.id = user_id
    cb.data = f"catc:{sig_id}:confirm"
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    await bot_main.on_category_toggle(cb, state)
    return cb


async def _confirm_region(sig_id: int, state: FakeState, code: str, *, user_id: int = 111) -> MagicMock:
    cb = MagicMock()
    cb.from_user.id = user_id
    cb.data = f"reg:{sig_id}:{code}"
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    await bot_main.on_region_button(cb, state)
    return cb


async def _search_region(sig_id: int, state: FakeState, query: str, *, user_id: int = 111) -> MagicMock:
    message = MagicMock()
    message.from_user.id = user_id
    message.text = query
    message.answer = AsyncMock()
    await bot_main.on_region_manual(message, state)
    return message


async def _run_full_npa_flow(
    sig_id: int, *, link_text: str = "skip", region_code: str = "rf", user_id: int = 111
) -> tuple[FakeState, MagicMock, MagicMock, MagicMock]:
    """Полный проход флоу: ссылка -> подтверждение ЖС (дефолтный предвыбор
    классификатора, без ручного toggle) -> подтверждение региона кнопкой. Для тестов,
    которым важен только конечный результат (SENT_TO_AGENT)."""
    state = FakeState({"sig_id": sig_id})
    message = await _send_npa_link(sig_id, state, link_text, user_id=user_id)
    cb_confirm = await _confirm_categories(sig_id, state, user_id=user_id)
    cb_region = await _confirm_region(sig_id, state, region_code, user_id=user_id)
    return state, message, cb_confirm, cb_region


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


def test_signal_card_escapes_html_special_characters() -> None:
    """`parse_mode=HTML` включён глобально (main()) — заголовок/ссылка приходят из
    реального веб-контента (листинги, Yandex Search) и могут содержать `&`/`<`/`>`
    (например, URL с query-параметрами `...?a=1&b=2`) — без экранирования Telegram
    не смог бы разобрать сообщение (`can't parse entities`)."""
    sig_id = _make_signal(
        title="Приказ №5 & <важно>",
        source_url="https://example.test/doc?a=1&b=2",
    )
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        card = bot_main.signal_card(signal, [])

    assert "<важно>" not in card
    assert "&lt;важно&gt;" in card
    assert "doc?a=1&amp;b=2" in card


def test_priority_filter_kb_marks_active_option() -> None:
    kb = bot_main.priority_filter_kb("pending", "high")
    buttons = kb.inline_keyboard[0]
    assert [b.callback_data for b in buttons] == [
        "filter:pending:all", "filter:pending:high", "filter:pending:medium", "filter:pending:low"
    ]
    assert buttons[1].text == "[🔴]"
    assert buttons[0].text == "Все"  # неактивная кнопка — без пометки


def test_apply_priority_filter_keeps_only_matching_priority() -> None:
    high_id = _make_signal(priority=Priority.HIGH)
    _make_signal(priority=Priority.LOW)
    factory = bot_main.get_session_factory()
    with factory() as session:
        signals = session.query(bot_main.Signal).all()
        filtered = bot_main.apply_priority_filter(signals, "high")
    assert [s.id for s in filtered] == [high_id]


def test_apply_priority_filter_all_returns_unchanged() -> None:
    _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        signals = session.query(bot_main.Signal).all()
        filtered = bot_main.apply_priority_filter(signals, "all")
    assert filtered == signals


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


async def test_on_signal_button_work_shows_alert_instead_of_crashing_when_already_in_progress() -> None:
    """Найдено вживую (после добавления второго эксперта в whitelist): двойное нажатие
    «Взять в работу» (или гонка двух экспертов) роняло `InvalidStatusTransition`
    необработанным — апдейт падал в логе, эксперт не получал вообще никакого ответа."""
    sig_id = _make_in_progress_signal()
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = f"sig:{sig_id}:work"
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()

    await bot_main.on_signal_button(cb, state)

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS  # статус не тронут


async def test_on_priority_filter_edits_header_and_sends_only_matching_priority() -> None:
    _make_signal(priority=Priority.HIGH, title="Высокий")
    _make_signal(priority=Priority.LOW, title="Низкий")
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = "filter:today:high"
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()

    await bot_main.on_priority_filter(cb)

    header_text = cb.message.edit_text.await_args.args[0]
    header_kwargs = cb.message.edit_text.await_args.kwargs
    assert "1" in header_text  # ровно один сигнал с приоритетом "высокий"
    assert header_kwargs["reply_markup"].inline_keyboard[0][1].text == "[🔴]"
    cb.message.answer.assert_awaited_once()
    assert "Высокий" in cb.message.answer.await_args.args[0]


async def test_on_priority_filter_ignores_unauthorized_user() -> None:
    cb = MagicMock()
    cb.from_user.id = 999
    cb.data = "filter:today:high"
    cb.answer = AsyncMock()

    await bot_main.on_priority_filter(cb)

    cb.answer.assert_awaited_once_with("Нет доступа", show_alert=True)


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


async def test_on_reject_reason_shows_alert_instead_of_crashing_when_already_rejected() -> None:
    sig_id = _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        from db.service import transition_status

        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.REJECTED, rejection_reason=RejectionReason.OTHER)
        session.commit()
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = f"rej:{sig_id}:r_dup"
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()

    await bot_main.on_reject_reason(cb, state)

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()  # не перезаписали текст карточки


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
    """'skip' проходит те же шаги, что и обычная ссылка: сначала подтверждение ЖС/
    региона (SPEC_analyst_confirm_ls_region.md), только потом SENT_TO_AGENT."""
    sig_id = _make_in_progress_signal()
    state = FakeState({"sig_id": sig_id})
    message = await _send_npa_link(sig_id, state, "skip")

    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS  # ждёт подтверждения ЖС/региона
    message.answer.assert_awaited_once()
    assert "ЖС" in message.answer.await_args.args[0]

    await _confirm_categories(sig_id, state)
    await _confirm_region(sig_id, state, "rf")

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
    state = FakeState({"sig_id": sig_id})

    await _send_npa_link(sig_id, state, "https://sfr.gov.ru/document/1")
    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS  # ждёт подтверждения ЖС/региона
    await _confirm_categories(sig_id, state)
    await _confirm_region(sig_id, state, "rf")

    assert _get_status(sig_id) == SignalStatus.SENT_TO_AGENT
    factory = bot_main.get_session_factory()
    with factory() as session:
        assert session.get(bot_main.Signal, sig_id).npa_link == "https://sfr.gov.ru/document/1"
    # AGENTS.md раздел 15 / docs/SPEC_autoupdate_agent_contract.md раздел 3.7: адаптер
    # обязан быть вызван с сигналом, итоговой ссылкой на НПА, discovery_url, ЖС и регионом.
    assert sent.call_count == 1
    call_signal, call_npa_url, call_discovery_url, call_categories, call_region = sent.call_args.args
    assert call_signal.id == sig_id
    assert call_npa_url == "https://sfr.gov.ru/document/1"
    assert call_discovery_url == "https://sfr.gov.ru/1"  # Signal.source_url из _make_signal
    assert call_categories == ["veterans"]
    assert call_region == "rf"


async def test_on_npa_link_accepts_unsupported_domain_on_trust_without_network_call(
    monkeypatch,
) -> None:
    """docs/SPEC_bot_npa_link_check.md: docs.cntd.ru размечен access=unsupported (сетевой
    доступ невозможен даже через прокси) — бот должен принимать такую ссылку на доверии,
    не пытаясь её проверить (иначе — 100%-й false negative, см. спеку).
    """
    sig_id = _make_in_progress_signal()
    fetch_mock = MagicMock(side_effect=AssertionError("fetch не должен вызываться для unsupported"))
    monkeypatch.setattr(bot_main, "fetch", fetch_mock)
    sent = MagicMock()
    monkeypatch.setattr(bot_main._autoupdate_client, "send", sent)
    state = FakeState({"sig_id": sig_id})

    await _send_npa_link(sig_id, state, "https://docs.cntd.ru/document/408415942")
    await _confirm_categories(sig_id, state)
    cb_region = await _confirm_region(sig_id, state, "rf")

    fetch_mock.assert_not_called()
    assert _get_status(sig_id) == SignalStatus.SENT_TO_AGENT
    assert "автопроверку" in cb_region.message.answer.await_args.args[0]
    assert sent.call_args.args[1] == "https://docs.cntd.ru/document/408415942"


async def test_on_npa_link_uses_ru_proxy_access_for_ru_proxy_domain(monkeypatch) -> None:
    """Раньше `access="direct"` был захардкожен — ссылки на ru_proxy-домены
    (kremlin.ru и т.п.) неизбежно проваливали бы проверку доступности вне RSNET.
    """
    sig_id = _make_in_progress_signal()
    fetch_mock = MagicMock(return_value=None)
    monkeypatch.setattr(bot_main, "fetch", fetch_mock)
    monkeypatch.setattr(bot_main._autoupdate_client, "send", MagicMock())
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"sig_id": sig_id})
    message = MagicMock()
    message.from_user.id = 111
    message.text = "https://kremlin.ru/acts/1"
    message.answer = AsyncMock()

    await bot_main.on_npa_link(message, state)

    fetch_mock.assert_called_once_with(
        "https://kremlin.ru/acts/1",
        access="ru_proxy",
        ru_proxy_url=bot_main.get_settings().ru_proxy_url,
    )


async def test_on_npa_link_shows_message_instead_of_crashing_when_already_sent_to_agent(
    monkeypatch,
) -> None:
    """on_npa_link больше не переводит статус сам (это финальный шаг после ЖС/региона,
    `_finish_npa_flow`) — гонка «уже отправлено» обнаруживается только там, на
    `transition_status` в SENT_TO_AGENT."""
    sig_id = _make_in_progress_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        from db.service import transition_status

        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.SENT_TO_AGENT)
        session.commit()
    monkeypatch.setattr(bot_main, "fetch", MagicMock(return_value=None))
    state = FakeState({"sig_id": sig_id})

    await _send_npa_link(sig_id, state, "https://sfr.gov.ru/document/1")
    await _confirm_categories(sig_id, state)
    cb_region = await _confirm_region(sig_id, state, "rf")

    assert "уже в статусе" in cb_region.message.answer.await_args.args[0]
    state.clear.assert_awaited_once()


# --- on_category_toggle: toggle ЖС, подтверждение пустого набора ---


async def test_on_category_toggle_toggles_selection_on_and_off() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.VETERANS])
    state = FakeState({"sig_id": sig_id})
    await _send_npa_link(sig_id, state, "skip")
    data = await state.get_data()
    assert data["categories"] == ["veterans"]  # предвыбор из классификатора

    await _toggle_category(sig_id, state, "veterans")  # снимаем предвыбор
    await _toggle_category(sig_id, state, "disabled")  # включаем другую ЖС

    data = await state.get_data()
    assert sorted(data["categories"]) == ["disabled"]


async def test_on_category_toggle_confirm_blocks_empty_selection() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.VETERANS])
    state = FakeState({"sig_id": sig_id})
    await _send_npa_link(sig_id, state, "skip")
    await _toggle_category(sig_id, state, "veterans")  # снимаем единственный предвыбор

    cb = await _confirm_categories(sig_id, state)

    cb.answer.assert_awaited_once_with("Выберите хотя бы одну ЖС", show_alert=True)
    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS  # дальше не продвинулись


# --- on_region_button / on_region_manual: выбор региона кнопкой и поиском по справочнику ---


async def test_on_region_button_selects_region_and_reaches_sent_to_agent() -> None:
    sig_id = _make_in_progress_signal()

    state, message, cb_confirm, cb_region = await _run_full_npa_flow(sig_id, region_code="moscow")

    assert _get_status(sig_id) == SignalStatus.SENT_TO_AGENT
    factory = bot_main.get_session_factory()
    with factory() as session:
        assert session.get(bot_main.Signal, sig_id).region == Region.MOSCOW
    assert "Передан агенту" in cb_region.message.answer.await_args.args[0]


def _region_search_fixture() -> tuple:
    """Фикстура с намеренно пересекающимися названиями («Москва» / «Московская
    область») — независимо от реального содержимого data/regions.yaml проверяет три
    исхода `find_region_matches`/`on_region_manual`: 0, 1, 2+ совпадений."""
    return (
        bot_main.RegionEntry(code="moscow", name="Москва", region=Region.MOSCOW, sources=()),
        bot_main.RegionEntry(code="moscow_obl", name="Московская область", region=Region.UNDEFINED, sources=()),
    )


def test_find_region_matches_zero_one_and_multiple(monkeypatch) -> None:
    monkeypatch.setattr(bot_main, "load_regions", _region_search_fixture)

    assert bot_main.find_region_matches("Атлантида") == []
    assert [r.code for r in bot_main.find_region_matches("Москва")] == ["moscow"]
    assert {r.code for r in bot_main.find_region_matches("моск")} == {"moscow", "moscow_obl"}


async def test_on_region_manual_zero_matches_asks_to_retry(monkeypatch) -> None:
    monkeypatch.setattr(bot_main, "load_regions", _region_search_fixture)
    sig_id = _make_in_progress_signal()
    state = FakeState({"sig_id": sig_id})
    await _send_npa_link(sig_id, state, "skip")
    await _confirm_categories(sig_id, state)
    await _confirm_region(sig_id, state, "other")

    message = await _search_region(sig_id, state, "Атлантида")

    assert "не найден" in message.answer.await_args.args[0]
    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS


async def test_on_region_manual_single_match_finishes_flow(monkeypatch) -> None:
    monkeypatch.setattr(bot_main, "load_regions", _region_search_fixture)
    sig_id = _make_in_progress_signal()
    state = FakeState({"sig_id": sig_id})
    await _send_npa_link(sig_id, state, "skip")
    await _confirm_categories(sig_id, state)
    await _confirm_region(sig_id, state, "other")

    message = await _search_region(sig_id, state, "Москва")

    assert _get_status(sig_id) == SignalStatus.SENT_TO_AGENT
    factory = bot_main.get_session_factory()
    with factory() as session:
        assert session.get(bot_main.Signal, sig_id).region == Region.MOSCOW


async def test_on_region_manual_multiple_matches_asks_to_narrow(monkeypatch) -> None:
    monkeypatch.setattr(bot_main, "load_regions", _region_search_fixture)
    sig_id = _make_in_progress_signal()
    state = FakeState({"sig_id": sig_id})
    await _send_npa_link(sig_id, state, "skip")
    await _confirm_categories(sig_id, state)
    await _confirm_region(sig_id, state, "other")

    message = await _search_region(sig_id, state, "моск")

    assert "уточните" in message.answer.await_args.args[0]
    assert "Москва" in message.answer.await_args.args[0]
    assert "Московская область" in message.answer.await_args.args[0]
    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS


# --- аудит расхождения классификатора с подтверждением аналитика (StatusHistory.reason) ---


async def test_finish_npa_flow_records_audit_reason_on_divergence() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.VETERANS], region=Region.RF)

    await _run_full_npa_flow(sig_id, region_code="moscow")  # аналитик поменял регион

    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        last = signal.history[-1]
        assert last.to_status == SignalStatus.SENT_TO_AGENT
        assert last.reason is not None
        assert "region=rf" in last.reason
        assert "confirmed=" in last.reason
        assert "region=moscow" in last.reason


async def test_finish_npa_flow_no_audit_reason_without_divergence() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.VETERANS], region=Region.RF)

    await _run_full_npa_flow(sig_id, region_code="rf")  # подтверждение = классификатор один в один

    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        last = signal.history[-1]
        assert last.to_status == SignalStatus.SENT_TO_AGENT
        assert last.reason is None


# --- docs/SPEC_autoupdate_agent_contract.md: контракт задачи, атомарность, ------
# идемпотентность, дозапись при старте, карточки по результатам --------------------


def _spool_dir() -> Path:
    return Path(bot_main.get_settings().autoupdate_spool_dir)


def test_autoupdate_client_send_writes_task_contract_with_real_enum_codes() -> None:
    """SPEC раздел 3.3: schema_version=1, signal_id отдельным полем (не парсим
    `task_id`), categories/region — реальные `.value` из `db/enums.py`."""
    sig_id = _make_signal(categories=[SignalCategory.VETERANS, SignalCategory.SVO], region=Region.MOSCOW)
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        client = AutoUpdateAgentClient()
        path = client.send(
            signal,
            "https://publication.pravo.gov.ru/document/1",
            signal.source_url,
            ["veterans", "svo"],
            "moscow",
        )

    assert path == _spool_dir() / "tasks" / f"sig-{sig_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    created_at = payload.pop("created_at")
    assert payload == {
        "schema_version": 1,
        "task_id": f"sig-{sig_id}",
        "signal_id": sig_id,
        "npa_url": "https://publication.pravo.gov.ru/document/1",
        "discovery_url": "https://sfr.gov.ru/1",
        "categories": ["veterans", "svo"],
        "region": "moscow",
    }
    dt.datetime.fromisoformat(created_at)  # не падает — валидный ISO 8601


def test_autoupdate_client_send_allows_null_npa_url_with_discovery_url() -> None:
    """SPEC раздел 3.3: `npa_url: null` допустим, если есть `discovery_url`."""
    sig_id = _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        path = AutoUpdateAgentClient().send(signal, None, signal.source_url, ["veterans"], "rf")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["npa_url"] is None
    assert payload["discovery_url"] == "https://sfr.gov.ru/1"


def test_autoupdate_client_send_writes_atomically_no_leftover_tmp_file() -> None:
    """SPEC раздел 3.5: tmp-файл + `os.rename` — после записи в каталоге задач должен
    остаться только итоговый файл, без временного `.tmp`."""
    sig_id = _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        AutoUpdateAgentClient().send(signal, "https://sfr.gov.ru/document/1", signal.source_url, [], "rf")

    files = sorted(p.name for p in (_spool_dir() / "tasks").iterdir())
    assert files == [f"sig-{sig_id}.json"]


def test_autoupdate_client_send_is_idempotent_overwrites_same_task_file() -> None:
    """SPEC раздел 3.3/3.7: `task_id = f"sig-{signal_id}"` — повторный `send()`
    перезаписывает тот же файл, не плодит дубликаты (используется сверкой при старте)."""
    sig_id = _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        client = AutoUpdateAgentClient()
        client.send(signal, "https://sfr.gov.ru/first", signal.source_url, ["veterans"], "rf")
        path = client.send(signal, "https://sfr.gov.ru/second", signal.source_url, ["svo"], "moscow")

    files = list((_spool_dir() / "tasks").iterdir())
    assert len(files) == 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["npa_url"] == "https://sfr.gov.ru/second"
    assert payload["region"] == "moscow"


async def test_finish_npa_flow_does_not_commit_when_task_write_fails(monkeypatch) -> None:
    """SPEC раздел 3.2 (ключевой фикс ревью): запись задачи ДО commit — если запись
    упала, переход не коммитится (сигнал остаётся «В работе»), аналитик видит ошибку."""
    sig_id = _make_in_progress_signal()
    monkeypatch.setattr(
        bot_main._autoupdate_client, "send", MagicMock(side_effect=OSError("disk full"))
    )

    state, message, cb_confirm, cb_region = await _run_full_npa_flow(sig_id)

    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS  # откат, не SENT_TO_AGENT
    assert "не удалось передать" in cb_region.message.answer.await_args.args[0].lower()
    assert not (_spool_dir() / "tasks" / f"sig-{sig_id}.json").exists()


async def test_reconcile_spool_tasks_writes_missing_task_for_sent_to_agent_signal() -> None:
    """SPEC раздел 3.2: сверка при старте — SENT_TO_AGENT без файла в spool -> дозапись."""
    sig_id = _make_signal(categories=[SignalCategory.DISABLED], region=Region.MOSCOW)
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.IN_PROGRESS)
        transition_status(session, signal, SignalStatus.SENT_TO_AGENT)
        session.commit()

    task_file = _spool_dir() / "tasks" / f"sig-{sig_id}.json"
    assert not task_file.exists()

    with factory() as session:
        written = bot_main._reconcile_spool_tasks(session, bot_main._autoupdate_client)

    assert written == 1
    payload = json.loads(task_file.read_text(encoding="utf-8"))
    assert payload["signal_id"] == sig_id
    assert payload["categories"] == ["disabled"]
    assert payload["region"] == "moscow"


async def test_reconcile_spool_tasks_skips_signal_with_existing_task_file() -> None:
    sig_id = _make_signal()
    factory = bot_main.get_session_factory()
    with factory() as session:
        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.IN_PROGRESS)
        transition_status(session, signal, SignalStatus.SENT_TO_AGENT)
        session.commit()
        bot_main._autoupdate_client.send(signal, None, signal.source_url, [], "rf")

    with factory() as session:
        written = bot_main._reconcile_spool_tasks(session, bot_main._autoupdate_client)

    assert written == 0


def _write_result(spool_dir: Path, name: str, payload: dict) -> Path:
    results = spool_dir / "results"
    results.mkdir(parents=True, exist_ok=True)
    path = results / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def test_scan_autoupdate_results_sends_card_and_archives_then_stays_silent() -> None:
    """SPEC раздел 3.6/4: фикстура `results/sig-<id>.json` -> карточка аналитику,
    перемещение в `results/.processed/`; повторный скан по тому же каталогу — тишина."""
    sig_id = _make_in_progress_signal()
    _write_result(
        _spool_dir(),
        f"sig-{sig_id}.json",
        {
            "schema_version": 1,
            "task_id": f"sig-{sig_id}",
            "signal_id": sig_id,
            "status": "done",
            "finished_at": "2026-08-25T01:40:00+00:00",
            "summary": "Найдено: изменение ежемесячной выплаты ВБД",
        },
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    sent_count = await bot_main.scan_autoupdate_results(bot)

    assert sent_count == 1
    bot.send_message.assert_awaited_once()
    card_text = bot.send_message.await_args.args[1]
    assert f"сигналу {sig_id}" in card_text
    assert "Найдено: изменение ежемесячной выплаты ВБД" in card_text
    assert not (_spool_dir() / "results" / f"sig-{sig_id}.json").exists()
    assert (_spool_dir() / "results" / ".processed" / f"sig-{sig_id}.json").exists()

    bot.send_message.reset_mock()
    sent_count_again = await bot_main.scan_autoupdate_results(bot)
    assert sent_count_again == 0
    bot.send_message.assert_not_awaited()


async def test_scan_autoupdate_results_ignores_unknown_signal_id_and_archives_it() -> None:
    _write_result(
        _spool_dir(),
        "sig-999999.json",
        {"schema_version": 1, "task_id": "sig-999999", "signal_id": 999999, "status": "error"},
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    sent_count = await bot_main.scan_autoupdate_results(bot)

    assert sent_count == 0
    bot.send_message.assert_not_awaited()
    assert (_spool_dir() / "results" / ".processed" / "sig-999999.json").exists()


async def test_cmd_today_ignores_unauthorized_user() -> None:
    message = MagicMock()
    message.from_user.id = 999  # не в ALLOWED_TELEGRAM_USER_IDS
    message.answer = AsyncMock()

    await bot_main.cmd_today(message)

    message.answer.assert_not_awaited()


# --- Регрессии: DetachedInstanceError на .categories после закрытия сессии ---


async def test_cmd_today_shows_only_header_until_filter_chosen() -> None:
    """UX-фикс, запрошенный пользователем вживую: `/today` раньше сразу выгружал все
    карточки отдельными сообщениями, даже без выбора фильтра — неудобно, когда
    сигналов десятки (после подключения Yandex Search, см. SPEC раздел 5). Теперь
    команда показывает только заголовок с кнопками; карточки — только по клику
    (см. `test_on_priority_filter_*` ниже)."""
    _make_signal(categories=[SignalCategory.VETERANS, SignalCategory.SVO])
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_today(message)

    assert message.answer.await_count == 1  # только заголовок, без карточек
    header_call = message.answer.await_args
    buttons = header_call.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons][:2] == ["filter:today:all", "filter:today:high"]


async def test_cmd_pending_shows_only_header_until_filter_chosen() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.DISABLED])
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_pending(message)

    assert _get_status(sig_id) == SignalStatus.IN_PROGRESS
    assert message.answer.await_count == 1


async def test_cmd_history_shows_only_header_until_filter_chosen() -> None:
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

    assert message.answer.await_count == 1


async def test_on_priority_filter_all_renders_signal_with_categories_without_crashing() -> None:
    """Регрессия (перенесена сюда после переноса рендеринга карточек из `/today` в
    кнопку «Все», см. выше): db.query(Signal) без selectinload(Signal.categories) —
    обращение к .categories после закрытия сессии падало с DetachedInstanceError."""
    _make_signal(categories=[SignalCategory.VETERANS, SignalCategory.SVO])
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = "filter:today:all"
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()

    await bot_main.on_priority_filter(cb)

    assert "ВБД" in cb.message.answer.await_args.args[0]


async def test_on_priority_filter_all_renders_pending_signal_with_categories() -> None:
    _make_in_progress_signal(categories=[SignalCategory.DISABLED])
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = "filter:pending:all"
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()

    await bot_main.on_priority_filter(cb)

    assert "Инвалиды" in cb.message.answer.await_args.args[0]


async def test_on_priority_filter_all_renders_history_signal_with_categories() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.SVO])
    factory = bot_main.get_session_factory()
    with factory() as session:
        from db.service import transition_status

        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.SENT_TO_AGENT)
        session.commit()
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = "filter:history:all"
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()

    await bot_main.on_priority_filter(cb)

    assert "СВО" in cb.message.answer.await_args.args[0]


# --- /sent: что передано агенту, с деталями подтверждения аналитика (Фаза 10.x) ---


async def test_cmd_sent_ignores_unauthorized_user() -> None:
    message = MagicMock()
    message.from_user.id = 999  # не в ALLOWED_TELEGRAM_USER_IDS
    message.answer = AsyncMock()

    await bot_main.cmd_sent(message)

    message.answer.assert_not_awaited()


async def test_cmd_sent_reports_no_signals_when_empty() -> None:
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_sent(message)

    message.answer.assert_awaited_once_with("Переданных агенту сигналов нет.")


async def test_cmd_sent_shows_card_with_confirmation_details(monkeypatch) -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.VETERANS], region=Region.RF)
    monkeypatch.setattr(bot_main, "fetch", MagicMock(return_value=None))
    monkeypatch.setattr(bot_main._autoupdate_client, "send", MagicMock())
    await _run_full_npa_flow(sig_id, link_text="https://sfr.gov.ru/document/1", region_code="rf")

    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_sent(message)

    assert message.answer.await_count == 2  # заголовок + одна карточка
    card = message.answer.await_args.args[0]
    assert f"🆔 <b>{sig_id}</b>" in card
    assert "ВБД" in card
    assert "Регион: РФ" in card
    assert "sfr.gov.ru/document/1" in card
    assert "Передал агенту: 111" in card
    assert "правки аналитика" not in card  # подтверждение = классификатор один в один


async def test_cmd_sent_shows_audit_line_on_analyst_divergence() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.VETERANS], region=Region.RF)
    await _run_full_npa_flow(sig_id, region_code="moscow")  # аналитик поменял регион

    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_sent(message)

    card = message.answer.await_args.args[0]
    assert "правки аналитика: classifier=" in card
    assert "region=rf" in card
    assert "confirmed=" in card
    assert "region=moscow" in card


async def test_cmd_sent_excludes_signals_still_in_progress() -> None:
    _make_in_progress_signal(categories=[SignalCategory.SVO])

    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_sent(message)

    message.answer.assert_awaited_once_with("Переданных агенту сигналов нет.")


async def test_cmd_sent_includes_completed_signals() -> None:
    sig_id = _make_in_progress_signal(categories=[SignalCategory.DISABLED])
    await _run_full_npa_flow(sig_id, region_code="rf")
    factory = bot_main.get_session_factory()
    with factory() as session:
        from db.service import transition_status

        signal = session.get(bot_main.Signal, sig_id)
        transition_status(session, signal, SignalStatus.COMPLETED)
        session.commit()

    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_sent(message)

    assert message.answer.await_count == 2
    card = message.answer.await_args.args[0]
    assert "Инвалиды" in card
    assert "Передал агенту: 111" in card  # дата SENT_TO_AGENT, а не последующего COMPLETED


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


async def test_cmd_start_answers_with_command_list() -> None:
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()

    await bot_main.cmd_start(message)

    text = message.answer.await_args.args[0]
    for command in ("/today", "/pending", "/history", "/sent", "/stats", "/reopen", "/complete"):
        assert command in text


def test_start_text_has_no_stray_angle_brackets() -> None:
    """Регрессия, найденная вживую: `parse_mode=HTML` включён глобально (main()), а
    START_TEXT содержал буквальный `/reopen <id>` — Telegram воспринял `<id>` как
    незнакомый HTML-тег и отклонил всё сообщение целиком
    (`TelegramBadRequest: can't parse entities: Unsupported start tag "id"`), /start
    падал у любого пользователя. START_TEXT — plain text, без намеренной HTML-разметки
    (в отличие от signal_card, где `<b>` — осознанные теги), поэтому в нём вообще не
    должно быть `<`."""
    assert "<" not in bot_main.START_TEXT


async def test_cmd_reopen_format_hint_has_no_stray_angle_brackets() -> None:
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()
    command = MagicMock()
    command.args = None

    await bot_main.cmd_reopen(message, command)

    assert "<" not in message.answer.await_args.args[0]


async def test_cmd_complete_format_hint_has_no_stray_angle_brackets() -> None:
    message = MagicMock()
    message.from_user.id = 111
    message.answer = AsyncMock()
    command = MagicMock()
    command.args = None

    await bot_main.cmd_complete(message, command)

    assert "<" not in message.answer.await_args.args[0]


def test_router_maps_each_command_name_to_matching_handler() -> None:
    """Регрессия: @router.message(Command("digest")) висел над `_digest_signals`
    (приватным синхронным helper'ом с сигнатурой (db: Session)), а не над
    `cmd_digest` — сама команда /digest в реальном Telegram-диспетчере не работала
    бы, хотя прямой вызов `cmd_digest(message)` в тестах (см. ниже) это не ловил,
    т.к. обходит router полностью."""
    from aiogram.filters import Command as CommandFilter

    seen = set()
    for handler in bot_main.router.message.handlers:
        for f in handler.filters:
            if isinstance(f.callback, CommandFilter):
                for command in f.callback.commands:
                    assert handler.callback.__name__ == f"cmd_{command}", (
                        f"/{command} зарегистрирован на {handler.callback.__name__}, "
                        f"ожидался cmd_{command}"
                    )
                    seen.add(command)

    assert seen == {
        "start", "today", "pending", "history", "sent", "stats", "reopen", "complete", "digest",
    }


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
