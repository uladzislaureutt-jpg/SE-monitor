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


def article(
    source_name: str,
    title: str,
    excerpt: str,
    url: str,
    *,
    category: str = "Качество товаров и услуг",
    region: str = "Беларусь",
    signature: str = "",
) -> social_monitor.ArticleResult:
    fingerprint = social_monitor.infer_event_fingerprint(title, "", excerpt)
    return social_monitor.ArticleResult(
        source_name=source_name,
        source_type="website",
        country="Беларусь",
        locality="Беларусь",
        priority="A",
        source_language="ru",
        title=title,
        title_generated=False,
        url=url,
        published_at="2026-08-26T12:00:00+00:00",
        category=category,
        subcategory="",
        excerpt=excerpt,
        signal_type="описание конкретной социально-экономической проблемы",
        official_response=False,
        score=10,
        matched_terms="",
        discovered_via="feed:test",
        text_length=len(excerpt),
        event_region=region,
        event_locality=fingerprint.locality,
        event_object=fingerprint.object_label,
        event_problem=fingerprint.problem_label,
        event_signature=signature or fingerprint.signature,
    )


def test_result_integrity_17_rejects_confirmed_report_14_noise():
    cases = (
        (
            "Кто такие программисты 1С и почему они нужны",
            "Справочный материал объясняет особенности профессии и правила учета.",
        ),
        (
            "Кипр разрешил беларусам получать вид на жительство с просроченными паспортами",
            "Власти Кипра упростили выдачу ВНЖ. Нарушений прав или жалоб не установлено.",
        ),
        (
            "Geely отзывает почти 93 тысячи автомобилей из-за бракованных датчиков",
            "Управление по регулированию рынка Китая объявило отзывную кампанию. "
            "Она касается машин, выпущенных и проданных в Китае.",
        ),
        (
            "Белоруска продала за 100 рублей просроченный на семь лет Киндер",
            "Частная занимательная история о старой сладости вызвала обсуждение в соцсетях.",
        ),
        (
            "Турчин: борьба с некачественным импортом будет только ужесточаться",
            "Премьер заявил о намерении правительства усилить контроль импорта. "
            "Конкретных нарушений не выявляли.",
        ),
        (
            "Красиво и аукнется. Чем опасны болотные фотосессии",
            "Специалисты дали бытовые советы любителям фотографироваться на болоте.",
        ),
        (
            "С 26 августа начали действовать новые правила направления в соцучреждения",
            "Министерство нейтрально разъяснило медицинские показания для социальных пансионатов.",
        ),
        (
            "Следственный комитет начал спецпроизводство в отношении двух человек",
            "Процедура применяется по нескольким составам преступлений. Социальной жалобы нет.",
        ),
        (
            "Парень сам утеплил двухэтажный дом, сэкономив 15 000 рублей",
            "Владелец рассказал о материалах, технологии и личной экономии.",
        ),
        (
            "Стали ли полешуки жить лучше? Сравнили рост зарплат и инфляцию",
            "Рост потребительских цен оказался ниже роста зарплат. "
            "Покупательная способность увеличилась и стала выше.",
        ),
        (
            "Какие новости принес сегодняшний день?",
            "В вечернем выпуске сразу несколько историй: штрафы работникам, драка и происшествие.",
        ),
    )
    for title, text in cases:
        result = decision(title, text)
        assert not result.relevant, (title, result.reason)


def test_result_integrity_17_keeps_transport_capacity_complaint_variants():
    cases = (
        (
            "Почему из Вилейки в Молодечно вместо большого автобуса приехал маленький",
            "На регулярный маршрут приехал малый автобус, пассажирам не хватило мест.",
        ),
        (
            "На маршрут прислали автобус меньшей вместимости",
            "Пассажиры жалуются, что вместо обычного автобуса подали маленький.",
        ),
    )
    for title, text in cases:
        result = decision(title, text, "Молодечно")
        assert result.relevant, (title, result.reason)
        assert result.category == "Общественный транспорт"


def test_result_integrity_17_corrects_six_confirmed_categories():
    development = decision(
        "Дольщики Северного Берега пожаловались на балконы",
        "Застройщик жилого комплекса продает одно, а строит другое. "
        "Дольщики заявили претензии к проекту балконов.",
        "Минск",
    )
    healthcare = decision(
        "Разбирались, как пройти осмотр гинеколога",
        "Женщины жалуются на очередь: невозможно записаться к врачу и нет талонов. "
        "Поликлиника объяснила график работы сотрудников.",
        "Молодечно",
    )
    waste = decision(
        "Жительница пожаловалась на заваленную мусором контейнерную площадку",
        "Контейнерная площадка переполнена мусором. После обращения жителей ЖКХ "
        "ответило, как организована работа сотрудников.",
        "Смолевичи",
    )
    inactive = decision(
        "Нетунеядцы в Беларуси: сколько их на самом деле — свежие данные",
        "Белстат опубликовал количество граждан, не занятых в экономике. "
        "Статус влияет на полную оплату коммунальных услуг.",
    )
    regulation = decision(
        "Беларуских товаров в магазинах должно стать больше. Но что с качеством?",
        "МАРТ подписал постановление и ввел норматив наличия белорусских товаров "
        "в торговом ассортименте. Покупатели обсуждают качество продукции.",
    )
    communal = decision(
        "Глава района провел выездной прием граждан в сельсовете",
        "На проблемы с отоплением в доме пожаловалась жительница. "
        "Она просила заменить батарейные краны силами коммунальщиков. ЖКХ подготовило ответ.",
    )
    assert development.relevant and development.category == "Строительство и новостройки"
    assert healthcare.relevant and healthcare.category == "Здравоохранение"
    assert waste.relevant and waste.category == "Дороги и благоустройство"
    assert inactive.relevant and inactive.category == "Работа, зарплаты и доходы"
    assert regulation.relevant and regulation.category == "Законы, права и общественное регулирование"
    assert communal.relevant and communal.category == "ЖКХ и состояние жилья"


def test_result_integrity_keeps_outpatient_queue_complaint_when_body_evidence_is_late():
    decision_result = decision(
        "«Квест: пройди медкомиссию»: жительница Молодечно пожаловалась на длинные очереди к гинекологу — в больнице ответили",
        " ".join([
            "Больница опубликовала общую справку о работе учреждения."
            for _ in range(8)
        ]) + " Пациентка пожаловалась, что попасть к гинекологу невозможно из-за длинной очереди.",
        "Молодечно",
    )
    assert decision_result.relevant, decision_result.reason
    assert decision_result.category == "Здравоохранение"


def test_event_integrity_17_consolidates_seven_report_14_event_groups():
    rows = [
        article("A", "Кипр разрешил ВНЖ с просроченными паспортами", "Кипр выдает вид на жительство беларусам с просроченными паспортами.", "https://a/1", signature="cyprus-a"),
        article("B", "Кипр начал выдавать виды на жительство", "Беларусы на Кипре могут получить вид на жительство с просроченным паспортом.", "https://b/1", signature="cyprus-b"),
        article("A", "Запретили шоколад Чаржед без сахара", "Шоколад ЧАРЖЕД признан опасным продуктом и снят с продажи.", "https://a/2", signature="chocolate-a"),
        article("B", "В Беларуси забанили российскую шоколадку", "Госстандарт запретил шоколад Charged как небезопасный товар.", "https://b/2", signature="chocolate-b"),
        article("A", "Белорусский спрей для ног запретили", "В продукции, спрее для ног, обнаружили борную кислоту.", "https://a/3", signature="spray-a"),
        article("B", "Госстандарт запретил спрей для ног", "Спрей для ног сняли с продажи из-за борной кислоты.", "https://b/3", signature="spray-b"),
        article("A", "Запретили натуральный какао-порошок", "Какао-порошок не соответствует требованиям и признан опасным продуктом.", "https://a/4", signature="cocoa-a"),
        article("B", "Госстандарт запретил популярный какао порошок", "Небезопасный какао-порошок изъяли из продажи.", "https://b/4", signature="cocoa-b"),
        article("A", "Число занятых в экономике снизилось", "Количество занятых в экономике уменьшилось на 400 человек.", "https://a/5", category="Работа, зарплаты и доходы", signature="jobs-a"),
        article("B", "В Беларуси стало меньше занятых", "Число занятых в экономике сократилось на 400 человек.", "https://b/5", category="Работа, зарплаты и доходы", signature="jobs-b"),
        article("A", "КГК предотвратил траты при строительстве моста", "КГК выявил завышенные расходы на мост и путепровод: предотвращены траты 2,3 млн рублей.", "https://a/6", category="Строительство и новостройки", region="Брестская область", signature="bridge-a"),
        article("B", "КГК предотвратил излишние расходы на путепровод", "Комитет госконтроля нашел завышенную стоимость строительства моста на 2,3 млн рублей.", "https://b/6", category="Строительство и новостройки", region="Брестская область", signature="bridge-b"),
        article("A", "Припять сильно обмелела", "Уровень воды в реке Припять установил антирекорд.", "https://a/7", category="Экология и санитарные проблемы", region="Гомельская область", signature="river-a"),
        article("B", "Припять можно перейти пешком", "Река Припять обмелела до антирекордного уровня воды.", "https://b/7", category="Земля, водоёмы и доступ к природе", region="Гомельская область", signature="river-b"),
    ]
    consolidated = social_monitor.deduplicate_results(rows)
    assert len(consolidated) == 7
    assert social_monitor.represented_publication_count(consolidated) == 14


def test_event_integrity_17_does_not_merge_different_rivers_or_products():
    pripyat = article("A", "Припять обмелела", "Река Припять достигла низкого уровня воды.", "https://a/river", category="Экология и санитарные проблемы", region="Гомельская область")
    dnieper = article("B", "Днепр обмелел", "Река Днепр достигла низкого уровня воды.", "https://b/river", category="Экология и санитарные проблемы", region="Гомельская область")
    cocoa = article("A", "Запретили какао-порошок", "Опасный какао-порошок сняли с продажи.", "https://a/product")
    spray = article("B", "Запретили спрей для ног", "Спрей для ног содержит борную кислоту.", "https://b/product")
    assert len(social_monitor.deduplicate_results([pripyat, dnieper])) == 2
    assert len(social_monitor.deduplicate_results([cocoa, spray])) == 2
