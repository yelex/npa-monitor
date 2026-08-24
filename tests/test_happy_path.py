"""Сквозной прогон happy path, AGENTS.md раздел 11, PLAN.md Фаза 6.

    1. Парсер обходит источник, находит релевантную публикацию → создаёт сигнал.
    2. Бот формирует утреннюю сводку → сигнал в ней.
    3. Эксперт: ✅ Взять в работу → бот просит ссылку на НПА.
    4. Эксперт отправляет ссылку → бот проверяет и передаёт «агенту» (статус
       «Передан агенту»).
    5. Эксперт проверяет результат агента → /complete → «Завершён».

Один и тот же файл БД на всём протяжении — не мок памяти, а реальный сквозной путь
через parser/orchestrator.py и bot/main.py вместе, единственное, что подменяется —
сетевые вызовы (сам листинг источника и проверка доступности ссылки на НПА).
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.main as bot_main
from db.enums import SignalCategory, SignalStatus
from db.models import Signal
from parser.classifier import Classifier
from parser.models import Publication
from parser.orchestrator import SourceSpec, process_source

NOW = dt.datetime(2026, 8, 20, 6, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_bot_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111")
    bot_main.get_settings.cache_clear()
    bot_main._session_factory = None
    yield
    bot_main.get_settings.cache_clear()
    bot_main._session_factory = None


async def test_happy_path_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # --- 1. Парсер: обход источника, создание сигнала ---
    publication = Publication(
        source_key="sfr.gov.ru/press_center/news",
        title="постановление ветеран боевых действий выплата новый",
        url="https://sfr.gov.ru/press_center/news/happy-path",
        published_at=NOW,
    )
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: [publication] if page == 1 else [])
    classifier = Classifier.load()

    factory = bot_main.get_session_factory()
    with factory() as session:
        result = process_source(session, classifier, spec, now=NOW)
        session.commit()

    assert result.ok is True
    assert result.new_signals == 1
    with factory() as session:
        signal = session.query(Signal).one()
        sig_id = signal.id
        assert signal.status == SignalStatus.NEW
        assert SignalCategory.VETERANS in [c.category for c in signal.categories]

    # --- 2. Бот: утренняя сводка содержит сигнал ---
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await bot_main.send_digest(bot)
    digest_texts = [call.args[1] if len(call.args) > 1 else "" for call in bot.send_message.await_args_list]
    assert any("постановление ветеран боевых действий" in text for text in digest_texts)

    # --- 3. Эксперт берёт сигнал в работу ---
    cb = MagicMock()
    cb.from_user.id = 111
    cb.data = f"sig:{sig_id}:work"
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()
    await bot_main.on_signal_button(cb, state)

    with factory() as session:
        assert session.get(Signal, sig_id).status == SignalStatus.IN_PROGRESS

    # --- 4. Эксперт присылает ссылку на НПА -> подтверждает ЖС -> подтверждает регион
    # -> статус "Передан агенту" (SPEC_analyst_confirm_ls_region.md, PLAN.md Фазы 10-11:
    # аналитик подтверждает ЖС/регион отдельными шагами перед передачей агенту) ---
    monkeypatch.setattr(bot_main, "fetch", MagicMock(return_value=None))  # ссылка "доступна"

    class _FakeState:
        """Мини-FSMContext: следующий шаг флоу читает данные, записанные предыдущим —
        AsyncMock с фиксированным get_data() для такой цепочки не годится."""

        def __init__(self, data: dict) -> None:
            self._data = dict(data)
            self.get_data = AsyncMock(side_effect=lambda: self._data.copy())
            self.update_data = AsyncMock(side_effect=lambda **kw: self._data.update(kw))
            self.set_state = AsyncMock()
            self.clear = AsyncMock()

    npa_state = _FakeState({"sig_id": sig_id})
    message = MagicMock()
    message.from_user.id = 111
    message.text = "https://sfr.gov.ru/document/happy-path-npa"
    message.answer = AsyncMock()
    await bot_main.on_npa_link(message, npa_state)

    with factory() as session:
        assert session.get(Signal, sig_id).status == SignalStatus.IN_PROGRESS  # ждёт ЖС/региона

    cb_confirm = MagicMock()
    cb_confirm.from_user.id = 111
    cb_confirm.data = f"catc:{sig_id}:confirm"
    cb_confirm.message.edit_text = AsyncMock()
    cb_confirm.answer = AsyncMock()
    await bot_main.on_category_toggle(cb_confirm, npa_state)

    cb_region = MagicMock()
    cb_region.from_user.id = 111
    cb_region.data = f"reg:{sig_id}:rf"
    cb_region.message.answer = AsyncMock()
    cb_region.answer = AsyncMock()
    await bot_main.on_region_button(cb_region, npa_state)

    with factory() as session:
        assert session.get(Signal, sig_id).status == SignalStatus.IN_PROGRESS  # ждёт типа сигнала

    # docs/SPEC_signal_type_measure_select.md: после региона — тип сигнала («новая
    # мера» — кратчайший путь до SENT_TO_AGENT, без шага выбора меры из базы).
    cb_signal_type = MagicMock()
    cb_signal_type.from_user.id = 111
    cb_signal_type.data = f"sigtype:{sig_id}:new"
    cb_signal_type.message.edit_text = AsyncMock()
    cb_signal_type.message.answer = AsyncMock()
    cb_signal_type.answer = AsyncMock()
    await bot_main.on_signal_type(cb_signal_type, npa_state)

    with factory() as session:
        signal = session.get(Signal, sig_id)
        assert signal.status == SignalStatus.SENT_TO_AGENT
        assert signal.npa_link == "https://sfr.gov.ru/document/happy-path-npa"
        assert signal.signal_type.value == "new"
        assert signal.measure_id is None

    # --- 5. Эксперт проверил результат агента → /complete → «Завершён» ---
    complete_message = MagicMock()
    complete_message.from_user.id = 111
    complete_message.answer = AsyncMock()
    complete_command = MagicMock()
    complete_command.args = str(sig_id)
    await bot_main.cmd_complete(complete_message, complete_command)

    with factory() as session:
        signal = session.get(Signal, sig_id)
        assert signal.status == SignalStatus.COMPLETED
        # AGENTS.md раздел 6: полная история переходов сохранена для аудита.
        to_statuses = [h.to_status for h in signal.history]
        assert to_statuses == [
            SignalStatus.NEW,
            SignalStatus.IN_PROGRESS,
            SignalStatus.SENT_TO_AGENT,
            SignalStatus.COMPLETED,
        ]

    # --- Завершённый сигнал уходит в /history, не в /today ---
    # /history теперь показывает только заголовок с кнопками фильтра (карточки — по
    # клику, см. bot/main.py::_send_list) — сама карточка проверяется через клик
    # «Все» (on_priority_filter), а не сразу в ответе команды.
    history_message = MagicMock()
    history_message.from_user.id = 111
    history_message.answer = AsyncMock()
    await bot_main.cmd_history(history_message)
    assert history_message.answer.await_count == 1  # только заголовок

    history_cb = MagicMock()
    history_cb.from_user.id = 111
    history_cb.data = "filter:history:all"
    history_cb.message.edit_text = AsyncMock()
    history_cb.message.answer = AsyncMock()
    history_cb.answer = AsyncMock()
    await bot_main.on_priority_filter(history_cb)
    assert f"🆔 <b>{sig_id}</b>" in history_cb.message.answer.await_args_list[0].args[0]

    today_message = MagicMock()
    today_message.from_user.id = 111
    today_message.answer = AsyncMock()
    await bot_main.cmd_today(today_message)
    today_message.answer.assert_awaited_once_with("Сигналы за сегодня: пусто")
