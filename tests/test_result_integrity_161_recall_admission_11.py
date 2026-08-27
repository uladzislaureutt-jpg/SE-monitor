from pathlib import Path

import social_monitor


SETTINGS = social_monitor.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source(locality: str = "Беларусь") -> social_monitor.Source:
    return social_monitor.Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality=locality,
        rank=1,
        priority="A",
        name="Тестовое СМИ",
        media_type="website",
        domain="example.by",
        start_url="https://example.by",
        language="ru",
        adapter="standard",
    )


def decision(title: str, text: str, locality: str = "Беларусь"):
    return social_monitor.evaluate_relevance(
        title, "", text, source(locality), SETTINGS
    )


def test_recall_161_keeps_domestic_product_safety_enforcement():
    cases = (
        (
            "В Беларуси запретили продавать лапшу Buldak из-за опасного красителя",
            "Госстандарт выявил опасный краситель. Продукт не соответствует "
            "обязательным требованиям, поэтому его запретили продавать в Беларуси.",
        ),
        (
            "Какао-порошок из Бобруйского района запретили продавать в Беларуси",
            "Госстандарт установил несоответствие требованиям безопасности и "
            "изъял пищевой продукт из продажи.",
        ),
    )
    for title, text in cases:
        result = decision(title, text)
        assert result.relevant, (title, result.reason)
        assert result.category == "Качество товаров и услуг"


def test_recall_161_keeps_confirmed_labour_rights_violation():
    result = decision(
        "На Могилевщине начальник придумал приказ и штрафы для работников",
        "Работодатель издал незаконный приказ и необоснованно удерживал штрафы "
        "из зарплаты работников. Прокуратура отменила приказ и восстановила их права.",
        "Могилев",
    )
    assert result.relevant, result.reason
    assert result.category == "Работа, зарплаты и доходы"


def test_recall_161_keeps_public_transport_capacity_failure():
    result = decision(
        "Почему из Вилейки в Молодечно вместо большого автобуса приехал маленький",
        "Пассажиры жалуются: вместо большого автобуса на регулярный маршрут "
        "подали маленький, мест не хватило, люди ехали стоя.",
        "Молодечно",
    )
    assert result.relevant, result.reason
    assert result.category == "Общественный транспорт"


def test_recall_161_keeps_real_income_and_employment_contraction():
    income = decision(
        "Стали ли жители жить лучше: сравнили рост зарплат и инфляцию",
        "Цены росли быстрее зарплат, реальные доходы жителей снизились, "
        "а покупательная способность стала хуже.",
    )
    employment = decision(
        "В Беларуси снизилось количество занятых в экономике",
        "Белстат сообщил, что число занятых в экономике сократилось, "
        "рабочих мест стало меньше.",
    )
    assert income.relevant, income.reason
    assert employment.relevant, employment.reason
    assert income.category == "Работа, зарплаты и доходы"
    assert employment.category == "Работа, зарплаты и доходы"


def test_recall_161_keeps_bounded_telecom_complaint():
    result = decision(
        "Белорус возмутился сообщением оператора по поводу мобильного трафика",
        "Абонент оспаривает ошибочное списание мобильного трафика. Оператор связи "
        "не решил проблему после обращения клиента.",
    )
    assert result.relevant, result.reason
    assert result.category == "Связь, интернет и телевидение"


def test_recall_161_does_not_open_broad_false_positive_paths():
    cases = (
        (
            "Geely отзывает автомобили из-за бракованных датчиков",
            "Регулятор Китая объявил глобальную отзывную кампанию для машин, "
            "выпущенных китайскими заводами.",
        ),
        (
            "Средняя зарплата медицинских работников выросла на 15%",
            "Министерство сообщило нейтральную статистику средней зарплаты.",
        ),
        (
            "В Минске запустят новый автобусный маршрут",
            "Маршрут начнет работать с сентября. Расписание опубликовано.",
        ),
        (
            "На неделю перекроют улицу для ремонта",
            "Движение планово перекрыли на ремонт до 1 сентября.",
        ),
    )
    for title, text in cases:
        assert not decision(title, text).relevant, title


def test_admission_11_raises_only_persistent_limit_sources():
    expected = {
        "Onlíner": 80,
        "Zerkalo.io": 75,
        "Наша Ніва": 75,
        "NewGrodno": 75,
        # Raised 70->90 on 2026-08-26 report-13 telemetry: clipped_fresh=21
        # (genuinely dated candidates cut by the base limit, not just
        # ambiguous tail). Single-day evidence — re-confirm as persistent
        # over subsequent runs.
        "Белновости": 90,
        "Минская правда": 70,
        "Smartpress.by": 60,
        "CityDog": 60,
        # Raised 65->105 on 2026-08-26 report-13 telemetry: clipped_fresh=36,
        # the largest single-day dated-candidate overflow observed across
        # all sources that day. Single-day evidence — re-confirm as
        # persistent over subsequent runs.
        "Виртуальный Брест": 105,
        "БрестСИТИ": 65,
        "ВГР": 60,
        "Слуцк-Город": 55,
        "MASHEKA": 60,
        "Витебская Весна": 55,
        "M-Media архив": 55,
    }
    limits = SETTINGS["monitor"]["source_candidate_limits"]
    assert {name: limits[name] for name in expected} == expected
    assert SETTINGS["monitor"]["max_candidates_per_run"] == 4000
    assert SETTINGS["monitor"]["per_source_candidate_limit"] == 35
