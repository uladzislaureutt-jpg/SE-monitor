from pathlib import Path
import datetime as dt

import social_monitor


SETTINGS = social_monitor.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source(
    locality: str = "Минск",
    *,
    name: str = "Тестовое СМИ",
    media_type: str = "website",
) -> social_monitor.Source:
    return social_monitor.Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality=locality,
        rank=1,
        priority="A",
        name=name,
        media_type=media_type,
        domain="example.by" if media_type == "website" else "t.me",
        start_url="https://example.by",
        language="ru",
        adapter="standard",
    )


def decision(title: str, text: str, locality: str = "Минск"):
    return social_monitor.evaluate_relevance(
        title, "", text, source(locality), SETTINGS
    )


def article(
    source_name: str,
    title: str,
    excerpt: str,
    url: str,
    category: str,
    *,
    locality: str = "Беларусь",
    source_type: str = "website",
    event_region: str = "",
) -> social_monitor.ArticleResult:
    return social_monitor.ArticleResult(
        source_name=source_name,
        source_type=source_type,
        country="Беларусь",
        locality=locality,
        priority="A",
        source_language="ru",
        title=title,
        title_generated=False,
        url=url,
        published_at="2026-08-25T08:00:00+00:00",
        category=category,
        subcategory="",
        excerpt=excerpt,
        signal_type="описание конкретной социально-экономической проблемы",
        official_response=False,
        score=10,
        matched_terms="",
        discovered_via="feed:test",
        text_length=len(excerpt),
        event_region=event_region,
    )


def flagshtok_source() -> social_monitor.Source:
    return social_monitor.Source(
        enabled=True,
        country="Беларусь",
        country_code="BY-HO",
        locality="Гомель",
        rank=2,
        priority="A",
        name="Флагшток",
        media_type="website",
        domain="flagshtok.info",
        start_url="https://flagshtok.info/",
        language="ru",
        adapter="standard",
        sitemap_url="https://flagshtok.info/sitemap-news.xml",
    )


def test_result_integrity_15_rejects_report_7_noise():
    cases = (
        (
            "Платная парковка в Минске расширяется. Появится более 10 тысяч машино-мест",
            "Сеть платных парковок планируют расширить и увеличить количество мест.",
        ),
        (
            "Какие грибы собирают под Минском в конце августа",
            "Грибники советуют места для тихой охоты и обсуждают урожай.",
        ),
        (
            "Очередь на три часа: брестчане повезли урожай яблок на переработку",
            "Переработка урожая удобна, а ожидание стало сезонной традицией.",
        ),
        (
            "Водитель остался без прав на 4 месяца за опасный обгон под Минском",
            "ГАИ привлекла водителя к ответственности, назначила штраф и лишение прав.",
        ),
        (
            "После границы с РФ невозможно заправиться: дефицит топлива",
            "Бензина нет в Смоленской области, Подмосковье и Москве.",
        ),
    )
    for title, text in cases:
        assert not decision(title, text).relevant, title


def test_result_integrity_15_keeps_report_7_public_problem_controls():
    cases = (
        (
            "Вместо большого автобуса приехал маленький",
            "Пассажиры жалуются: автобус был переполнен, люди опоздали на пересадки.",
        ),
        (
            "Около года не было ни одного офтальмолога",
            "Жители Светлогорска жалуются, что не могут записаться к врачу.",
        ),
        (
            "Школьника несколько раз избивали толпой",
            "Семья пожаловалась на буллинг в гимназии, чиновники начали разбирательство.",
        ),
        (
            "Почему загрязняется Дитва",
            "Жители жалуются на загрязнение реки Дитва: предприятия сбрасывают стоки, "
            "а очистные сооружения перегружены.",
        ),
    )
    for title, text in cases:
        assert decision(title, text).relevant, title


def test_result_integrity_15_keeps_and_classifies_animal_welfare_complaint():
    result = decision(
        "Минчан разозлили живые рыбки в лампе",
        "Аквариумисты подтвердили неправильные условия содержания: перегрев от лампы, "
        "нулевая фильтрация и нехватка воды. Владельцы не собираются ничего менять.",
    )
    assert result.relevant
    assert result.category == "Защита животных и условия содержания"


def test_event_integrity_15_merges_pinsk_confectionery_rewrites():
    category = "Качество товаров и услуг"
    results = social_monitor.deduplicate_results([
        article(
            "Onliner",
            "В пирожных из Пинска нашли кишечную палочку и золотистый стафилококк",
            "В пирожных Улитка и Эгер Пинского кооппрома выявили кишечную палочку и стафилококк.",
            "https://example.by/a", category, event_region="Брестская область",
        ),
        article(
            "Виртуальный Брест",
            "После Улитки под запрет попали Кокоски",
            "В пирожных Пинского кооппрома нашли золотистый стафилококк.",
            "https://example.by/b", category, event_region="Брестская область",
        ),
        article(
            "Сильные Новости",
            "Стафилококк и кишечная палочка: какие десерты запретили",
            "Десерты признаны опасными из-за стафилококка и кишечной палочки.",
            "https://example.by/c", category,
        ),
    ])
    assert len(results) == 1
    assert len(results[0].related_coverage) == 2


def test_event_integrity_15_merges_communal_overcharge_and_prefers_site():
    category = "ЖКХ и состояние жилья"
    results = social_monitor.deduplicate_results([
        article(
            "Беларусь за МКАДом", "Год платили за чужую протечку",
            "В общежитии воду из-за утечки списывали на жильцов.",
            "https://t.me/example/1", category, locality="Кричев",
            source_type="telegram", event_region="Могилевская область",
        ),
        article(
            "Kraj.by", "Жильцам вернули деньги за утечку воды в общежитии",
            "Жильцы почти год платили за утечку воды. После проверки им вернули 8 тысяч рублей.",
            "https://example.by/leak", category, locality="Кричев",
            event_region="Могилевская область",
        ),
    ])
    assert len(results) == 1
    assert results[0].source_name == "Kraj.by"
    assert results[0].related_coverage == (
        ("Беларусь за МКАДом", "https://t.me/example/1"),
    )


def test_event_integrity_15_merges_store_hiring_rewrites():
    category = "Работа, зарплаты и доходы"
    results = social_monitor.deduplicate_results([
        article(
            "Zerkalo.io", "Наниматель не может найти продавцов на зарплату 1500 рублей",
            "Наниматель пожаловалась, что не может найти продавцов. Обсуждают зарплату.",
            "https://example.by/jobs-a", category,
        ),
        article(
            "Хартия-97", "Год ищем человека в наш магазин, никто не хочет идти",
            "Владелица магазина не может найти работника. В комментариях спорят о зарплате 3000 рублей.",
            "https://example.by/jobs-b", category,
        ),
    ])
    assert len(results) == 1
    assert len(results[0].related_coverage) == 1


def test_event_integrity_15_does_not_merge_food_findings_across_regions():
    category = "Качество товаров и услуг"
    results = social_monitor.deduplicate_results([
        article(
            "Источник А", "В пирожных нашли стафилококк",
            "Пирожные признаны опасными из-за стафилококка.",
            "https://example.by/brest", category, event_region="Брестская область",
        ),
        article(
            "Источник Б", "В десертах нашли стафилококк",
            "Десерты признаны опасными из-за стафилококка.",
            "https://example.by/minsk", category, event_region="Минск",
        ),
    ])
    assert len(results) == 2


def test_event_integrity_15_fingerprints_bullying_and_food_safety():
    bullying = social_monitor.infer_event_fingerprint(
        "В Барановичах школьника избивали толпой",
        "",
        "Семья пожаловалась на буллинг в гимназии.",
    )
    assert bullying.object_key == "education"
    assert bullying.problem_key == "bullying"

    food = social_monitor.infer_event_fingerprint(
        "В пирожных из Пинска нашли кишечную палочку",
        "",
        "Опасную продукцию Пинского кооппрома запретили.",
    )
    assert food.object_key == "food_product"
    assert food.problem_key == "contamination"


def test_source_resilience_11_flagshtok_uses_curated_current_sitemaps():
    profile = social_monitor.effective_source_profile(flagshtok_source(), SETTINGS)
    assert profile["exact_discovery"]
    assert "https://flagshtok.info/sitemap-news.xml" in profile["sitemaps"]
    assert "https://flagshtok.info/sitemap-part-2026.xml" in profile["sitemaps"]


def test_source_resilience_11_news_sitemap_reads_nested_date_and_newest_first():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
      <url>
        <loc>https://flagshtok.info/ru/naviny/svezhaja-problema-gomelja.html</loc>
        <news:news>
          <news:publication_date>2026-08-25T08:00:00+03:00</news:publication_date>
          <news:title>Свежая публикация</news:title>
        </news:news>
      </url>
      <url>
        <loc>https://flagshtok.info/ru/naviny/vtoraja-problema-gomelja.html</loc>
        <news:news>
          <news:publication_date>2026-08-24T20:00:00+03:00</news:publication_date>
          <news:title>Вторая публикация</news:title>
        </news:news>
      </url>
      <url>
        <loc>https://flagshtok.info/ru/naviny/staraja-problema-gomelja.html</loc>
        <news:news>
          <news:publication_date>2025-01-01T08:00:00+03:00</news:publication_date>
          <news:title>Старая публикация</news:title>
        </news:news>
      </url>
    </urlset>'''.encode("utf-8")

    kind, records = social_monitor.parse_sitemap_document(xml)
    assert kind == "urlset"
    assert records[0]["publication_date"] == "2026-08-25T08:00:00+03:00"
    assert records[0]["title"] == "Свежая публикация"

    class Response:
        content = xml

        def __bool__(self):
            return True

    class Client:
        def get(self, _url):
            return Response()

    candidates = social_monitor.collect_from_sitemap(
        flagshtok_source(),
        "https://flagshtok.info/sitemap-news.xml",
        Client(),
        dt.datetime(2026, 8, 24, 0, 0, tzinfo=dt.timezone.utc),
        limit=2,
        max_children=0,
    )
    assert [item.title for item in candidates] == [
        "Свежая публикация", "Вторая публикация",
    ]
