"""Тесты parser/ru_stem.py — три случая ниже найдены вживую 2026-08-20 (см. докстринг
модуля): реальные публикации sfr.gov.ru, пропущенные точным совпадением подстроки."""
from __future__ import annotations

from parser.ru_stem import contains_keyword, find_matches


def test_exact_form_still_matches() -> None:
    """Расширение строгое — точная форма всегда продолжает матчиться."""
    assert contains_keyword("получил компенсация вчера", ("компенсация",)) is True
    assert contains_keyword("постановление о мера поддержки", ("мера поддержки",)) is True


def test_real_case_compensaciyu_matches_compensaciya() -> None:
    text = "более 15 тысяч людей с инвалидностью получили компенсацию по осаго".lower()
    assert contains_keyword(text, ("компенсация",)) is True


def test_real_case_genitive_plural_mer_podderzhki_matches() -> None:
    text = "могут получить в социальном фонде около 15 различных мер поддержки".lower()
    assert contains_keyword(text, ("мера поддержки",)) is True


def test_real_case_veteran_declined_forms_match() -> None:
    text = "уволенные ветераны боевых действий с инвалидностью".lower()
    assert contains_keyword(text, ("ветеран боевых действий",)) is True


def test_does_not_match_mid_word() -> None:
    """Граница слова (\\b) — стем не должен матчиться посреди другого слова: "плата"
    даёт стем "плат", который является подстрокой "зарплата", но не с начала слова."""
    assert contains_keyword("хорошая зарплата у сотрудников", ("плата",)) is False
    assert contains_keyword("положена доплата за выслугу", ("плата",)) is False


def test_short_word_not_overtrimmed() -> None:
    """Слова <=3 букв не обрезаются вовсе (риск слишком общего стема)."""
    assert contains_keyword("вбд подтверждён", ("вбд",)) is True


def test_words_must_stay_adjacent_and_ordered() -> None:
    """В отличие от отклонённой эвристики phase4-classifier — слова фразы должны идти
    подряд в том же порядке, не только оба присутствовать где-то в тексте."""
    assert contains_keyword("поддержки мера тут нет подряд", ("мера поддержки",)) is False
    assert contains_keyword("мера где-то тут поддержки далеко", ("мера поддержки",)) is False


def test_unrelated_text_does_not_match() -> None:
    assert contains_keyword("сегодня хорошая погода, дует ветер", ("ветеран боевых действий",)) is False


def test_dobrovolec_does_not_match_unrelated_dobrovolny() -> None:
    """Регрессия, найдена вживую 2026-08-20: обрезка «доброволец» до «добровол»
    матчилась на «добровольное страхование»/«добровольного пенсионного страхования»
    — частые фразы, не связанные с участниками СВО. См. докстринг модуля."""
    assert contains_keyword("участники эксперимента по добровольному страхованию", ("доброволец",)) is False
    assert (
        contains_keyword("вопросы добровольного пенсионного страхования самозанятых", ("доброволец",))
        is False
    )


def test_dobrovolec_exact_form_still_matches() -> None:
    assert contains_keyword("доброволец подписал контракт", ("доброволец",)) is True


def test_novyi_matches_declined_forms() -> None:
    """PLAN.md Фаза 9 п.5 / docs/SPEC_priority_substance_markers.md: стандартная
    обрезка (5 букв -> минус 1) давала стем «новы», не покрывающий «новую»/«новое»/
    «новых» — реальный пример из дампа пилота («…ввели новую меру поддержки…»)."""
    assert contains_keyword("в регионе ввели новую меру поддержки", ("новый",)) is True
    assert contains_keyword("подписано новое постановление", ("новый",)) is True
    assert contains_keyword("утверждены новые правила", ("новый",)) is True
    assert contains_keyword("новый указ подписан", ("новый",)) is True


def test_invalid_does_not_match_unrelated_invalyuta() -> None:
    """PLAN.md Фаза 9 п.6 / docs/SPEC_classifier_false_positives.md: стандартная
    обрезка (7 букв -> минус 2) давала стем «инвал», матчащийся на «инвалюта»/
    «инвалютный» — финансовый термин, не связанный с инвалидностью."""
    assert contains_keyword("золотовалютные и инвалютные резервы страны выросли", ("инвалид",)) is False
    assert contains_keyword("инвалютные операции банков ограничены", ("инвалид",)) is False


def test_invalid_declined_forms_still_match() -> None:
    assert contains_keyword("инвалиду назначена выплата", ("инвалид",)) is True
    assert contains_keyword("оформление инвалидности заняло месяц", ("инвалид",)) is True
    assert contains_keyword("льготы для инвалидов расширены", ("инвалид",)) is True


def test_kontraktnik_does_not_match_unrelated_kontraktny() -> None:
    """PLAN.md Фаза 9 п.6 / docs/SPEC_classifier_false_positives.md: стандартная
    обрезка (11 букв -> минус 2) давала стем «контрактн», матчащийся на прилагательное
    «контрактный»/«контрактная» (юридический/закупочный смысл), не про
    контрактников-военнослужащих."""
    assert contains_keyword("квоты распределяются на контрактной основе", ("контрактник",)) is False
    assert contains_keyword("система контрактных отношений в отрасли", ("контрактник",)) is False


def test_kontraktnik_declined_forms_still_match() -> None:
    assert contains_keyword("контрактнику выплатили денежное довольствие", ("контрактник",)) is True
    assert contains_keyword("контрактников призвали для участия в СВО", ("контрактник",)) is True


def test_vyplata_does_not_match_unrelated_vyplakat() -> None:
    """PLAN.md Фаза 9 п.6 / docs/SPEC_classifier_false_positives.md: стандартная
    обрезка (7 букв -> минус 2) давала стем «выпла», матчащийся на «выплакать» —
    не связанный по смыслу глагол."""
    assert contains_keyword("она смогла выплакать всю боль после потери", ("выплата",)) is False


def test_vyplata_declined_forms_still_match() -> None:
    assert contains_keyword("ежемесячная выплата ветеранам увеличена", ("выплата",)) is True
    assert contains_keyword("единовременную выплату получат участники СВО", ("выплата",)) is True
    assert contains_keyword("социальные выплаты индексированы", ("выплата",)) is True


def test_find_matches_returns_matched_keywords() -> None:
    text = "получили компенсацию и новую субсидию"
    assert find_matches(text, ("компенсация", "субсидия", "льгота")) == ("компенсация", "субсидия")


def test_find_matches_empty_when_nothing_matches() -> None:
    assert find_matches("ничего не подходит", ("компенсация", "льгота")) == ()
