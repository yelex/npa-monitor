"""Тесты parser/hybrid_classifier.py (Stage B), docs/SPEC_hybrid_classifier.md.

Реальный ONNX/BM25-пайплайн не мокается — артефакты `parser/models/rubert-tiny2/` лежат
в репозитории, инференс короткого заголовка на CPU занимает единицы мс, гонять его в
юнит-тестах дёшево (тот же подход, что `tests/test_ru_stem.py` для Stage A: тестируем
реальный алгоритм, а не идеализированную заглушку).
"""
from __future__ import annotations

import parser.hybrid_classifier as hybrid_classifier
from db.enums import SignalCategory
from parser.hybrid_classifier import classify_hybrid

# Заголовок в духе кейса 412-П Камчатки (AGENTS.md раздел 16 п.13/14,
# docs/SPEC_hybrid_classifier.md, /tmp/claude_hybrid_classifier_plan.md) — канцелярский
# оборот СВО без опорного ЖС-слова из data/life_situations.yaml. Дословную фразу «в
# связи с проведением специальной военной операции» из реального инцидента уже добавили
# в life_situations.yaml (коммит 7f15844) — Stage A теперь ловит её сам, поэтому здесь
# используется другой канцеляризм того же класса риска («принимающим участие в
# специальной военной операции» без слова «участник» вплотную рядом — Stage A требует
# смежности слов, см. parser/ru_stem.py), чтобы тест продолжал проверять именно Stage B.
KAMCHATKA_STYLE_TITLE = (
    "Постановление Правительства Камчатского края № 412-П о дополнительных выплатах "
    "отдельным категориям граждан, принимающим участие в специальной военной операции"
)

# Реальный ложный кейс из AGENTS.md раздел 16 п.15 (RSS rg.ru) — контрольный пример
# заведомо нерелевантного текста для приёмки Stage B (спека, «Калибровка и приёмка»,
# выборка 3).
UNRELATED_TITLE = "пятилетний план развития рыболовства Китая"


def test_kamchatka_style_title_accepted_as_svo() -> None:
    decision = classify_hybrid(KAMCHATKA_STYLE_TITLE)
    assert decision is not None
    assert decision.accepted is True
    assert decision.category == SignalCategory.SVO
    assert decision.anchor
    assert (
        decision.cos_score >= hybrid_classifier.DEFAULT_T_COS
        or decision.bm25_score >= hybrid_classifier.DEFAULT_T_BM25
    )


def test_unrelated_topic_is_rejected_as_near_miss() -> None:
    decision = classify_hybrid(UNRELATED_TITLE)
    assert decision is not None
    assert decision.accepted is False
    # near-miss не отбрасывается целиком — категория-топ и оба скора остаются в решении
    # для trace-лога (спека, п.7 — «Near-miss... в trace-лог для аудита якорей»).
    assert decision.category in set(SignalCategory)
    assert decision.cos_score < hybrid_classifier.DEFAULT_T_COS
    assert decision.bm25_score < hybrid_classifier.DEFAULT_T_BM25


def test_ranking_covers_all_categories_sorted_by_rrf_desc() -> None:
    decision = classify_hybrid(KAMCHATKA_STYLE_TITLE)
    assert decision is not None
    assert {score.category for score in decision.ranking} == set(SignalCategory)
    rrf_values = [score.rrf for score in decision.ranking]
    assert rrf_values == sorted(rrf_values, reverse=True)
    assert decision.ranking[0].category == decision.category


def test_returns_none_when_stage_b_context_unavailable(monkeypatch) -> None:
    """Fallback (спека, п.8): нет весов модели -> Stage B недоступен, `None` вместо
    исключения — вызывающий код (`parser/classifier.py`) должен деградировать тихо."""
    monkeypatch.setattr(hybrid_classifier, "MODEL_DIR", hybrid_classifier.MODEL_DIR / "does-not-exist")
    hybrid_classifier.reset_cache()
    try:
        assert classify_hybrid(KAMCHATKA_STYLE_TITLE) is None
    finally:
        # следующий вызов в других тестах пересоберёт кэш с реальным MODEL_DIR
        hybrid_classifier.reset_cache()


def test_context_unavailable_warns_only_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(hybrid_classifier, "MODEL_DIR", hybrid_classifier.MODEL_DIR / "does-not-exist")
    hybrid_classifier.reset_cache()
    try:
        with caplog.at_level("WARNING", logger="parser.hybrid_classifier"):
            classify_hybrid(KAMCHATKA_STYLE_TITLE)
            classify_hybrid(UNRELATED_TITLE)
        warnings = [r for r in caplog.records if "Stage B" in r.message]
        assert len(warnings) == 1
    finally:
        hybrid_classifier.reset_cache()
