"""Systemic recall, precision, title and event regressions from run 42."""

from pathlib import Path

import pytest

import social_monitor as sm


SETTINGS = sm.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source(locality: str = "Беларусь") -> sm.Source:
    return sm.Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality=locality,
        rank=1,
        priority="A",
        name="Тестовый источник",
        media_type="website",
        domain="example.by",
        start_url="https://example.by",
        language="ru",
        adapter="standard",
    )


def decision(title: str, text: str, locality: str = "Беларусь"):
    return sm.evaluate_relevance(title, "", text, source(locality), SETTINGS)


@pytest.mark.parametrize(
    ("expected_category", "title", "text"),
    (
        (
            "ЖКХ и состояние жилья",
            "Застройщик отреагировал на проблему мокрых пятен в «Английском квартале»",
            "О проблеме жительницы мы писали ранее. На стене квартиры в Минске "
            "появляются мокрые разводы. Залития продолжаются несколько лет, "
            "появилась плесень, опасная для здоровья четырехлетнего ребенка. "
            "Застройщик провел комиссионное обследование межпанельных швов.",
        ),
        (
            "Социальная защита и базовые услуги",
            "Изменения «Беларусбанка» вызвали активное обсуждение",
            "Банк отменяет возможность добавлять в мобильное приложение карты "
            "других людей. Если их не удалить, все карты перестанут работать. "
            "У клиентов возникло много вопросов: функция позволяла помогать "
            "пожилым родственникам распоряжаться счетом. Пользователи возмущены.",
        ),
        (
            "ЖКХ и состояние жилья",
            "Жители улицы Полевой страдают от хламовозов",
            "В редакцию обратилась жительница дома. Возле контейнерной площадки "
            "регулярно возникает несанкционированная свалка: мусор гниет, появился "
            "запах и крысы. Хлам сгружают на зеленую зону, где жильцы своими "
            "силами обустроили детскую площадку. Материалы можно направлять в РОВД.",
        ),
    ),
)
def test_confirmed_run42_false_negatives_are_admitted(
    expected_category: str, title: str, text: str
) -> None:
    result = decision(title, text)
    assert result.relevant is True
    assert result.category == expected_category


@pytest.mark.parametrize(
    ("expected_reason", "title", "text"),
    (
        (
            "медицинское разъяснение рисков",
            "Молодость ничего не гарантирует: почему инсульты бывают даже у детей",
            "Врач-невролог объяснила причины заболевания и назвала внешние факторы риска.",
        ),
        (
            "зарубежная критическая инфраструктура",
            "Тотальный блэкаут: на ЗАЭС нет внешнего электроснабжения",
            "Запорожская АЭС работает от дизельных генераторов из-за обстрелов на Украине.",
        ),
        (
            "позитивная ретроспектива",
            "А вы помните, как появилась детская площадка в парке?",
            "В 2021 году площадку открыли как доброе дело. Она стала любимым местом семей.",
        ),
        (
            "официальное толкование нормы",
            "Требования к сбросу дождевых и талых вод от жилой застройки",
            "Официальное толкование новой редакции Водного кодекса. Новая редакция "
            "не меняет действующие подходы; такой сброс не является нарушением и "
            "основанием для административной ответственности.",
        ),
    ),
)
def test_clear_run42_noise_is_rejected(
    expected_reason: str, title: str, text: str
) -> None:
    result = decision(title, text)
    assert result.relevant is False
    # A short synthetic lead may be stopped by the general relevance barrier
    # before the diagnostic genre gate.  Exercise the gate directly as well,
    # so both the rejection and its reusable editorial classification are fixed.
    reason = sm.result_integrity_genre_rejection(title, text)
    assert expected_reason in reason


def test_foreign_named_quarter_exception_requires_domestic_housing_evidence() -> None:
    result = decision(
        "Жильцы жалуются на плесень в «Английском квартале» Лондона",
        "Жители британской столицы рассказали о мокрых стенах в квартирах.",
    )
    assert result.relevant is False


def test_neutral_bank_announcement_does_not_gain_mass_grievance_status() -> None:
    result = decision(
        "Банк обновляет мобильное приложение",
        "С октября изменится порядок добавления карт. Банк опубликовал инструкцию.",
    )
    assert result.relevant is False


def test_single_dumping_enforcement_is_not_a_collective_grievance() -> None:
    result = decision(
        "Нарушителя оштрафовали за мусор возле контейнера",
        "Милиция установила одного водителя и составила протокол.",
    )
    assert result.relevant is False


def test_real_healthcare_complaint_overrides_advice_like_wording() -> None:
    result = decision(
        "Почему дети не могут попасть к неврологу",
        "Родители массово жалуются: запись в детскую поликлинику отсутствует, "
        "а очередь к врачу растянулась на несколько месяцев.",
    )
    assert result.relevant is True
    assert result.category == "Здравоохранение"


def article(
    title: str,
    excerpt: str,
    source_name: str,
    source_type: str,
    url: str,
    signature: str,
) -> sm.ArticleResult:
    return sm.ArticleResult(
        source_name=source_name,
        source_type=source_type,
        country="Беларусь",
        locality="Беларусь",
        priority="A",
        source_language="ru",
        title=title,
        title_generated=False,
        url=url,
        published_at="2026-09-04T08:00:00+00:00",
        category="Здравоохранение",
        subcategory="",
        excerpt=excerpt,
        signal_type="жалоба жителей или пользователей",
        official_response=False,
        score=10,
        matched_terms="",
        discovered_via="homepage",
        text_length=len(excerpt),
        event_region="Гомельская область",
        event_locality="Гомель",
        event_signature=signature,
    )


def test_three_hospital_rewrites_become_one_card() -> None:
    results = [
        article(
            "Маці паскардзілася на траўмы 5‑гадовага сына ў псіхіятрычнай бальніцы Гомеля",
            "Што адказалі дактары?",
            "Наша Ніва",
            "telegram",
            "https://t.me/nashaniva/121131",
            "гомель|healthcare|safety",
        ),
        article(
            "Откуда у ребенка травмы? Конфликт мамы и Гомельской областной психиатрической больницы",
            "Мать пятилетнего мальчика с инвалидностью заявила, что сын получил "
            "травмы в психиатрическом отделении. После выписки обнаружены гематомы.",
            "БрестСИТИ",
            "website",
            "https://brestcity.com/blog/konflikt-mamy-rebenka-invalida",
            "гомель|healthcare|safety",
        ),
        article(
            "Жлобинчанка увидела на теле сына гематомы после выписки из гомельской психбольницы",
            "Мама пожаловалась на больницу и написала обращение в Минздрав.",
            "Gomel Today",
            "telegram",
            "https://t.me/gomeltoday/162662",
            "region:гомельская область|healthcare|access_restriction",
        ),
    ]
    consolidated = sm.deduplicate_results(results)
    assert len(consolidated) == 1
    assert consolidated[0].source_name == "БрестСИТИ"
    assert {name for name, _url in consolidated[0].related_coverage} == {
        "Наша Ніва", "Gomel Today",
    }
    assert sm.represented_publication_count(consolidated) == 3


def test_telegram_title_keeps_brand_domain_and_stops_at_question() -> None:
    raw = (
        "😖 Что происходит с картошкой фри в Mak.by ? "
        "Посетители регулярно жалуются на её качество. К нам обратился подписчик."
    )
    assert sm.telegram_title_from_text(raw) == "Что происходит с картошкой фри в Mak.by?"


def test_update79_build_marker() -> None:
    assert sm.MONITOR_BUILD == "2026-09-04.social.81-run44-balanced-integrity-1.0"
    assert sm.ARCHITECTURE_CORE_VERSION == "3.9"
    assert sm.SEMANTIC_DATA_CONTRACT_VERSION == "1.0"
