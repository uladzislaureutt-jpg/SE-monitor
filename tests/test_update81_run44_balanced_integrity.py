"""Balanced recall, precision and event identity regressions from run 44."""

from pathlib import Path

import pytest

import social_monitor as sm


SETTINGS = sm.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source() -> sm.Source:
    return sm.Source(
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
    )


def evaluate(title: str, summary: str = "", text: str = "") -> sm.RelevanceDecision:
    return sm.evaluate_relevance(title, summary, text, source(), SETTINGS)


@pytest.mark.parametrize(
    ("title", "summary", "text", "category"),
    (
        (
            "Фуры едут в магазин, а оказываются у подъезда",
            "Жильцы обращались в ЖЭС и исполком, однако проблема не решается несколько лет.",
            "Из-за одинакового адреса грузовики попадают во двор. Однажды произошло ДТП.",
            "Дороги и благоустройство",
        ),
        (
            "Люди жалуются на камеры в туалетах и утечки персональных данных",
            "За восемь месяцев поступило 700 жалоб — вдвое больше, чем год назад.",
            "Граждане сообщают о камерах в женских туалетах, незаконной передаче данных и утечках.",
            "Законы, права и общественное регулирование",
        ),
        (
            "Мужчина уволился, а с ним не рассчитались",
            "В последний рабочий день прорабу не выплатили окончательный расчет.",
            "Прокуратура установила долг в 8 тысяч рублей и невыплату взносов в ФСЗН.",
            "Работа, зарплаты и доходы",
        ),
        (
            "Кулинарный цех приостановил работу из-за санитарных нарушений",
            "",
            "В пищевом производстве нашли грязное оборудование и сомнительное мясо.",
            "Качество товаров и услуг",
        ),
        (
            "Почему во дворах Минска так много машин",
            "Дворы заставлены автомобилями, а паркинги не заполнены.",
            "Эксперт-урбанист объяснила, что проблема с парковками системная. Лукашенко предложил убрать стоянки.",
            "Дороги и благоустройство",
        ),
        (
            "Беларуска пожаловалась на обслуживание в магазине",
            "Сотрудница отказалась помочь покупательнице, которая плохо видит.",
            "В комментариях описали похожие случаи в других магазинах. Представители сети извинились и начали разбор.",
            "Качество товаров и услуг",
        ),
        (
            "Что влияет на качество питьевой воды в микрорайоне",
            "Водоканал признал временное ухудшение качества воды.",
            "Старая автоматика водозабора изношена, остановки повторялись; заключен договор на обновление.",
            "ЖКХ и состояние жилья",
        ),
        (
            "Можно ли продавцам работать по 13–14 часов",
            "В профсоюз обратилась работница торговли, которая трудится по 13–14 часов.",
            "В профсоюз обратилась работница торговли, которая трудится по 13–14 часов. "
            "Инспектор пояснил: рабочая смена не должна превышать 12 часов.",
            "Работа, зарплаты и доходы",
        ),
    ),
)
def test_confirmed_run44_losses_are_restored(
    title: str, summary: str, text: str, category: str
) -> None:
    result = evaluate(title, summary, text)
    assert result.relevant is True
    assert result.category == category


@pytest.mark.parametrize(
    ("title", "summary", "text"),
    (
        (
            "Ушел с дачи: пропал 79-летний мужчина",
            "Поисковики просят сообщить сведения.",
            "Мужчина ушел с дачи и не вернулся.",
        ),
        (
            "На дороге включили систему фиксации средней скорости",
            "Новый участок длиной 12 км взят под контроль.",
            "Камеры начали работать сегодня.",
        ),
        (
            "Что изменят в законодательстве о персональных данных. Главное для бизнеса",
            "Собрали основные изменения для предпринимателей.",
            "Это нейтральное разъяснение проекта закона без жалоб и нарушений.",
        ),
        (
            "Комбайнеры заработали до 19 тысяч рублей",
            "Лучшие механизаторы получили высокий сезонный заработок.",
            "Опубликован позитивный рейтинг работников уборочной кампании.",
        ),
        (
            "Дальний Восток заинтересован в белорусской технике",
            "Российский регион хочет покупать технику и топливо.",
            "Стороны обсудили экспортные поставки.",
        ),
        (
            "Внимание безопасности на дорогах",
            "ГАИ проводит профилактическую кампанию.",
            "Водителям напомнили правила дорожного движения.",
        ),
        (
            "ТРЦ ищет название: победителю обещают смартфон",
            "Объявлен конкурс на лучшее название торгового центра.",
            "Участники предлагают варианты, победитель получит приз.",
        ),
        (
            "Перевозчик разрешил бесплатно пересаживаться в автобус",
            "Пассажиры могут выбрать автобус, который стоит ближе к границе.",
            "Бесплатная пересадка уже действует.",
        ),
        (
            "Фермер возмутил покупателей ценой домашнего сала",
            "Частный продавец назначил 40 рублей за килограмм.",
            "Пользователи спорят о цене сала у одного фермера.",
        ),
        (
            "Мэр опроверг слухи о переносе строительства метро",
            "Информация о задержке не соответствует действительности.",
            "Работы идут по графику.",
        ),
    ),
)
def test_confirmed_run44_noise_is_rejected(
    title: str, summary: str, text: str
) -> None:
    assert evaluate(title, summary, text).relevant is False


@pytest.mark.parametrize(
    ("title", "summary", "text"),
    (
        (
            "В магазине произошел единичный конфликт",
            "Один покупатель сообщил о грубом ответе кассира.",
            "Других жалоб и проверки не было.",
        ),
        (
            "Как защитить персональные данные",
            "Специалист дал рекомендации пользователям.",
            "Фактических жалоб, утечек и нарушений не выявлено.",
        ),
        (
            "Водоканал рассказал о подготовке к зиме",
            "Оборудование проверили по графику.",
            "Качество питьевой воды соответствует нормам, жалоб нет.",
        ),
        (
            "Кулинарный цех обновил оборудование",
            "Предприятие провело плановую уборку.",
            "Санитарных нарушений не выявлено, производство работает.",
        ),
    ),
)
def test_new_recall_profiles_do_not_admit_neutral_or_single_cases(
    title: str, summary: str, text: str
) -> None:
    assert evaluate(title, summary, text).relevant is False


def make_result(
    name: str,
    title: str,
    excerpt: str,
    category: str,
    url: str,
) -> sm.ArticleResult:
    fingerprint = sm.infer_event_fingerprint(title, excerpt, excerpt)
    return sm.ArticleResult(
        source_name=name,
        source_type="website",
        country="Беларусь",
        locality="Беларусь",
        priority="A",
        source_language="ru",
        title=title,
        title_generated=False,
        url=url,
        published_at="2026-09-04T13:00:00+00:00",
        category=category,
        subcategory="",
        excerpt=excerpt,
        signal_type="описание конкретной социально-экономической проблемы",
        official_response=False,
        score=10,
        matched_terms="",
        discovered_via="feed",
        text_length=len(excerpt),
        event_region=fingerprint.region,
        event_locality=fingerprint.locality,
        event_object=fingerprint.object_label,
        event_problem=fingerprint.problem_label,
        event_signature=fingerprint.signature,
    )


def test_lyuban_adjective_resolves_to_lyuban_not_soligorsk() -> None:
    fp = sm.infer_event_fingerprint(
        "Авария в Любанском районе. Без воды остались сотни домов",
        "Бригада Солигорскводоканала устраняет прорыв.",
        "Без воды 168 домов, четыре школы, пять детсадов и больница.",
    )
    assert fp.locality == "Любань"
    assert fp.signature == "любань|water_supply|outage"


def test_dns_enforcement_rewrites_consolidate_despite_different_headline_numbers() -> None:
    left = make_result(
        "Белновости",
        "В сети DNS обнаружили опасные товары",
        "Госстандарт проверил 14 магазинов DNS и запретил реализацию 116 наименований техники без документов о соответствии.",
        "Качество товаров и услуг",
        "https://example.by/dns-a",
    )
    right = make_result(
        "Blizko.by",
        "В магазинах DNS запретили продавать 116 наименований техники",
        "В 14 торговых точках ДНС нашли технику без обязательных документов о соответствии.",
        "Качество товаров и услуг",
        "https://example.by/dns-b",
    )
    assert left.event_signature == right.event_signature == (
        "беларусь|consumer_electronics_compliance|sale_noncompliance"
    )
    assert len(sm.deduplicate_results([left, right])) == 1


def test_veterinary_price_survey_rewrites_consolidate_across_categories() -> None:
    left = make_result(
        "Gomel Today",
        "Напомним, ранее в министерстве выложили опрос о ценах на ветеринарные услуги",
        "В опросе МАРТ участвовали 5 тысяч человек; 51% считают цены на ветуслуги завышенными.",
        "Качество товаров и услуг",
        "https://example.by/vet-a",
    )
    right = make_result(
        "Могилёв Online",
        "Более половины белорусов считают цены в частных ветклиниках завышенными",
        "В опросе МАРТ участвовали свыше 5 тысяч человек, 51% назвали цены завышенными.",
        "Цены, торговля и дефицит",
        "https://example.by/vet-b",
    )
    assert left.event_signature == right.event_signature == (
        "беларусь|veterinary_service_prices|price_overcharge"
    )
    assert len(sm.deduplicate_results([left, right])) == 1


def test_short_generated_discourse_fragment_is_recovered_from_full_text() -> None:
    title = sm.repair_generated_title_from_text(
        "Напомним, ранее в министерстве",
        "Напомним, ранее в министерстве выложили онлайн-опрос о ценах на "
        "ветеринарные услуги. Ответили около 5 тысяч человек.",
    )
    assert title == (
        "Напомним, ранее в министерстве выложили онлайн-опрос о ценах на "
        "ветеринарные услуги."
    )


def test_update81_build_marker() -> None:
    assert sm.MONITOR_BUILD == "2026-09-04.social.81-run44-balanced-integrity-1.0"
    assert sm.ARCHITECTURE_CORE_VERSION == "3.9"
    assert sm.SEMANTIC_DATA_CONTRACT_VERSION == "1.0"


def test_neutral_working_hours_do_not_trigger_enforcement_profile() -> None:
    text = "Работники магазина работают по 8–9 часов. График согласован с нанимателем."
    assert "labour_rights_enforcement" not in sm.bound_public_issue_profiles(text.lower())


def test_apology_alone_does_not_prove_repeated_consumer_harm() -> None:
    text = "В магазине продавец отказалась помочь. Представители сети извинились."
    assert "recurring_retail_service_complaints" not in sm.protected_public_issue_profiles(text.lower())


def test_foreign_vaccine_harm_without_belarus_market_link_stays_rejected() -> None:
    result = evaluate(
        "В России владельцы кошек жалуются на тяжелые реакции после вакцины",
        "В Москве и Самаре многочисленные владельцы сообщили о гибели кошек после вакцинации.",
        "Российский производитель проверяет сообщения. Препарат продают в России.",
    )
    assert result.relevant is False


def test_dns_same_chain_different_enforcement_numbers_do_not_merge() -> None:
    left = make_result("A", "DNS: запрет продажи техники", "Госстандарт запретил 116 товаров в 14 магазинах DNS.",
                       "Качество товаров и услуг", "https://example.by/a")
    right = make_result("B", "DNS: запрет продажи техники", "Госстандарт запретил 37 товаров в 6 магазинах DNS.",
                        "Качество товаров и услуг", "https://example.by/b")
    # Test the new narrow event matcher, not the independent generic
    # near-identical-title rule: genuinely different titles are essential.
    right.title = "Проверка бытовых приборов: ограничения для торговой сети ДНС"
    assert not sm._looks_like_same_event(left, right)


def test_lead_numeric_evidence_is_not_lost_with_short_display_excerpt() -> None:
    left = make_result("A", "В DNS нашли опасную технику", "Нет сертификатов соответствия.",
                       "Качество товаров и услуг", "https://example.by/dns-1")
    right = make_result("B", "ДНС: запрещены 116 товаров", "Проверены 14 магазинов.",
                        "Качество товаров и услуг", "https://example.by/dns-2")
    for article in (left, right):
        article.event_signature = "беларусь|consumer_electronics_compliance|sale_noncompliance"
    left.event_numeric_anchors = sm.event_numeric_anchors_from_text("Проверены 14 магазинов, запрещены 116 товаров.")
    assert sm._looks_like_same_event(left, right)


def test_explicit_national_scope_does_not_inherit_newsroom_city() -> None:
    left = make_result("A", "Опрос МАРТ о ценах на ветуслуги", "51% считают цены завышенными.",
                       "Качество товаров и услуг", "https://example.by/survey-1")
    right = make_result("B", "Цены в ветклиниках завышены", "Так ответили 51% опрошенных МАРТ.",
                        "Цены, торговля и дефицит", "https://example.by/survey-2")
    left.locality, right.locality = "Гомель", "Могилёв"
    assert sm._same_event_scope(left, right)


def test_water_outage_uses_named_satellite_settlement_not_generic_area() -> None:
    left = make_result("A", "Авария в Любани", "168 домов без воды, агрогородок Сорочи также отключён.",
                       "ЖКХ и состояние жилья", "https://example.by/water-1")
    right = make_result("B", "Любанский район без водоснабжения", "Прорыв трубы: агрогородок Сорочи и районная больница без воды.",
                        "ЖКХ и состояние жилья", "https://example.by/water-2")
    for article in (left, right):
        article.event_signature = "любань|water_supply|outage"
        article.event_locality = "Любань"
    assert sm._looks_like_same_event(left, right)
    right.excerpt = "Прорыв трубы: агрогородок Другой без воды."
    assert not sm._looks_like_same_event(left, right)
