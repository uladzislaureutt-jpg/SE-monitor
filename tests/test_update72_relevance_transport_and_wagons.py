from pathlib import Path
from unittest.mock import patch

import requests

import social_monitor


SETTINGS = social_monitor.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source() -> social_monitor.Source:
    return social_monitor.Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name="Тестовый источник",
        media_type="website",
        domain="example.by",
        start_url="https://example.by",
        language="ru",
        adapter="standard",
    )


def decision(title: str, text: str):
    return social_monitor.evaluate_relevance(title, "", text, source(), SETTINGS)


def result(name: str, title: str, excerpt: str, url: str, category: str):
    fingerprint = social_monitor.infer_event_fingerprint(title, "", excerpt)
    return social_monitor.ArticleResult(
        source_name=name,
        source_type="website",
        country="Беларусь",
        locality="Беларусь",
        priority="A",
        source_language="ru",
        title=title,
        title_generated=False,
        url=url,
        published_at="2026-08-30T12:00:00+00:00",
        category=category,
        subcategory="",
        excerpt=excerpt,
        signal_type="описание конкретной социально-экономической проблемы",
        official_response=False,
        score=10,
        matched_terms="",
        discovered_via="feed:test",
        text_length=len(excerpt),
        event_region=fingerprint.region,
        event_locality=fingerprint.locality,
        event_object=fingerprint.object_label,
        event_problem=fingerprint.problem_label,
        event_signature=fingerprint.signature,
    )


def test_update72_rejects_confirmed_report_26_editorial_noise():
    cases = (
        ("Когда Щомыслица примет районные «Дожинки-2026»", "Организаторы рассказали о подготовке праздника и благоустройстве."),
        ("В ходе строительства 1-й очереди ВЛ-330 кВ «Березовская ГРЭС – Пинск» ведётся установка опор-великанов", "Энергетики сообщили о ходе штатного строительства линии."),
        ("Психолог поделился опытом – что давать детям для перекуса в школу", "Специалист дал родителям бытовые советы по школьному питанию."),
        ("«Самая красивая страна в мире из тех, где я побывала»", "Белоруска рассказала о личной поездке в Чили и смене профессии."),
        ("Главные новости в воскресенье, 30 августа", "В одном дайджесте собраны несвязанные новости, среди которых упомянута очередь к врачу."),
        ("Не только стресс: скрытые симптомы дефицита магния", "Врач перечислил симптомы и дал рекомендации по питанию."),
        ("Топ-5 вакансий Гродно и области с зарплатами от 6000 рублей", "Редакция составила подборку предложений работодателей."),
        ("Жир при похудении не превращается в мышцы: ученые объяснили, куда он уходит", "Научно-популярное объяснение физиологии похудения."),
        ("Ограничения на посещение лесов введены почти по всей Беларуси", "Лесхозы опубликовали штатное административное ограничение."),
        ("В Барсуках дали вторую жизнь целой библиотеке", "Позитивный репортаж о местной инициативе и книгах."),
        ("В Полоцке состоялось плановое заседание комиссии по противодействию коррупции", "Комиссия обсудила план работы без сообщения о нарушениях."),
        ("Пассажир получил травму позвоночника в автобусе. Суд взыскал компенсацию морального вреда", "Суд рассмотрел единичное происшествие с одним пассажиром."),
        ("Не работаете после увольнения? Когда можно попасть в базу «тунеядцев»", "Справочный материал объясняет порядок учёта граждан."),
    )
    for title, text in cases:
        evaluated = decision(title, text)
        assert not evaluated.relevant, (title, evaluated.reason)


def test_update72_consolidates_national_platform_wagon_shortage_across_categories():
    rows = [
        result(
            "Zerkalo", "В Беларуси начался дефицит грузовых вагонов-платформ",
            "Железнодорожники связывают нехватку платформ БЖД с воинскими перевозками.",
            "https://a.example/wagons", "Дороги и благоустройство",
        ),
        result(
            "Reform", "На БЖД возник дефицит вагонов-платформ из-за воинских перевозок",
            "Сообщество железнодорожников сообщило о нехватке платформ БЖД.",
            "https://b.example/wagons", "Общественный транспорт",
        ),
        result(
            "Медиа-Полесье", "На Белорусской железной дороге сообщили о нехватке вагонов-платформ",
            "Нехватку вагонов-платформ БЖД связывают с воинскими перевозками.",
            "https://c.example/wagons", "Цены, торговля и дефицит",
        ),
    ]
    assert {item.event_signature for item in rows} == {
        "беларусь|rail_platform_wagons|absence_shortage"
    }
    consolidated = social_monitor.deduplicate_results(rows)
    assert len(consolidated) == 1
    assert social_monitor.represented_publication_count(consolidated) == 3


def test_update72_records_safe_transport_failure_detail_and_report_warning():
    client = social_monitor.HttpClient({"monitor": {"request_timeout_seconds": 1}})
    with patch.object(
        social_monitor.requests,
        "get",
        side_effect=requests.ConnectionError("temporary name resolution failure"),
    ):
        assert client.get("https://orshanka.by", retries=0) is None
    observation = client.observation_for("https://orshanka.by")
    assert observation is not None
    assert observation.failure_class == "network_error"
    assert observation.detail.startswith("ConnectionError:")

    alerts = social_monitor.source_access_alerts([
        {
            "country": "Беларусь",
            "source": "Аршанская газета",
            "access_status": "transport_blocked",
            "access_status_reason": "all attempted access paths failed",
        },
        {"country": "Беларусь", "source": "Кліч Радзімы", "access_status": "healthy_no_recent"},
    ])
    assert len(alerts) == 1
    assert "Аршанская газета" in alerts[0]
    assert "social_access_telemetry" in alerts[0]
