"""Guarded recall, diagnostics and event regressions from run 43."""

from pathlib import Path

import pytest

import social_monitor as sm


SETTINGS = sm.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source(name: str = "Тестовый источник", media_type: str = "website") -> sm.Source:
    return sm.Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name=name,
        media_type=media_type,
        domain="example.by",
        start_url="https://example.by",
        language="ru",
    )


def evaluate(title: str, summary: str, text: str = "") -> sm.RelevanceDecision:
    return sm.evaluate_relevance(title, summary, text, source(), SETTINGS)


def test_summary_can_complete_strict_three_anchor_profile() -> None:
    result = evaluate(
        "Фуры едут в магазин, а оказываются у подъезда",
        "Жильцы обращались в ЖЭС и исполком, однако проблема не решается несколько лет.",
        "Фуры едут в магазин, а оказываются у подъезда. https://example.by/story",
    )
    assert result.relevant is True
    assert result.category == "Дороги и благоустройство"


def test_mass_veterinary_product_harm_is_kept_with_uncertainty() -> None:
    result = evaluate(
        "Кошатники бьют тревогу из-за вакцины, которая вызывает побочки",
        "Препарат продают и в Беларуси.",
        "Люди массово жалуются на вакцину для кошек: после прививки отмечали "
        "рвоту, кровавый понос и температуру, некоторые сообщили о смерти "
        "питомцев. Прямая связь пока не установлена, производитель находится "
        "под вниманием ведомства.",
    )
    assert result.relevant is True
    assert result.category == "Качество товаров и услуг"


def test_wage_disparity_overrides_name_only_political_gate() -> None:
    result = evaluate(
        "Лукашенко взялся за зарплаты в Минске — из-за них может рвануть",
        "В некоторых отраслях разница в разы.",
        "В столице зарплаты выше, чем в регионах, но в отдельных сферах "
        "заработки скромные. Статистика показывает разрыв между отраслями.",
    )
    assert result.relevant is True
    assert result.category == "Работа, зарплаты и доходы"


@pytest.mark.parametrize(
    ("title", "summary", "text"),
    (
        (
            "Фура доставила товар к магазину",
            "Разовая поставка завершена.",
            "Водитель разгрузил товар и уехал.",
        ),
        (
            "Кот плохо чувствовал себя после прививки",
            "Хозяин рассказал об одном случае.",
            "Ветеринар осмотрел питомца, массовых жалоб не поступало.",
        ),
        (
            "Средняя зарплата в Минске выросла",
            "Опубликованы очередные статистические данные.",
            "Сообщение не содержит жалоб или разрыва между группами.",
        ),
        (
            "Квота на беспошлинный ввоз электромобилей почти исчерпана",
            "ГТК сообщил, что осталось 98 автомобилей.",
            "После исчерпания квоты будет применяться обычная пошлина.",
        ),
    ),
)
def test_new_profiles_do_not_admit_neutral_or_single_cases(
    title: str, summary: str, text: str
) -> None:
    assert evaluate(title, summary, text).relevant is False


def test_tested_is_not_a_political_protest() -> None:
    result = evaluate(
        "ЖКХ рассказал о готовности к отопительному сезону",
        "Все системы готовы.",
        "Коммунальные службы проверили и протестировали оборудование.",
    )
    assert result.relevant is False
    assert "политическая тема: протест" not in result.reason


def test_real_protest_still_hits_political_gate() -> None:
    result = evaluate(
        "В городе прошел протест",
        "Участники вышли на митинг.",
        "Политическая акция завершилась вечером.",
    )
    assert result.relevant is False
    assert "политическая тема" in result.reason


def make_water_result(
    source_name: str,
    source_type: str,
    priority: str,
    title: str,
    excerpt: str,
    url: str,
) -> sm.ArticleResult:
    fingerprint = sm.infer_event_fingerprint(title, "", excerpt)
    return sm.ArticleResult(
        source_name=source_name,
        source_type=source_type,
        country="Минская область",
        locality="Минская область",
        priority=priority,
        source_language="ru",
        title=title,
        title_generated=source_type == "telegram",
        url=url,
        published_at="2026-09-04T10:15:00+00:00",
        category="ЖКХ и состояние жилья",
        subcategory="водоснабжение",
        excerpt=excerpt,
        signal_type="описание конкретной социально-экономической проблемы",
        official_response=False,
        score=11,
        matched_terms="водоснабжение, без воды",
        discovered_via="homepage",
        text_length=len(excerpt),
        event_region=fingerprint.region,
        event_locality=fingerprint.locality,
        event_object=fingerprint.object_label,
        event_problem=fingerprint.problem_label,
        event_signature=fingerprint.signature,
    )


def test_lyuban_outage_beats_service_crew_geography_and_deduplicates() -> None:
    telegram = make_water_result(
        "Zerkalo.io",
        "telegram",
        "A",
        "Райцентр Любань на Минщине остался без воды из-за повреждения трубопровода",
        "Без воды 168 многоквартирных домов, четыре школы, пять детсадов и больница.",
        "https://t.me/zerkalo_io/173602",
    )
    website = make_water_result(
        "Минская правда",
        "website",
        "B",
        "В Любани 168 домов и соцобъекты остались без воды — идёт восстановление",
        "Аварийная бригада Солигорскводоканала ремонтирует трубопровод. "
        "Без воды 168 домов, школы, детские сады и районная больница.",
        "https://mlyn.by/water-outage",
    )
    assert telegram.event_locality == website.event_locality == "Любань"
    assert telegram.event_signature == website.event_signature == (
        "любань|water_supply|outage"
    )
    consolidated = sm.deduplicate_results([telegram, website])
    assert len(consolidated) == 1
    assert consolidated[0].source_name == "Zerkalo.io"
    assert consolidated[0].related_coverage == (("Минская правда", website.url),)
    assert sm.represented_publication_count(consolidated) == 2


def test_update80_build_marker() -> None:
    assert sm.MONITOR_BUILD == "2026-09-04.social.81-run44-balanced-integrity-1.0"
    assert sm.ARCHITECTURE_CORE_VERSION == "3.9"
    assert sm.SEMANTIC_DATA_CONTRACT_VERSION == "1.0"
