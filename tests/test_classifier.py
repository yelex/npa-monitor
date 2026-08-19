"""Тесты классификатора, PLAN.md Фаза 4: синтетические публикации по каждой ЖС
и каждому уровню приоритета, плюс негативные случаи и регион/даты."""
from __future__ import annotations

import datetime as dt

import pytest

from db.enums import EventType, Priority, Region, SignalCategory
from db.catalog import load_life_situations
from parser.classifier import Classification, classify, classification_to_signal_kwargs
from parser.models import Publication

LS = {ls.category: ls.keywords for ls in load_life_situations()}


def pub(title: str, url: str = "https://sfr.gov.ru/news/1", summary: str | None = None) -> Publication:
    return Publication(
        source_key="sfr_news",
        title=title,
        url=url,
        published_at=dt.datetime(2026, 8, 19, 6, 0),
        summary=summary,
    )


# --- Релевантность по ЖС -----------------------------------------------------


def test_veterans_relevant() -> None:
    c = classify(pub("Подписан закон о выплатах ветеранам боевых действий"), LS)
    assert c.relevant
    assert SignalCategory.VETERANS in c.categories
    assert c.priority is Priority.HIGH
    assert c.event_type is EventType.NEW_DOCUMENT


def test_disabled_relevant() -> None:
    c = classify(pub("Постановление:新的 пособие для инвалидов с детства", summary="утверждена мера поддержки"), LS)
    assert c.relevant
    assert SignalCategory.DISABLED in c.categories


def test_svo_relevant() -> None:
    c = classify(pub("Указ о мерах поддержки мобилизованных — вступает в силу с 01.09.2026"), LS)
    assert c.relevant
    assert SignalCategory.SVO in c.categories
    assert c.event_type is EventType.ENTRY_INTO_FORCE
    assert c.effective_from == dt.date(2026, 9, 1)


def test_no_ls_not_relevant() -> None:
    c = classify(pub("Постановление о субсидиях для сельхозпроизводителей"), LS)
    assert not c.relevant


def test_ls_without_markers_not_relevant() -> None:
    c = classify(pub("Интервью об инвалидах"), LS)  # ЖС есть, но нет мер и маркеров
    assert not c.relevant


def test_review_low_priority() -> None:
    c = classify(pub("Обзор мер поддержки инвалидов за год", summary="выплата"), LS)
    assert c.relevant
    assert c.priority is Priority.LOW
    assert c.event_type is EventType.REVIEW


# --- Приоритет ---------------------------------------------------------------


def test_priority_medium_when_region_undefined() -> None:
    c = classify(pub("Утверждены выплаты инвалидам", url="https://rg.ru/articles/1"), LS)
    assert c.relevant
    assert c.region is Region.UNDEFINED
    assert c.priority is Priority.MEDIUM


def test_priority_high_with_action_marker_and_url() -> None:
    c = classify(pub("Подписан закон о льготах детям участников СВО"), LS)
    assert c.relevant
    assert c.priority is Priority.HIGH


def test_priority_medium_no_action_marker() -> None:
    c = classify(pub("Пособие контрактникам: разъяснение министерства"), LS)
    assert c.relevant
    assert c.priority is Priority.MEDIUM


# --- Регион ------------------------------------------------------------------


def test_region_moscow_by_domain() -> None:
    c = classify(pub("Постановление о выплатах инвалидам", url="https://www.mos.ru/authority/documents/1"), LS)
    assert c.region is Region.MOSCOW


def test_region_rf_by_domain() -> None:
    c = classify(pub("Постановление о выплатах инвалидам"), LS)
    assert c.region is Region.RF


def test_region_moscow_by_text() -> None:
    c = classify(pub("Указание Правительства Москвы о пособиях инвалидам", url="https://rg.ru/a/2"), LS)
    assert c.region is Region.MOSCOW


# --- Тип события ---------------------------------------------------------------


def test_event_type_repeal() -> None:
    c = classify(pub("Закон о льготах ВБД признан утратившим силу"), LS)
    assert c.event_type is EventType.REPEAL


def test_event_type_amendment() -> None:
    c = classify(pub("Внесены изменения в закон о мерах поддержки инвалидов"), LS)
    assert c.event_type is EventType.AMENDMENT


# --- Сборка kwargs для create_signal ------------------------------------------


def test_kwargs_for_relevant() -> None:
    c = classify(pub("Подписан указ о выплатах ветеранам боевых действий"), LS)
    kwargs = classification_to_signal_kwargs(c, pub("x"))
    assert kwargs is not None
    assert kwargs["priority"] is Priority.HIGH
    assert kwargs["region"] is Region.RF


def test_kwargs_none_for_irrelevant() -> None:
    c = classify(pub("Спортивные новости"), LS)
    assert classification_to_signal_kwargs(c, pub("x")) is None
