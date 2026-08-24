from pathlib import Path

import social_monitor


SETTINGS = social_monitor.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source(locality: str = "Минск") -> social_monitor.Source:
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


def decision(title: str, text: str, locality: str = "Минск"):
    return social_monitor.evaluate_relevance(
        title, "", text, source(locality), SETTINGS
    )


def test_result_integrity_14_rejects_report_5_54_noise():
    cases = (
        (
            "Японец оставил семилетнего сына одного на горе Фудзи ради восхождения",
            "Мальчик пожаловался отцу на усталость и остался на скамейке.",
        ),
        (
            "В белорусские магазины завезли нашу цветную капусту и брокколи",
            "Торговая сеть получила пробный урожай. Предприятие ждет отклик покупателей.",
        ),
        (
            'В Пинске появилась "телефонная будка" для обмена книгами',
            "Точку буккроссинга оформляют. На очереди подключение подсветки.",
        ),
        (
            "Змея Ксюша. Пинский биолог приносит на уроки необычный экспонат",
            "Ребенок жалуется, что не понимает тему урока. Учитель использует наглядность.",
        ),
        (
            "Мир на пороге песчаного кризиса",
            "Расскажем, почему песок становится дефицитным ресурсом во всем мире.",
        ),
        (
            "В Гродно на выходные закрыли движение на кольце",
            "Движение будет запрещено до 05:00 из-за ремонта асфальта.",
        ),
        (
            "Водители поплатились правами и рублями за маневр на дороге",
            "Двое водителей привлечены к ответственности за аварийную обстановку.",
        ),
        (
            "Семья купила заброшенный хутор и вернула его к жизни",
            "Семья восстановила старую школу и живет на берегу реки.",
        ),
        (
            "Платья открыли жительнице путь к собственному бренду",
            "В девяностые в магазинах был дефицит. Теперь она развивает свое дело.",
        ),
    )
    for title, text in cases:
        assert not decision(title, text).relevant, title


def test_result_integrity_14_keeps_public_problem_controls():
    cases = (
        (
            "У гомельчанки не работает лифт, а обращения не помогают",
            "Жительница жалуется: лифт ломается по два раза в день.",
        ),
        (
            "Жители деревни жалуются на разбитую дорогу и сами латают ее кирпичом",
            "Жители 26 домов подписали коллективное обращение, но срок ремонта разбитой дороги не назван.",
        ),
        (
            "Жители пожаловались на некошеную траву и заросший двор",
            "Жители требуют привести двор и территорию в порядок.",
        ),
        (
            "В Смолевичах из-за ветра нет света и воды",
            "Жители остались без электричества и водоснабжения, аварийные бригады работают.",
        ),
        (
            "Люди занимают очередь к автолавке с шести утра",
            "Пенсионеры ждут продукты несколько часов, части покупателей товара не хватает.",
        ),
    )
    for title, text in cases:
        assert decision(title, text).relevant, title


def test_result_integrity_14_keeps_belarus_financial_access_restriction():
    result = decision(
        "Для белорусов ввели новые финансовые ограничения",
        "Payoneer изменил правила для белорусских пользователей. "
        "Долларовые банковские счета в Беларуси больше не поддерживаются, "
        "а вывод средств на счета банков ограничен.",
        "Беларусь",
    )
    assert result.relevant


def test_result_integrity_14_assigns_unpaid_final_pay_to_work():
    result = decision(
        "В компании почти год не выплачивали расчет уволенному сотруднику",
        "Мужчина уволился, но окончательный расчет при увольнении не получил.",
        "Беларусь",
    )
    assert result.relevant
    assert result.category == "Работа, зарплаты и доходы"


def article(
    source_name: str,
    locality: str,
    title: str,
    excerpt: str,
    url: str,
    category: str,
) -> social_monitor.ArticleResult:
    return social_monitor.ArticleResult(
        source_name=source_name,
        source_type="website",
        country="Беларусь",
        locality=locality,
        priority="A",
        source_language="ru",
        title=title,
        title_generated=False,
        url=url,
        published_at="2026-08-24T08:00:00+00:00",
        category=category,
        subcategory="",
        excerpt=excerpt,
        signal_type="описание конкретной социально-экономической проблемы",
        official_response=False,
        score=10,
        matched_terms="",
        discovered_via="feed:test",
        text_length=len(excerpt),
    )


def test_result_integrity_14_merges_national_rewrites_from_regional_outlets():
    shared = (
        "Payoneer изменил правила для белорусских пользователей. "
        "Долларовые счета в Беларуси больше не поддерживаются. "
        "Ограничен вывод средств на счета белорусских банков."
    )
    results = social_monitor.deduplicate_results([
        article(
            "Источник А", "Гродно",
            "Для белорусов ввели новые финансовые ограничения",
            shared, "https://example.by/a",
            "Законы, права и общественное регулирование",
        ),
        article(
            "Источник Б", "Брест",
            "Белорусы столкнулись с очередными финансовыми ограничениями",
            shared, "https://example.org/b",
            "Законы, права и общественное регулирование",
        ),
    ])
    assert len(results) == 1
    assert len(results[0].related_coverage) == 1


def test_result_integrity_14_does_not_merge_local_problems_across_cities():
    results = social_monitor.deduplicate_results([
        article(
            "Источник А", "Минск",
            "Жители жалуются на кнопки открытия дверей в автобусах",
            "Пассажиры жалуются на систему адресного открытия дверей.",
            "https://example.by/minsk", "Общественный транспорт",
        ),
        article(
            "Источник Б", "Гродно",
            "Жители жалуются на кнопки открытия дверей в автобусах",
            "Пассажиры жалуются на систему адресного открытия дверей.",
            "https://example.by/grodno", "Общественный транспорт",
        ),
    ])
    assert len(results) == 2
