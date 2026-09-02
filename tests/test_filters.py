"""Тесты parser/filters.py, PLAN.md Фаза 3."""
from __future__ import annotations

from parser.filters import (
    is_domain_whitelisted,
    is_excluded_path,
    is_news_activity_noise,
    is_text_content,
)

WHITELIST = {"kremlin.ru", "sfr.gov.ru", "mos.ru"}


def test_is_domain_whitelisted_exact_match() -> None:
    assert is_domain_whitelisted("http://kremlin.ru/acts/news/80518", WHITELIST) is True


def test_is_domain_whitelisted_subdomain_matches() -> None:
    assert is_domain_whitelisted("https://www.mos.ru/authority/documents/doc/1/", WHITELIST) is True
    assert is_domain_whitelisted("https://msupport.dszn.ru/news", WHITELIST) is False


def test_is_domain_whitelisted_rejects_unlisted_domain() -> None:
    assert is_domain_whitelisted("https://evil.example.com/npa", WHITELIST) is False


def test_is_domain_whitelisted_rejects_lookalike_domain() -> None:
    # "not-kremlin.ru" не должен матчиться под "kremlin.ru" через endswith без точки
    assert is_domain_whitelisted("https://not-kremlin.ru/", {"kremlin.ru"}) is False


def test_is_domain_whitelisted_empty_url_is_false() -> None:
    assert is_domain_whitelisted("", WHITELIST) is False


def test_is_text_content_accepts_html_and_pdf() -> None:
    assert is_text_content("text/html; charset=utf-8") is True
    assert is_text_content("application/pdf") is True
    assert is_text_content("text/plain") is True


def test_is_text_content_rejects_video_and_missing_header() -> None:
    assert is_text_content("video/mp4") is False
    assert is_text_content(None) is False
    assert is_text_content("") is False


def test_is_excluded_path_matches_sfr_branches_info() -> None:
    """PLAN.md Фаза 9 п.1: sfr.gov.ru/branches/*/info/ — статичные справочные страницы
    с рансующейся датой в URL, не публикации о событии, docs/SPEC_stale_publications_filter.md."""
    assert is_excluded_path("https://sfr.gov.ru/branches/77/info/~2026/08/20/1?info_category=3") is True
    assert is_excluded_path("https://www.sfr.gov.ru/branches/78/info/~2023/01/01/9") is True


def test_is_excluded_path_matches_vrf_tass_aggregator() -> None:
    """vrf.tass.ru — агрегатор региональных СМИ (перепечатки «Популярные новости
    России», <регион>/<издание>-ru/<id>), docs/SPEC_vrf_tass_aggregator_filter.md."""
    assert is_excluded_path("https://vrf.tass.ru/arxangelskaia-oblast/region29-ru/1372313") is True
    assert is_excluded_path("https://vrf.tass.ru/ianao/sever-press-ru/13727476-pravitelstvo") is True
    # собственные ленты ТАСС — не агрегатор, остаются в обработке
    assert is_excluded_path("https://tass.ru/obschestvo/28050531") is False


def test_is_excluded_path_does_not_match_regular_news() -> None:
    assert is_excluded_path("https://sfr.gov.ru/press_center/news/~2026/08/19/284025") is False
    assert is_excluded_path("https://kremlin.ru/acts/news/80518") is False


# docs/SPEC_news_activity_filter.md: сигналы 170/171/175 из живого прогона, отклонённые
# экспертом вручную (rejection_reason=not_npa) — должны отсекаться pre-filter'ом.
def test_is_news_activity_noise_speech_verb_with_name_sig170() -> None:
    assert (
        is_news_activity_noise(
            "https://ria.ru/20260828/golikova-1.html",
            "Голикова рассказала об обработке обращений от участников СВО",
        )
        is True
    )


def test_is_news_activity_noise_name_colon_with_statistic_sig171() -> None:
    assert (
        is_news_activity_noise(
            "https://tass.ru/obschestvo/28050531",
            "Голикова: правительство решило 96% обращений участников СВО",
        )
        is True
    )


def test_is_news_activity_noise_crime_sig175() -> None:
    assert (
        is_news_activity_noise(
            "https://rg.ru/2026/08/28/reg-cfo/bryanchanka.html",
            "Брянчанка оформила фиктивный брак с инвалидом и незаконно получила 1,4 млн",
        )
        is True
    )


def test_is_news_activity_noise_lets_real_amendment_through() -> None:
    assert (
        is_news_activity_noise(
            "https://tass.ru/obschestvo/99999999",
            "Внесены изменения в постановление о выплатах участникам СВО № 1234",
        )
        is False
    )


def test_is_news_activity_noise_only_applies_to_news_sources() -> None:
    # тот же заголовок, что sig 170 — на первоисточнике (не tass/ria/rg) не фильтруется
    assert (
        is_news_activity_noise(
            "https://mos.ru/authority/documents/doc/1/",
            "Голикова рассказала об обработке обращений от участников СВО",
        )
        is False
    )


def test_is_news_activity_noise_ignores_statistic_with_npa_requisites() -> None:
    # цифра есть, но реквизиты НПА в описании — не считается "голой статистикой"
    assert (
        is_news_activity_noise(
            "https://tass.ru/obschestvo/1",
            "Выплаты повышены на 5%",
            "Постановление Правительства № 456 от 01.02.2026",
        )
        is False
    )


def test_is_news_activity_noise_empty_title_is_false() -> None:
    assert is_news_activity_noise("https://tass.ru/x", "") is False


# Ревью docs/SPEC_news_activity_filter.md: "следствие"/"прокуратур" бились подстрокой —
# "вследствие" содержит "следствие", а голое упоминание прокуратуры не значит криминал.
def test_is_news_activity_noise_vsledstvie_with_npa_requisites_passes() -> None:
    assert (
        is_news_activity_noise(
            "https://tass.ru/obschestvo/2",
            "Выплаты инвалидам вследствие военной травмы увеличены — "
            "постановление № 10 от 01.01.2026",
        )
        is False
    )


def test_is_news_activity_noise_prosecutor_explains_law_passes() -> None:
    assert (
        is_news_activity_noise(
            "https://ria.ru/20260901/prokuratura-1.html",
            "Прокуратура разъяснила о выплатах инвалидам",
        )
        is False
    )
