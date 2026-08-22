import copy
import datetime as dt

import social_monitor
from social_monitor import (
    ArticleExtraction,
    Candidate,
    Source,
    canonicalize_url,
    detect_language,
    evaluate_relevance,
    exact_excerpt,
    extract_article_from_html,
    extract_date_from_url,
    email_recipients,
    local_now,
    process_candidate,
    result_integrity_genre_rejection,
)

UTC = dt.timezone.utc

SETTINGS = social_monitor.load_settings(
    social_monitor.Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source(region: str = "Минск", locality: str = "Минск", media_type: str = "website") -> Source:
    return Source(
        enabled=True,
        country=region,
        country_code="BY-HM",
        locality=locality,
        rank=1,
        priority="A",
        name="Тестовое СМИ",
        media_type=media_type,
        domain="example.com" if media_type == "website" else "t.me",
        start_url="https://example.com" if media_type == "website" else "https://t.me/test",
        language="ru",
        adapter="standard" if media_type == "website" else "telegram",
    )


def candidate(*, title: str, summary: str = "", url: str = "https://example.com/2026/07/30/item", published_at: str = "", discovered_via: str = "feed:https://example.com/rss", inline_text: str = "") -> Candidate:
    return Candidate(
        source=source(), url=url, title=title, summary=summary,
        published_at=published_at, discovered_via=discovered_via,
        inline_text=inline_text,
    )


def decision(title: str, text: str, src: Source | None = None):
    return evaluate_relevance(title, "", text, src or source(), SETTINGS)


def test_canonicalize_url_removes_tracking():
    assert canonicalize_url("https://example.com/a/b/?utm_source=x&id=7#x") == "https://example.com/a/b?id=7"


def test_road_complaint_is_included():
    result = decision(
        "Жители улицы жалуются на разбитую дорогу",
        "Жители Минска несколько лет не могут добиться ремонта. Дорога покрыта глубокими ямами.",
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"
    assert result.signal_type.startswith("жалоба")


def test_belarusian_complaint_is_included():
    result = decision(
        "Жыхары скардзяцца на адсутнасць вулічнага асвятлення",
        "У вёсцы няма святла на вуліцы, жыхары неаднаразова звярталіся ў райвыканкам.",
        source("Витебская область", "Орша"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_neutral_repair_announcement_is_excluded():
    result = decision(
        "В районе отремонтируют три дороги",
        "Работы начнутся в августе. На проект выделено финансирование.",
    )
    assert not result.relevant


def test_plain_price_statistics_are_excluded():
    result = decision(
        "Белстат сообщил об изменении цен",
        "Цены на продукты выросли на 0,2 процента за месяц.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_high_prices_are_included():
    result = decision(
        "Покупатели жалуются на резко подорожавшие продукты",
        "Жители Минска говорят, что высокие цены на овощи стали для них проблемой.",
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Цены, торговля и дефицит"


def test_product_quality_finding_is_included_without_resident_quote():
    result = decision(
        "КГК выявил нарушения качества продуктов",
        "В магазине продавали просроченные продукты ненадлежащего качества. Предписано устранить нарушения.",
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Качество товаров и услуг"
    assert "критический" in result.signal_type


def test_political_material_is_excluded():
    result = decision(
        "Оппозиция критикует Лукашенко из-за цен",
        "Политики обсуждают санкции и выборы в Беларуси.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "полит" in result.reason


def test_foreign_social_story_is_excluded():
    result = decision(
        "Жители Москвы жалуются на очереди в поликлиниках",
        "В России пациенты не могут записаться к врачу.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "иностран" in result.reason


def test_crime_story_is_excluded():
    result = decision(
        "В Минске задержан мошенник в магазине",
        "Покупатель украл продукты и был задержан милицией.",
    )
    assert not result.relevant


def test_denial_of_problem_is_not_negative_signal():
    result = decision(
        "В городе нет проблем с водой",
        "Коммунальные службы сообщили, что перебоев нет и жалоб нет.",
    )
    assert not result.relevant


def test_result_integrity_rejects_neutral_instructional_genre():
    reason = result_integrity_genre_rejection(
        "Как правильно оформить медсправку для ребенка",
        "Рассказываем, какие документы необходимо принести.",
    )
    assert "справочный" in reason


def test_result_integrity_keeps_instruction_with_concrete_service_failure():
    reason = result_integrity_genre_rejection(
        "Куда обращаться, если не работает уличное освещение",
        "Жители деревни несколько недель остаются без света.",
    )
    assert reason == ""


def test_result_integrity_13_rejects_control_run_editorial_genres():
    cases = (
        (
            "В Гродно девушки перепутали лежачий полицейский с переходом — видео завирусилось",
            "Ролик носит исключительно поучительный характер, претензий никто не имеет.",
        ),
        (
            "Открыли для путешествия забытую усадьбу и нашли новый символ",
            "Во время мотопутешествия авторы осмотрели старинный дворец.",
        ),
        (
            "Станислав Бородавко, бывший председатель горисполкома: правильный путь выбрал",
            "Герой рассказал о саде, огороде и семейном отдыхе.",
        ),
        (
            "Мужчина сдавал чужую квартиру: жильцы рассказали, чем все закончилось",
            "Это версия участницы частного конфликта без независимых подтверждений.",
        ),
        (
            "Кому не стоит есть яблоки с кожурой — врач рассказал",
            "Рекомендации врача по питанию для людей с заболеваниями пищеварения.",
        ),
        (
            "Белорускам до 40 лет будут проводить новый скрининг — что смогут выявить",
            "Меры вошли в профилактический план и помогут выявлять болезни раньше.",
        ),
        (
            "Мировое здравоохранение испытывает нехватку миллионов медиков",
            "Глобальное исследование оценивает число работников во всем мире.",
        ),
        (
            "От цен волосы встают дыбом",
            "Рассказываем, сколько стоит пересадка волос методом FUE.",
        ),
        (
            "А что, если бы вдруг так обмелели и другие водоемы Беларуси",
            "Мы попросили нейросеть представить, как бы это могло выглядеть.",
        ),
        (
            "Что сегодня по новостям?",
            "В вечернем выпуске собраны несколько несвязанных сюжетов.",
        ),
        (
            "Забытые кладбища города: два кладбища в одном месте",
            "Исторический рассказ о том, кого хоронили здесь в давние времена.",
        ),
        (
            "Банковские карты: как подобрать и оформить карту онлайн",
            "Справочное сравнение платежных систем и валют счетов.",
        ),
        (
            "Можно ли получить медсправку без очередей: как избежать ошибок",
            "Педиатр объясняет порядок диспансеризации и список врачей.",
        ),
    )
    for title, lead in cases:
        reason = result_integrity_genre_rejection(
            title,
            lead,
            domestic_scope=False,
        )
        assert reason, title


def test_result_integrity_13_keeps_explainer_with_bound_resident_evidence():
    reason = result_integrity_genre_rejection(
        "Могут ли не пустить в магазин до закрытия? Разбираем закон",
        "Жительница возмутилась: магазин закрыл двери раньше времени.",
        resident_explicit=True,
        lead_bound_evidence=True,
        domestic_scope=True,
    )
    assert reason == ""


def test_evidence_binding_rejects_problem_found_only_deep_in_article():
    result = decision(
        "Городские новости и полезная информация недели",
        (
            "Редакция подготовила обзор городских событий. "
            "Сначала автор рассказывает о погоде. "
            "Затем перечисляет культурные события. "
            "Отдельный раздел посвящен истории района. "
            "Также приведены советы для туристов. "
            "После этого опубликовано интервью с предпринимателем. "
            "В конце дана спортивная хроника. "
            "Только глубоко в тексте жители жалуются на разбитую дорогу и глубокие ямы."
        ),
    )
    assert not result.relevant
    assert "Evidence Binding" in result.reason


def test_core351_keeps_bound_consumer_redress_dispute():
    result = decision(
        "Белоруска купила ботинки, поносила пару часов и решила вернуть. В магазине отказали — кто прав?",
        (
            "Покупательница попыталась вернуть обувь в магазин, однако получила отказ. "
            "Продавец отказалась принимать товар и сначала не выдала бланк заявления. "
            "Девушка направила претензию на юридический адрес магазина."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Качество товаров и услуг"


def test_core351_keeps_broken_public_waste_container_complaint():
    result = decision(
        "В Витебске попросили закрывать крышку мусорного бака, но есть нюанс",
        (
            "Коммунальщики попросили жителей закрывать мусорный контейнер. "
            "У старого металлического бака крышки нет, поэтому выполнить требование невозможно. "
            "Жители сообщают, что птицы разносят мусор по двору, и требуют заменить контейнер."
        ),
        source("Витебская область", "Витебск"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_core351_public_consultation_accountability_overrides_war_marker():
    result = decision(
        "В Калинковичах не рассказали об итогах общественного обсуждения переноса танка-памятника",
        (
            "Райисполком не уведомил об итогах общественных обсуждений проекта. "
            "Идея разместить в парке военную технику вызвала массовую критику жителей. "
            "После завершения обсуждения решение не принято, а чиновница дала расплывчатый ответ."
        ),
        source("Гомельская область", "Калинковичи"),
    )
    assert result.relevant
    assert result.category == "Законы, права и общественное регулирование"


def test_core351_rejects_private_tenancy_deposit_dispute():
    result = decision(
        "Хозяйка квартиры не хотела возвращать залог за якобы убитую квартиру. Арендатор пошел в суд",
        (
            "Хозяйка заявила, что квартира испорчена, и отказалась возвращать залог. "
            "Арендатор считал повреждения естественным износом и обратился в суд."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "частный спор найма" in result.reason


def test_core351_rejects_single_driver_passenger_insult():
    result = decision(
        "Пассажир пожаловался на оскорбления водителя маршрутки. Установлена запись разговора",
        (
            "Житель обратился в правоохранительные органы из-за единичного оскорбления. "
            "Экспертиза установила, что водитель дал негативную оценку пассажиру в ненормативной форме."
        ),
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "межличностный конфликт" in result.reason


def test_core351_keeps_systemic_public_transport_complaint():
    result = decision(
        "Пассажиры жалуются: маршрутка регулярно не приезжает по расписанию",
        (
            "Жители несколько недель не могут дождаться транспорта по вечерам. "
            "Перевозчик пока не восстановил рейсы и не объяснил постоянные перебои."
        ),
        source("Брестская область", "Брест"),
    )
    assert result.relevant
    assert result.category == "Общественный транспорт"


def test_core36_editorial_intent_gate_rejects_report52_neutral_genres():
    cases = (
        (
            "Врач-офтальмолог рассказала, как распознать болезни глаз у ребенка",
            "Врач перечислила симптомы и дала рекомендации по профилактике.",
        ),
        (
            "Почему нам так интересна чужая личная жизнь и чужие конфликты? Рассказывает психологиня",
            "Психотерапевт объяснила, почему людям не хватает внутренней опоры.",
        ),
        (
            "В Беларуси женщины смогут проверить риски бесплодия во время диспансеризации",
            "Плановая диспансеризация будет включать новый профилактический скрининг.",
        ),
        (
            "На каких улицах Слуцка не будет электричества с 25 по 28 августа",
            "Электроснабжение будет отсутствовать из-за плановых работ на линии.",
        ),
        (
            "Когда и куда обращаться за пособием по уходу за ребенком до 3 лет",
            "Разъяснен порядок назначения пособия и перечень необходимых документов.",
        ),
        (
            "Беларуска выбросила свидетельство о рождении. Теперь его не хватает для получения польского гражданства",
            "Частная история восстановления документа, который владелица уничтожила сама.",
        ),
        (
            "Денис Колесень обсудил вопросы экологии с работниками предприятия",
            "На встрече перечислили общие вопросы экологии и благоустройства.",
        ),
        (
            "Журналисты привыкли, что в редакцию чаще всего обращаются с проблемой",
            "Но в этом письме нет ни одной жалобы и нет самостоятельной проблемы.",
        ),
        (
            "Чаму ў Магілёве так шмат львоў? Гараджане задаюцца пытаннем",
            "Жителям не нравится повторяющаяся городская символика, но объекты исправны.",
        ),
        (
            "Головная боль во время магнитной бури – невролог назвала симптом, который нельзя игнорировать",
            "Врач дала рекомендации и рассказала, когда нужно принять обезболивающее.",
        ),
        (
            "Минобороны аккредитовало нового военного атташе Германии",
            "Состоялась дипломатическая процедура и обсуждение отношений двух стран.",
        ),
    )
    for title, lead in cases:
        reason = result_integrity_genre_rejection(
            title,
            lead,
            lead_bound_evidence=True,
            domestic_scope=True,
        )
        assert reason, title


def test_core36_editorial_intent_gate_keeps_verified_public_problem():
    assert result_integrity_genre_rejection(
        "Врач объяснил, почему пациенты месяцами не могут попасть на прием",
        "Жители жалуются на постоянные очереди и отсутствие записи.",
        title_explicit=True,
        resident_explicit=True,
        persistence=True,
    ) == ""
    assert result_integrity_genre_rejection(
        "Председатель обсудил вопросы водоснабжения с работниками предприятия",
        "Проверка выявила нарушение, жители нескольких домов остаются без воды.",
        resident_explicit=True,
        lead_findings=True,
    ) == ""
    assert result_integrity_genre_rejection(
        "В Беларуси расширят скрининг после жалоб на недоступность обследований",
        "Пациентки не могут пройти обследование, проблема сохраняется несколько месяцев.",
        title_explicit=True,
        persistence=True,
    ) == ""


def test_core36_full_relevance_rejects_report52_advice_and_notice_noise():
    cases = (
        (
            "Врач-офтальмолог рассказала, как распознать болезни глаз у ребенка",
            "Врач городской больницы перечислила симптомы и посоветовала смотреть на деревья.",
        ),
        (
            "На каких улицах Слуцка не будет электричества с 25 по 28 августа",
            "Электроснабжение будет отсутствовать из-за плановых работ на линии.",
        ),
        (
            "Когда и куда обращаться за пособием по уходу за ребенком до 3 лет",
            "Соцзащита разъяснила сроки назначения пособия неработающим родителям.",
        ),
        (
            "Денис Колесень обсудил вопросы экологии с работниками предприятия",
            "На встрече говорили про мусор, воду, дороги и строительство нового жилья.",
        ),
        (
            "Головная боль во время магнитной бури – невролог назвала симптом, который нельзя игнорировать",
            "Врач больницы рассказала о головной боли и дала медицинские рекомендации.",
        ),
    )
    for title, text in cases:
        result = decision(title, text, source("Беларусь", "Беларусь"))
        assert not result.relevant, title


def test_core36_keeps_systemic_doctor_complaints():
    result = decision(
        "О грубости и бестактности врачей этой специальности уже ходят страшилки",
        (
            "Жительницы Гомельской области рассказали о многочисленных случаях грубости. "
            "Посты с жалобами на врачей регулярно собирают сотни комментариев. "
            "Пациентки сообщают как минимум об испорченном настроении, "
            "а как максимум — о психологических травмах после приема."
        ),
        source("Гомельская область", "Гомель"),
    )
    assert result.relevant


def test_result_integrity_rejects_routine_forest_restriction():
    result = decision(
        "Ситуация с лесами в Беларуси. Запрет на посещение действует в 27 районах",
        "Из-за высокого класса пожарной опасности действует ограничение доступа. "
        "Ведомство опубликовало интерактивную карту.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "Result Integrity" in result.reason


def test_result_integrity_rejects_currency_bulletin():
    result = decision(
        "Доллар и евро в Беларуси резко подорожали. Курсы валют на 17 августа",
        "Банк опубликовал новые официальные курсы валют.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_result_integrity_rejects_single_entertainment_incident():
    result = decision(
        "Каскадер получил травмы на фестивале",
        "Во время трюка произошел несчастный случай. Пострадавшего доставили в больницу.",
        source("Гродненская область", "Лида"),
    )
    assert not result.relevant


def test_result_integrity_rejects_foreign_demography_story():
    result = decision(
        "В Нидерландах значительно снизилась рождаемость",
        "Исследователи обсуждают демографические причины и доходы семей.",
        source("Брестская область", "Брест"),
    )
    assert not result.relevant


def test_result_integrity_rejects_corporate_repair_story():
    result = decision(
        "Крупнейший завод в Беларуси уйдет на ремонт – пострадать может Россия",
        "Предприятие остановит линию на плановый ремонт. Возможен дефицит экспортной продукции.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_result_integrity_keeps_direct_infrastructure_complaint():
    result = decision(
        "Куда обращаться, если не работает уличное освещение в деревне",
        "Жители жалуются, что на улице несколько недель нет света. "
        "Они неоднократно обращались в коммунальную службу.",
        source("Гродненская область", "Гродно"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_product_contamination_is_classified_as_quality():
    result = decision(
        "В белорусской колбасе нашли кишечную палочку",
        "Проверка выявила в продукте кишечную палочку. "
        "Товар признан небезопасным и снят с продажи.",
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Качество товаров и услуг"


def test_complaint_negation_after_verb_is_not_public_signal():
    result = decision(
        "Проверили две шашлычные в Минске",
        "На размер порции мы жаловаться не собираемся. "
        "Посетителям предлагают мясо и другие продукты.",
    )
    assert not result.relevant


def test_official_response_is_detected_and_selected():
    text = (
        "Жители Минска жалуются на отсутствие горячей воды. "
        "Проблема сохраняется несколько недель. "
        "В исполкоме в ответ сообщили, что ремонт завершат в пятницу. "
        "После этого подачу воды обещают восстановить."
    )
    dec = decision("Дом несколько недель остаётся без горячей воды", text)
    assert dec.relevant and dec.official_response
    excerpt = exact_excerpt(text, "", dec, SETTINGS)
    assert "в ответ сообщили" in excerpt


def test_excerpt_has_no_more_than_seven_sentences_and_keeps_order():
    text = " ".join([
        "Жители Минска жалуются на разбитую дорогу.",
        "Ямы появились после зимы.",
        "Автомобилисты вынуждены объезжать опасный участок.",
        "Проблема сохраняется несколько месяцев.",
        "Люди обращались в коммунальные службы.",
        "В исполкоме ответили, что ремонт включён в план.",
        "Работы обещают начать в августе.",
        "Дополнительное обследование проведут позже.",
        "Соседняя улица в публикации не обсуждается.",
    ])
    dec = decision("Жители жалуются на разбитую дорогу", text)
    excerpt = exact_excerpt(text, "", dec, SETTINGS)
    assert len(social_monitor.split_sentences(excerpt.replace("[…]", ""))) <= 7
    assert excerpt.index("Жители") < excerpt.index("исполкоме")


def test_exact_excerpt_uses_ellipsis_for_non_contiguous_sentences():
    text = (
        "Жители жалуются на отсутствие воды. "
        "Первый второстепенный комментарий редакции. "
        "Второй второстепенный комментарий редакции. "
        "В исполкоме в ответ сообщили, что аварию устранят завтра."
    )
    dec = decision("Жители жалуются на отсутствие воды", text)
    excerpt = exact_excerpt(text, "", dec, SETTINGS)
    assert "Жители жалуются" in excerpt
    assert "ответ сообщили" in excerpt


def test_process_candidate_uses_exact_title_and_excerpt(monkeypatch):
    item = candidate(title="Карточка новости")
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda candidate, settings: ArticleExtraction(
            title="Жители Минска жалуются на разбитую дорогу",
            text="Жители несколько лет не могут добиться ремонта дороги. На улице глубокие ямы.",
            published_at="2026-07-30T07:00:00+00:00",
            date_source="json-ld",
        ),
    )
    result = process_candidate(item, SETTINGS, dt.datetime(2026, 7, 29, tzinfo=UTC))
    assert result is not None
    assert result.title == "Жители Минска жалуются на разбитую дорогу"
    assert "не могут добиться" in result.excerpt


def test_old_article_is_rejected(monkeypatch):
    item = candidate(title="Жители жалуются на разбитую дорогу")
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda candidate, settings: ArticleExtraction(
            title=candidate.title,
            text="Жители Минска жалуются на глубокие ямы на дороге.",
            published_at="2026-07-01T07:00:00+00:00",
        ),
    )
    assert process_candidate(item, SETTINGS, dt.datetime(2026, 7, 29, tzinfo=UTC)) is None


def test_undated_sitemap_requires_strong_title(monkeypatch):
    item = candidate(
        title="Городские новости",
        discovered_via="sitemap:https://example.com/sitemap.xml",
        url="https://example.com/news/undated",
    )
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda candidate, settings: ArticleExtraction(
            title=candidate.title,
            text="Жители Минска жалуются на отсутствие воды в доме.",
        ),
    )
    assert process_candidate(item, SETTINGS, dt.datetime(2026, 7, 29, tzinfo=UTC)) is None


def test_undated_sitemap_with_strong_title_is_kept(monkeypatch):
    item = candidate(
        title="Жители жалуются на отсутствие воды",
        discovered_via="sitemap:https://example.com/sitemap.xml",
        url="https://example.com/news/undated",
    )
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda candidate, settings: ArticleExtraction(
            title=candidate.title,
            text="Жители Минска жалуются на отсутствие воды в доме.",
        ),
    )
    assert process_candidate(item, SETTINGS, dt.datetime(2026, 7, 29, tzinfo=UTC)) is not None


def test_related_card_is_removed_from_article():
    item = candidate(title="Жители жалуются на воду")
    html = """
    <html><head><meta property="og:title" content="Жители жалуются на качество воды"></head>
    <body><article><div class="article-content">
      <p>Жители Минска жалуются на плохое качество питьевой воды.</p>
      <p>Вода имеет неприятный запах уже несколько дней.</p>
      <div class="related"><p>Читайте также: Лукашенко выступил на совещании.</p></div>
    </div></article></body></html>
    """
    extracted = extract_article_from_html(item, html)
    assert "питьевой воды" in extracted.text
    assert "Лукашенко" not in extracted.text


def test_json_ld_title_and_date_are_extracted():
    item = candidate(title="Новости")
    html = """
    <html><head><script type="application/ld+json">
    {"@type":"NewsArticle","headline":"Жители жалуются на разбитую дорогу","datePublished":"2026-07-30T08:00:00Z","articleBody":"Жители Минска жалуются на разбитую дорогу. На улице глубокие ямы."}
    </script></head><body></body></html>
    """
    extracted = extract_article_from_html(item, html)
    assert extracted.title == "Жители жалуются на разбитую дорогу"
    assert extracted.published_at == "2026-07-30T08:00:00+00:00"


def test_telegram_inline_text_needs_no_http(monkeypatch):
    item = Candidate(
        source=source(media_type="telegram"),
        url="https://t.me/test/123",
        title="Жители жалуются на отсутствие освещения",
        published_at="2026-07-30T08:00:00+00:00",
        discovered_via="telegram:https://t.me/s/test",
        inline_text="Жители Минска жалуются: на улице нет освещения уже несколько месяцев.",
        title_generated=True,
    )
    monkeypatch.setattr(social_monitor.HttpClient, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HTTP не нужен")))
    result = process_candidate(item, SETTINGS, dt.datetime(2026, 7, 29, tzinfo=UTC))
    assert result is not None and result.title_generated
    assert result.url.endswith("/123")


def test_language_detection():
    assert detect_language("Жыхары скардзяцца, што на вуліцы няма святла.", "ru") == "be"
    assert detect_language("Жители жалуются, что на улице нет света.", "be") == "ru"


def test_extract_date_from_url():
    assert extract_date_from_url("https://example.com/2026/07/30/item") == dt.datetime(2026, 7, 30, tzinfo=UTC)


def test_default_email_recipient(monkeypatch):
    monkeypatch.delenv("REPORT_TO", raising=False)
    assert email_recipients({"report": {"email_recipients": ["vladreuth@gmail.com"]}}) == ["vladreuth@gmail.com"]


def test_additional_email_recipient(monkeypatch):
    monkeypatch.setenv("REPORT_TO", "second@example.com, vladreuth@gmail.com")
    assert email_recipients({"report": {"email_recipients": ["vladreuth@gmail.com"]}}) == ["vladreuth@gmail.com", "second@example.com"]


def test_minsk_timezone():
    assert local_now({"monitor": {"timezone": "Europe/Minsk"}}).utcoffset().total_seconds() == 10800


def test_direct_complaint_stem_is_recognized():
    result = decision(
        "Жители жалуются на очереди в поликлинике",
        "Пациенты не могут записаться к врачу.",
    )
    assert result.relevant
    assert result.signal_type.startswith("жалоба")


def test_expensive_product_does_not_trigger_road_category():
    result = decision(
        "Покупатели жалуются на слишком дорогой товар",
        "В магазине резко подорожали продукты, и жители недовольны высокими ценами.",
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Цены, торговля и дефицит"


def test_public_telegram_preview_is_parsed(monkeypatch):
    html = """
    <html><body>
      <div class="tgme_widget_message_wrap">
        <div class="tgme_widget_message" data-post="testchannel/42"></div>
        <div class="tgme_widget_message_text">Жители Минска жалуются на отсутствие уличного освещения.</div>
        <time datetime="2026-07-30T08:00:00+00:00"></time>
      </div>
    </body></html>
    """.encode("utf-8")

    class Response:
        content = html
        status_code = 200
        url = "https://t.me/s/testchannel"

    client = social_monitor.HttpClient(SETTINGS)
    monkeypatch.setattr(client, "get", lambda *args, **kwargs: Response())
    items = social_monitor.collect_from_telegram(
        source(media_type="telegram"),
        client,
        dt.datetime(2026, 7, 29, tzinfo=UTC),
        10,
    )
    assert len(items) == 1
    assert items[0].url == "https://t.me/testchannel/42"
    assert items[0].title_generated is True
    assert "освещения" in items[0].inline_text


def test_config_contains_45_enabled_sources_without_stale_vitebsk_duplicate():
    loaded = social_monitor.load_sources(
        social_monitor.Path(__file__).resolve().parents[1] / "config" / "sources.csv"
    )
    assert len(loaded) == 45
    assert sum(item.media_type == "telegram" for item in loaded) == 12
    assert all(item.name != "Свободный Витебск" for item in loaded)
    assert sum(item.start_url == "https://t.me/vitebsk_info" for item in loaded) == 1
    fallback_by_name = {item.name: item.telegram_url for item in loaded}
    assert fallback_by_name["Наша Ніва"] == "https://t.me/nashaniva"
    assert fallback_by_name["Позірк"] == "https://t.me/pozirkonline"
    assert fallback_by_name["Zerkalo.io"] == "https://t.me/zerkalo_io"


def test_undated_sitemap_is_allowed_after_warmup(monkeypatch):
    item = candidate(
        title="Городские новости",
        discovered_via="sitemap:https://example.com/sitemap.xml",
        url="https://example.com/news/new-undated",
    )
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda candidate, settings: ArticleExtraction(
            title=candidate.title,
            text="Жители Минска жалуются на отсутствие воды в доме.",
        ),
    )
    result = process_candidate(
        item,
        SETTINGS,
        dt.datetime(2026, 7, 29, tzinfo=UTC),
        allow_undated_sitemap=True,
    )
    assert result is not None


def test_word_ocherednoy_does_not_mean_queue():
    result = decision(
        "В городе начнётся очередной ремонт дороги",
        "Работы проведут по утверждённому графику, движение не перекроют.",
    )
    assert not result.relevant


def test_working_hours_word_regime_is_not_political():
    result = decision(
        "Жители жалуются на неудобный режим работы поликлиники",
        "Пациенты Минска не могут записаться к врачу после работы.",
    )
    assert result.relevant
    assert result.category == "Здравоохранение"


def make_result(url: str, locality: str) -> social_monitor.ArticleResult:
    return social_monitor.ArticleResult(
        source_name="Тест", source_type="website", country="Минская область",
        locality=locality, priority="A", source_language="ru",
        title="Жители жалуются на плохую дорогу", title_generated=False,
        url=url, published_at="2026-07-30T08:00:00+00:00",
        category="Дороги и благоустройство", subcategory="дорога",
        excerpt="Жители жалуются на плохую дорогу.",
        signal_type="жалоба жителей или пользователей",
        official_response=False, score=8, matched_terms="дорога, жалу",
        discovered_via="feed:test", text_length=100,
    )


def test_same_generic_title_in_different_places_is_not_deduplicated():
    results = social_monitor.deduplicate_results([
        make_result("https://example.com/a", "Слуцк"),
        make_result("https://example.com/b", "Молодечно"),
    ])
    assert len(results) == 2


def test_region_level_event_fingerprint_is_available_without_locality():
    fingerprint = social_monitor.infer_event_fingerprint(
        "В Минской области жители жалуются на перебои с горячей водой"
    )
    assert fingerprint.region == "Минская область"
    assert fingerprint.locality == ""
    assert fingerprint.signature == "region:минская область|water_supply|outage"


def _product_safety_result(
    source_name: str,
    title: str,
    excerpt: str,
    url: str,
    *,
    language: str = "ru",
    region: str = "Брестская область",
) -> social_monitor.ArticleResult:
    return social_monitor.ArticleResult(
        source_name=source_name,
        source_type="website",
        country=region,
        locality="Беларусь",
        priority="A",
        source_language=language,
        title=title,
        title_generated=False,
        url=url,
        published_at="2026-08-19T10:00:00+00:00",
        category="Качество товаров и услуг",
        subcategory="продукты",
        excerpt=excerpt,
        signal_type="институциональное выявление",
        official_response=True,
        score=12,
        matched_terms="продукты, опасные",
        discovered_via="feed:test",
        text_length=len(excerpt),
        event_region=region,
    )


def test_multilingual_product_safety_rewrites_are_one_event_card():
    results = social_monitor.deduplicate_results([
        _product_safety_result(
            "Onliner",
            "Просроченные мясо и молочка. Из магазинов изъяли более 40 кг продуктов",
            "В продаже обнаружили просроченные продукты без документов о безопасности.",
            "https://onliner.by/a",
        ),
        _product_safety_result(
            "Zerkalo",
            "КГК проверил магазины и снял с продажи более 40 кг опасных продуктов",
            "Нашли просроченные хлебобулочные изделия, молочную и мясную продукцию.",
            "https://zerkalo.io/b",
        ),
        _product_safety_result(
            "Pozirk",
            "КГК изъял 40 кг опасной продукции в магазинах двух районов",
            "В магазинах выявлены нарушения торговли и просроченная пищевая продукция.",
            "https://pozirk.online/c",
        ),
        _product_safety_result(
            "Pozirk",
            "КДК канфіскаваў 40 кг небяспечнай прадукцыі ў крамах двух раёнаў",
            "Устаноўлены факты рэалізацыі пратэрмінванай харчовай прадукцыі.",
            "https://pozirk.online/d",
            language="be",
        ),
    ])
    assert len(results) == 1
    assert {name for name, _url in results[0].related_coverage} == {
        "Pozirk",
        "Zerkalo",
    }
    assert social_monitor.represented_publication_count(results) == 3


def test_semantic_event_match_does_not_cross_regions():
    results = social_monitor.deduplicate_results([
        _product_safety_result(
            "Источник A",
            "Из магазинов изъяли 40 кг опасных продуктов",
            "Проверка выявила просроченные продукты в магазинах.",
            "https://example.com/brest",
        ),
        _product_safety_result(
            "Источник B",
            "Из магазинов изъяли 40 кг опасных продуктов",
            "Проверка выявила просроченные продукты в магазинах.",
            "https://example.com/gomel",
            region="Гомельская область",
        ),
    ])
    assert len(results) == 2


def test_editorial_question_about_id_card_is_excluded():
    result = decision(
        "Белоруска спросила, можно ли обменять ID-карту назад на бумажный паспорт?",
        (
            "В редакцию обратилась Елена с вопросом: правда ли, что вернуть старый паспорт нельзя? "
            "Даже если документ утерян, испорчен или просрочился. "
            "В ведомстве пояснили, что такой возможности по закону нет."
        ),
        source("Могилёвская область", "Могилёв", media_type="telegram"),
    )
    assert not result.relevant


def test_neutral_school_bazaar_announcement_is_excluded():
    result = decision(
        "Школьные базары в Беларуси продлятся до конца сентября",
        (
            "В магазинах выделены дополнительные торговые места. "
            "Большой выбор школьных товаров представлен в интернет-магазинах. "
            "Представитель МАРТ обратил внимание на бесплатную подгонку формы."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant


def test_salary_fraud_crime_story_is_excluded():
    result = decision(
        "Работникам начислялась зарплата, пока бригадир скрывал их отсутствие",
        (
            "Прокуратура сообщила об уголовном деле о служебном подлоге. "
            "Схема позволяла получать зарплату без выхода на работу."
        ),
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "кримин" in result.reason


def test_wage_arrears_crime_case_can_still_be_included():
    result = decision(
        "Директора осудили за невыплату зарплаты работникам",
        (
            "Работники несколько месяцев жаловались на задолженность по зарплате. "
            "Прокуратура установила нарушение трудовых прав."
        ),
        source("Минская область", "Борисов"),
    )
    assert result.relevant
    assert result.category == "Работа, зарплаты и доходы"


def test_harvest_praise_is_excluded():
    result = decision(
        "Водитель первым перевез пять тысяч тонн зерна",
        (
            "В сельском хозяйстве он работает четверть века. "
            "Руководитель обратил внимание на темпы жатвы и похвалил сельских тружеников. "
            "Упоминались воспитательные сборы для нерадивых работников."
        ),
        source("Брестская область", "Брест"),
    )
    assert not result.relevant


def test_export_certification_dispute_is_excluded_without_consumer_problem():
    result = decision(
        "Предприятие опровергает информацию о транзите мясной продукции",
        (
            "Россельхознадзор сообщил, что продукция не соответствует требованиям, и ввел ограничения на ввоз и транзит. "
            "Речь идет о ветеринарном сертификате и поставке в адрес предприятия ЕАЭС. "
            "Белорусская компания заявила, что не отгружала эту партию продукции."
        ),
        source("Минская область", "Молодечно"),
    )
    assert not result.relevant


def test_unsafe_bathing_water_is_ecology_not_rural_infrastructure():
    result = decision(
        "Вада ў Рудэі ўсё яшчэ небяспечная для купання",
        (
            "У вадасховішчы каля вёскі вада не адпавядае санітарным нормам па мікрабіялагічных паказчыках. "
            "Купанне можа прывесці да кішачных інфекцый."
        ),
        source("Могилёвская область", "Могилёв"),
    )
    assert result.relevant
    assert result.category == "Экология и санитарные проблемы"


def test_encoded_query_separator_is_repaired():
    assert canonicalize_url(
        "https://m-media.storage.googleapis.com/index.html%3Fp=139709.html"
    ) == "https://m-media.storage.googleapis.com/index.html?p=139709.html"


def test_naive_belarus_time_is_interpreted_as_minsk():
    parsed = social_monitor.parse_datetime("2026-07-30 18:21:28")
    assert parsed == dt.datetime(2026, 7, 30, 15, 21, 28, tzinfo=UTC)


def test_internal_regex_is_not_shown_as_subcategory():
    result = decision(
        "Жители жалуются на разбитую дорогу",
        "Дорога покрыта глубокими ямами.",
    )
    assert result.relevant
    assert "re:" not in result.subcategory
    assert "дорога" in result.subcategory


def test_hospital_administration_response_is_detected():
    result = decision(
        "Пациентка жалуется на очередь в поликлинике",
        (
            "Пациентка не могла стоять в очереди из-за боли. "
            "В администрации больницы отметили, что ее принял врач."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert result.relevant
    assert result.official_response


def test_expired_meat_in_store_is_product_quality():
    result = decision(
        "В магазине нашли просроченное мясо",
        (
            "МАРТ выявил нарушения в торговом объекте. "
            "Из продажи изъяли просроченные мясные продукты."
        ),
        source("Брестская область", "Иваново"),
    )
    assert result.relevant
    assert result.category == "Качество товаров и услуг"


def test_weather_forecast_without_actual_damage_is_excluded():
    result = decision(
        "Мощный штормовой фронт движется по Европе: синоптики оценили риски для Беларуси",
        "Синоптики прогнозируют сильные грозы с риском локальных подтоплений. Опасная погода ожидается в Европе.",
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "прогноз" in result.reason


def test_planned_hot_water_shutdown_is_excluded_without_complaint():
    result = decision(
        "Напомним, кто на Могилевщине будет сидеть без горячей воды в августе",
        "На подходе следующая очередь ремонта тепловых сетей. С 10 по 20 августа горячая вода будет отсутствовать по графику.",
        source("Могилёвская область", "Могилёв", "telegram"),
    )
    assert not result.relevant
    assert "плановое" in result.reason


def test_gai_queue_complaint_is_included_as_administrative_service():
    result = decision(
        "Мінчанка паскардзілася на вялікія чэргі ў ДАІ Малінаўкі",
        "Мінчанка апублікавала фатаграфію, на якой людзі чакаюць сваёй чаргі, каб атрымаць талон на рэгістрацыю аўтамабіля. Нумары выдаюць толькі праз тры дні пасля падачы заявы.",
        source("Беларусь", "Минск"),
    )
    assert result.relevant
    assert result.category == "Государственные и административные услуги"
    assert result.signal_type.startswith("жалоба")


def test_mould_after_fire_is_classified_as_housing():
    result = decision(
        "Стены влажные, плесень повсюду. Что происходит в сталинках после пожара",
        "После тушения пожара воды в квартире было по щиколотку. Жилье пока не пригодно для жизни, имущество испорчено, а на стенах появилась плесень.",
        source("Беларусь", "Минск"),
    )
    assert result.relevant
    assert result.category == "ЖКХ и состояние жилья"


def test_foreign_budget_deficit_story_is_excluded():
    result = decision(
        "Для покрытия расходов Саудовской Аравии нужна нефть по 115 долларов за баррель",
        "Дефицит бюджета королевства составил 34 миллиарда риалов. Добыча нефти остается ниже прежнего уровня.",
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "иностран" in result.reason or "макроэконом" in result.reason


def test_gardening_advice_about_dust_is_excluded():
    result = decision(
        "Что посадить вдоль забора, чтобы заглушить пыль с дороги",
        "Растения задерживают пыль и работают как природный фильтр. Лучшее решение — зеленый барьер.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_municipal_audit_finding_is_included():
    result = decision(
        "Минский зеленстрой вернул в бюджет 110 тысяч рублей после проверки КГК",
        "Выявлены нарушения при ремонте улиц и объектов благоустройства. Из бюджета оплатили завышенные объемы укладки тротуарной плитки.",
        source("Беларусь", "Минск"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"
    assert "критический" in result.signal_type


def test_utility_recalculation_explainer_is_excluded():
    result = decision(
        "Как сделать перерасчет платы за коммунальные услуги при отсутствии дома более 10 дней",
        "Для перерасчета необходимо подать заявление. Перерасчет производится за водоснабжение и вывоз коммунальных отходов.",
        source("Минская область", "Смолевичи"),
    )
    assert not result.relevant
    assert "справоч" in result.reason


def test_foreign_california_lifestyle_story_is_excluded():
    result = decision(
        "«Лос-Анджэлес – гэта страшная памыйніца». Беларус з сям’ёй пераехаў у Каліфорнію",
        (
            "Горад перанаселены, школы няважныя, няма парковак і высокая арэнда. "
            "Дзеці вучацца ў звычайнай дзяржаўнай школе ў Orange County."
        ),
        source("Минск", "Минск"),
    )
    assert not result.relevant
    assert "иностран" in result.reason


def test_routine_gai_enforcement_operation_is_excluded():
    result = decision(
        "ГАИ переходит на усиленный режим работы в предвыходные и выходные дни",
        (
            "В Могилёвской области пройдет специальное мероприятие «Безопасность на первом месте». "
            "К контролю привлечены экипажи ДПС и наряды из соседних регионов. "
            "Сотрудники ГАИ будут пресекать грубые нарушения ПДД и аварийные ситуации."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "профилактичес" in result.reason


def test_search_and_rescue_story_about_lost_pensioner_is_excluded():
    result = decision(
        "77-летнего мужчину нашли ночью поисковики-волонтёры в лесу под Сморгонью",
        (
            "Пенсионер заблудился, собирая грибы. "
            "Поисково-спасательный отряд нашел мужчину ночью. "
            "Он жаловался на боли в области сердца, после чего его передали медикам."
        ),
        source("Минская область", "Молодечно"),
    )
    assert not result.relevant


def test_word_pensioner_alone_is_not_social_protection_category():
    result = decision(
        "Пенсионер выиграл районный турнир",
        "77-летний пенсионер получил приз и поблагодарил организаторов.",
        source("Минская область", "Молодечно"),
    )
    assert not result.relevant


def test_real_pension_payment_complaint_remains_included():
    result = decision(
        "Пенсионеры жалуются на задержку пенсии",
        "Жители Борисова не получили пенсию в установленный срок и обратились за помощью.",
        source("Минская область", "Борисов"),
    )
    assert result.relevant
    assert result.category == "Социальная защита и базовые услуги"


def test_cybercrime_police_interview_is_excluded_even_when_marker_is_later():
    result = decision(
        "Как одна ссылка в мессенджере лишает жилья и денег, рассказали в Минском РУВД",
        (
            "В официальных магазинах нет взломанных игр с расширенными возможностями. "
            "Родителям советуют следить за изменениями в поведении ребенка. "
            "Сотрудники милиции предупреждают о мошенничестве и анонимности в интернете."
        ),
        source("Минская область", "Минский район"),
    )
    assert not result.relevant


def test_word_valuables_does_not_mean_prices():
    result = decision(
        "У подростка появились дорогие материальные ценности",
        "Родители должны выяснить происхождение вещей.",
        source("Минская область", "Минский район"),
    )
    assert not result.relevant





def test_doctor_heat_advice_is_excluded():
    result = decision(
        "Как пожилым людям пережить жару: рекомендации врача-гериатра",
        (
            "Не пользоваться общественным транспортом. "
            "Автобусы и троллейбусы быстро нагреваются и становятся похожими на парилку. "
            "Длительное нахождение в таком транспорте может довести до обморока или усугубить проблемы с сердцем. "
            "Регулярный душ помогает коже дышать и увеличивает теплоотдачу. "
            "Измерять его необходимо как минимум два раза в день. "
            "В первую очередь это правило касается гипертоников. "
            "Чтобы не допускать скачков давления, нужно принимать назначенные врачом препараты без перерывов."
        ),
        source("Могилёвская область", "Могилёв"),
    )
    assert not result.relevant
    assert "рекомендатель" in result.reason or "справоч" in result.reason


def test_first_of_all_phrase_does_not_mean_a_queue():
    result = decision(
        "Врач напомнил правила поведения в жару",
        "В первую очередь это правило касается гипертоников. Врач рекомендует пить воду.",
        source("Могилёвская область", "Могилёв"),
    )
    assert not result.relevant


def test_official_finding_of_mouldy_expired_products_remains_included():
    result = decision(
        "Госконтроль в Логойском районе обнаружил продукты с плесенью и за грубые нарушения приостановил работу гастролавки",
        (
            "Комитетом госконтроля Минской области в ходе мониторинга выявлены многочисленные нарушения в работе торгового объекта. "
            "Установлены факты реализации недоброкачественной продукции, продукции с истекшим сроком годности, отсутствия документов по качеству и безопасности. "
            "Пирожные продавались с плесенью, при этом определить сроки их годности было невозможно. "
            "Кроме того, использовалось загрязненное оборудование, на ряд товаров отсутствовали ценники."
        ),
        source("Минская область", "Логойский район"),
    )
    assert result.relevant
    assert result.category == "Качество товаров и услуг"



def zerkalo_source() -> Source:
    return Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=5,
        priority="A",
        name="Zerkalo.io",
        media_type="website",
        domain="zerkalo.io",
        start_url="https://news.zerkalo.io/latest/",
        language="ru",
        adapter="robust_article",
        feed_url=(
            "https://news.zerkalo.io/rss/economics.rss|"
            "https://news.zerkalo.io/rss/life.rss"
        ),
    )


def test_zerkalo_bank_commission_complaint_is_included():
    result = decision(
        "Беларуска сняла наличные, а со счета списали 1260 рублей комиссии",
        (
            "Со счета беларуски списали комиссию за снятие наличных в банкомате. "
            "О неприятном финансовом сюрпризе женщина рассказала в Threads."
        ),
        zerkalo_source(),
    )
    assert result.relevant
    assert result.category == "Социальная защита и базовые услуги"


def test_zerkalo_beltelecom_service_complaint_is_included():
    result = decision(
        "Письмо от Белтелекома смутило клиентов",
        (
            "Пользовательница назвала сомнительной схемой подключение платной услуги "
            "Белтелекома для ее мамы-пенсионерки."
        ),
        zerkalo_source(),
    )
    assert result.relevant
    assert result.category == "Связь, интернет и телевидение"


def test_zerkalo_tree_landscaping_complaint_is_included():
    result = decision(
        "Жители недовольны: деревья на проспекте заменили вазонами",
        (
            "Жителям Минска не понравилось решение убрать деревья. "
            "Они недовольны тем, как теперь выглядит городское озеленение."
        ),
        zerkalo_source(),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_zerkalo_station_storage_complaint_is_included():
    result = decision(
        "Беларуска возмутилась проблемой на вокзале в Минске",
        (
            "На железнодорожном вокзале камеры хранения не работают ночью. "
            "Чтобы оставить багаж, пассажиры вынуждены стоять в большой очереди."
        ),
        zerkalo_source(),
    )
    assert result.relevant
    assert result.category == "Общественный транспорт"


def test_zerkalo_routine_tax_warning_is_excluded():
    result = decision(
        "Налоговики предупредили об административной ответственности",
        (
            "Налоговики сообщили, что просроченная уплата подоходного налога "
            "может повлечь административную ответственность."
        ),
        zerkalo_source(),
    )
    assert not result.relevant


def test_zerkalo_border_queue_digest_is_excluded():
    result = decision(
        "Латвия закрыла границу с Беларусью",
        (
            "На выезд в ЕС через погранпереход образовалась очередь из автомобилей "
            "и автобусов. Пункт пропуска продолжает работать."
        ),
        zerkalo_source(),
    )
    assert not result.relevant


def test_zerkalo_news_subdomain_matches_apex_domain():
    assert social_monitor.same_site(
        "https://news.zerkalo.io/life/133283.html", "zerkalo.io"
    )
    assert social_monitor.is_probable_article_url(
        "https://news.zerkalo.io/life/133283.html", "zerkalo.io"
    )


# --- Стихийные события: включаем только жалобы на работу общественных служб. ---

def test_plain_storm_damage_is_excluded():
    result = decision(
        "Ливень затопил улицы Гродно и повалил деревья",
        (
            "После сильной грозы вода залила проезжую часть и подвалы. "
            "Спасатели откачивают воду, коммунальные службы убирают поваленные деревья."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant
    assert "стихии" in result.reason


def test_weather_damage_with_active_emergency_response_is_excluded():
    result = decision(
        "В Гродно устраняют последствия непогоды",
        (
            "Шквалистый ветер повалил деревья. Сотрудники МЧС оперативно распиливают их, "
            "а спецтехника расчищает дорогу и обеспечивает безопасный проезд."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant


def test_drainage_failed_during_extreme_rain_without_complaint_is_excluded():
    result = decision(
        "Ливневая канализация не справилась с сильным дождем",
        (
            "Из-за обильных осадков система ливневой канализации временно вышла из строя. "
            "Подтопило улицы и подземные переходы. Городские службы откачивают воду."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant


def test_residents_complain_services_ignore_storm_damage_is_included():
    result = decision(
        "Жители жалуются: коммунальные службы не реагируют после бури",
        (
            "После шквалистого ветра двор завален ветками. Жители жалуются, что коммунальные "
            "службы не приехали и не убирают поваленные деревья."
        ),
        source("Гродненская область", "Лида"),
    )
    assert result.relevant
    assert result.signal_type.startswith("жалоба")


def test_prolonged_power_restoration_after_storm_is_included():
    result = decision(
        "Поселок третьи сутки остается без электричества после грозы",
        (
            "После сильной грозы повреждена линия электропередачи. Жители третьи сутки без "
            "света, электроснабжение до сих пор не восстановлено."
        ),
        source("Минская область", "Минский район"),
    )
    assert result.relevant


def test_no_drinking_water_delivery_after_flood_is_included():
    result = decision(
        "После паводка деревня осталась без питьевой воды",
        (
            "Паводок повредил водопровод. Водоснабжение не восстановлено, а подвоз питьевой "
            "воды не организован. Жители требуют решить проблему."
        ),
        source("Гомельская область", "Петриковский район"),
    )
    assert result.relevant


def test_residents_clear_damage_instead_of_services_is_included():
    result = decision(
        "После шквала жители сами расчищают дорогу",
        (
            "Шквал повалил деревья на въезде в деревню. Не дождавшись служб, жители сами "
            "расчищают дорогу и убирают ветки."
        ),
        source("Брестская область", "Пружанский район"),
    )
    assert result.relevant


def test_zerkalo_citizen_opened_storm_drain_is_included():
    result = decision(
        "Гродно затопило во время сильного ливня",
        (
            "Местный житель отметил, что все так бы и стояли, пока какой-то парень сам не "
            "снял решетку ливневки и не ускорил сход воды."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert result.relevant


def test_failure_to_search_for_missing_people_is_included():
    result = decision(
        "Родные жалуются, что после паводка не ищут пропавшего",
        (
            "После паводка мужчина пропал в лесу. Родные утверждают, что поиски пропавшего "
            "не организованы и спасатели не реагируют на обращения."
        ),
        source("Гомельская область", "Житковичский район"),
    )
    assert result.relevant


def test_successful_search_after_storm_is_excluded():
    result = decision(
        "Спасатели нашли пропавшего после бури мужчину",
        (
            "Во время бури мужчина заблудился в лесу. Поисково-спасательная группа быстро "
            "нашла пропавшего и передала его медикам."
        ),
        source("Витебская область", "Полоцкий район"),
    )
    assert not result.relevant


def test_epasluga_neutral_explainer_is_excluded():
    result = decision(
        "Тестируем «Е-Паслугу»: как получить справки онлайн без очередей",
        (
            "Получение справок раньше ассоциировалось с бюрократией и очередями. "
            "Портал позволяет подать документы и получить справку дистанционно. "
            "Это инструкция по использованию государственного сервиса."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant


def test_household_advice_about_sweat_smell_is_excluded():
    result = decision(
        "Как убрать стойкий запах пота с одежды: простой народный метод",
        (
            "Стирайте загрязненные вещи в прохладной воде. Можно использовать уксус, "
            "водку или спирт, чтобы убрать запах без стирки."
        ),
        source("Брестская область", "Полесье"),
    )
    assert not result.relevant


def test_neutral_question_title_with_direct_resident_complaint_is_included():
    result = decision(
        "Что делать, если в доме неделю нет горячей воды",
        (
            "Жители обратились в редакцию и жалуются, что горячей воды нет уже неделю. "
            "Коммунальные службы не называют срок восстановления."
        ),
        source("Минская область", "Борисов"),
    )
    assert result.relevant


# --- Уточнение: обычная непогода и нейтральные кампании помощи. ---

def test_august_downpour_with_only_damage_is_excluded_even_with_emoji_title():
    result = decision(
        "💸",
        (
            "Потоп в кафе и лужи в жилом доме — как августовский ливень затопил Гродно. "
            "Ливень обрушился на город вечером и стремительно затопил улицы, жилые дома и другие здания. "
            "Посетители кафе не могли открыть дверь, пока помещение затапливало изнутри. "
            "Также пострадал жилой дом: очевидцы говорят, что здание затопило с первого по седьмой этаж."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant
    assert "стихии" in result.reason


def test_red_cross_school_campaign_is_excluded():
    result = decision(
        "На Минщине стартовала кампания Красного Креста «Соберем детей в школу»",
        (
            "Цель проекта — поддержать детей, оказавшихся в социально опасном положении, "
            "с инвалидностью, из семей с низким уровнем дохода, детей вынужденных мигрантов и беженцев. "
            "Присоединиться и поддержать инициативу может каждый желающий."
        ),
        source("Минская область", "Смолевичи"),
    )
    assert not result.relevant


def test_socially_dangerous_situation_phrase_is_not_a_problem_signal():
    result = decision(
        "Школьникам передали канцелярские принадлежности",
        (
            "Помощь получили дети, находящиеся в социально опасном положении, "
            "а также дети из многодетных семей и дети с инвалидностью."
        ),
        source("Минская область", "Смолевичи"),
    )
    assert not result.relevant


def test_support_campaign_with_service_failure_complaint_remains_included():
    result = decision(
        "Красный Крест начал сбор воды после жалоб жителей деревни",
        (
            "Жители жалуются, что после паводка водоснабжение не восстановлено. "
            "Коммунальные службы не реагируют, подвоз питьевой воды не организован. "
            "Красный Крест объявил сбор питьевой воды для семей."
        ),
        source("Гомельская область", "Петриковский район"),
    )
    assert result.relevant


# --- Уточнение фильтров по контрольному отчёту 2026-08-03. ---

def test_routine_utility_notice_with_exact_dates_is_excluded():
    result = decision(
        "Большое отключение горячей воды в Бресте: уже начинаем",
        (
            'Филиал "Брестские тепловые сети" информирует: с 00.00 3 августа '
            'до 17.00 7 августа будет временно отсутствовать горячая вода. '
            'В связи с ремонтными работами в тепловом пункте будет отключена '
            'горячая и холодная вода. Приносим извинения за временные неудобства.'
        ),
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "плановое" in result.reason


def test_drowning_accident_is_excluded():
    result = decision(
        "Два человека утонули за выходные на озере",
        (
            "Один мужчина утонул во время купания. ОСВОД напоминает, что "
            "купание в состоянии алкогольного опьянения создает опасность на воде."
        ),
        source("Минская область", "Слуцк"),
    )
    assert not result.relevant
    assert "происшествен" in result.reason or "спасатель" in result.reason


def test_foreign_health_research_is_excluded():
    result = decision(
        "Риск деменции зависит от места проживания",
        (
            "Исследование строилось на оценке распространенности факторов риска "
            "среди жителей 14 стран. У жителей США и Южной Кореи риски деменции "
            "связаны с разными причинами. В Бразилии отмечен дефицит физической активности."
        ),
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "исследован" in result.reason or "иностран" in result.reason


def test_currency_and_crypto_forecast_is_excluded():
    result = decision(
        "Вырастет ли доллар до трех рублей, а биткоин — до 70 тысяч? Прогноз на август",
        (
            "Финансовый аналитик отмечает, что курс белорусского рубля стабилизировался. "
            "Высокие цены на нефть поддерживают экспортную выручку. "
            "ФРС опасается нового ускорения инфляции."
        ),
        source("Брестская область", "Полесье"),
    )
    assert not result.relevant
    assert "финансов" in result.reason or "рыночн" in result.reason


def test_store_manager_profile_is_excluded():
    result = decision(
        'Кто управляет магазином "Перекресток" в Заславле',
        (
            "Руководитель лично чинит холодильники и управляет коллективом. "
            "Мы иногда жалуемся на очереди, но редко задумываемся, кто отвечает "
            "за хозяйство. Для директора главное не цена, а качество."
        ),
        source("Минская область", "Минский район"),
    )
    assert not result.relevant
    assert "портрет" in result.reason or "имидж" in result.reason


def test_festival_safety_complaint_is_quality_of_service_not_street_lighting():
    result = decision(
        "Вместо запуска фонариков — осы и скорая. Минчане пожаловались на фестиваль",
        (
            "Посетители пожаловались на укусы насекомых и организацию мероприятия. "
            "Места скопления гнезд никто не оградил, нескольким детям потребовалась помощь."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Качество товаров и услуг"


def _transport_result(
    source_name: str,
    title: str,
    excerpt: str,
    url: str,
    published_at: str,
    text_length: int,
) -> social_monitor.ArticleResult:
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
        published_at=published_at,
        category="Общественный транспорт",
        subcategory="автобус, остановка",
        excerpt=excerpt,
        signal_type="жалоба жителей или пользователей",
        official_response=False,
        score=9,
        matched_terms="автобус, жалова",
        discovered_via="feed:test",
        text_length=text_length,
    )


def test_same_transport_event_from_two_media_is_consolidated_with_resonance_links():
    first = _transport_result(
        "Zerkalo.io",
        "Противники кнопок открытия дверей добились своего — в Минске функцию поставили на стоп",
        (
            "В Минсктрансе сообщили о планах вернуться к вопросу тестирования кнопок. "
            "Система адресного открытия дверей активируется после полной остановки. "
            "Пассажиры жаловались, что водители иногда просто уезжают."
        ),
        "https://news.zerkalo.io/life/133376.html",
        "2026-08-03T07:35:00+00:00",
        1310,
    )
    second = _transport_result(
        "Белновости",
        "Система адресного открытия дверей в наземном транспорте Минска приостановлена",
        (
            "Система адресного открытия дверей тестировалась в Минске. "
            "Пассажиры жаловались на неудобство. Кнопки работают только после полной остановки. "
            "Минсктранс планирует вернуться к вопросу тестирования позже."
        ),
        "https://www.belnovosti.by/minsk/adresnoe-otkrytie-dverey",
        "2026-08-03T07:30:01+00:00",
        2029,
    )
    results = social_monitor.deduplicate_results([first, second])
    assert len(results) == 1
    assert results[0].related_coverage == ((
        "Zerkalo.io", "https://news.zerkalo.io/life/133376.html"
    ),)


def test_two_similar_transport_complaints_in_different_cities_are_not_merged():
    first = _transport_result(
        "Первое СМИ",
        "Жители жалуются на кнопки открытия дверей в автобусах",
        "Пассажиры жалуются на систему адресного открытия дверей.",
        "https://example.com/a",
        "2026-08-03T07:00:00+00:00",
        500,
    )
    first.locality = "Минск"
    second = _transport_result(
        "Второе СМИ",
        "Жители жалуются на кнопки открытия дверей в автобусах",
        "Пассажиры жалуются на систему адресного открытия дверей.",
        "https://example.org/b",
        "2026-08-03T08:00:00+00:00",
        500,
    )
    second.locality = "Гродно"
    assert len(social_monitor.deduplicate_results([first, second])) == 2


def test_neutral_veterinary_regulatory_reform_is_excluded():
    result = decision(
        "Чиновники намерены ввести ужесточения для ветеринарных клиник",
        "В Беларуси намерены ввести изменения для работников ветеринарных клиник. "
        "С внедрением правил будут установлены одинаковые требования. "
        "Депутат парламента сообщила о фактах некачественного оказания помощи.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "регулятор" in result.reason


def test_foreign_accident_linked_only_to_belarusian_tourists_is_excluded():
    result = decision(
        "Дрон упал на пляж в Геленджике, куда ездит много белорусских туристов",
        "В России погибли люди. Геленджик остается массовым направлением отдыха белорусов, "
        "куда группы направляют многие турагентства.",
        source("Брестская область", "Полесье"),
    )
    assert not result.relevant


def test_single_traffic_violation_is_excluded():
    result = decision(
        "Проезд на красный и букет нарушений: водитель привлечен к ответственности",
        "Правоохранители установили личность нарушителя. Водитель автомобиля проехал на красный свет.",
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "дорожный инцидент" in result.reason


def test_systemic_road_safety_complaint_is_kept():
    result = decision(
        "Жители жалуются на опасную остановку трамвая",
        "Пассажиры неоднократно жаловались, что посадка идет с проезжей части. "
        "Они требуют решить проблему и оборудовать безопасную остановку.",
    )
    assert result.relevant


def test_neutral_new_transport_service_is_excluded():
    result = decision(
        "В Беларуси запустили новый сервис автобусных стыковок",
        "Заработал новый сервис автобусных поездок. Новая услуга работает по принципу "
        "пересадок и автоматически подбирает маршрут.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_new_chief_physician_appointment_is_excluded():
    result = decision(
        "В районной больнице назначен новый главврач",
        "Районное здравоохранение с этого дня возглавляет новый главный врач. "
        "Коллективу представили руководителя.",
        source("Минская область", "Дзержинск"),
    )
    assert not result.relevant


def test_tourism_popularity_ranking_is_excluded():
    result = decision(
        "Минск стал самым популярным зарубежным городом у российских туристов",
        "Компания назвала популярные города для туристов. Беларусь возглавила рейтинг направлений для путешественников.",
        source("Минская область", "Дзержинск"),
    )
    assert not result.relevant


def test_drying_local_river_is_classified_as_ecology():
    result = decision(
        "В Пинске стремительно высыхает река",
        "Уровень воды снизился ниже исторического минимума. Река обмелела, существует риск замора рыбы. "
        "Власти намерены решать проблему реконструкцией гидротехнических сооружений.",
        source("Брестская область", "Пинск"),
    )
    assert result.relevant
    assert result.category == "Экология и санитарные проблемы"


def test_same_water_outage_keeps_priority_a_source_and_compact_resonance():
    first = social_monitor.ArticleResult(
        source_name="Kraj.by", source_type="website", country="Минская область",
        locality="Молодечно и регион", priority="A", source_language="ru",
        title="Сбой электроники нового насоса оставил без горячей воды жителей пятиэтажки в Чисти",
        title_generated=False, url="https://news.kraj.by/item", published_at="2026-08-03T09:00:00+03:00",
        category="ЖКХ и состояние жилья", subcategory="горячая вода",
        excerpt="Житель дома 12а на Парковой улице в Чисти пожаловался: пятые сутки нет горячей воды. Коммунальщик установил новый насос.",
        signal_type="жалоба жителей или пользователей", official_response=True,
        score=13, matched_terms="горячая вода", discovered_via="feed", text_length=3000,
    )
    second = social_monitor.ArticleResult(
        source_name="Минская правда", source_type="website", country="Минская область",
        locality="Минская область", priority="B", source_language="ru",
        title="Сбой в работе нового насоса лишил горячей воды жителей пятиэтажки в Молодечненском районе",
        title_generated=False, url="https://mlyn.by/item", published_at="2026-08-03T10:00:00+03:00",
        category="ЖКХ и состояние жилья", subcategory="горячая вода",
        excerpt="В поселке Чисть житель дома 12а на Парковой улице пожаловался: пятые сутки отсутствует горячая вода. Был установлен новый насос.",
        signal_type="жалоба жителей или пользователей", official_response=True,
        score=13, matched_terms="горячая вода", discovered_via="page", text_length=2200,
    )
    kept = social_monitor.deduplicate_results([first, second])
    assert len(kept) == 1
    assert kept[0].source_name == "Kraj.by"
    assert kept[0].related_coverage == ((
        "Минская правда", "https://mlyn.by/item"
    ),)


def test_neutral_maintenance_smell_notice_is_excluded():
    result = decision(
        "В ближайшие дни в Минске может пахнуть мазутом. В чем причина?",
        "С 4 по 8 августа жители некоторых районов могут чувствовать запах мазута. "
        "Это связано с технологическими особенностями обслуживания оборудования и не представляет угрозы для экологии и здоровья жителей. "
        "Энергетики делают всё необходимое, чтобы обеспечить надежную и бесперебойную работу. "
        "Прошлым летом в отдельных районах отмечались перебои с горячей водой.",
    )
    assert not result.relevant
    assert "планов" in result.reason or "сервис" in result.reason


def test_preventive_cardiology_interview_is_excluded():
    result = decision(
        "Дела семейные: врач-кардиолог о том, как внимание и забота внутри пары спасает жизни",
        "Медицинский директор рассказывает, как близкий человек влияет на состояние сосудов. "
        "Совместный быт может стать лучшей кардиопрофилактикой. Врач объясняет, почему перебои в работе сердца опасны.",
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant
    assert "медицин" in result.reason or "профилакти" in result.reason


def test_estonian_employment_story_is_excluded_as_foreign():
    result = decision(
        "В эстонской Нарве уволили более 100 помощников воспитателей из-за языковых требований",
        "В городе Нарва уволены 108 помощников воспитателей из-за отсутствия уровня владения эстонским языком.",
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "иностран" in result.reason


def test_positive_new_bridge_feature_is_excluded():
    result = decision(
        "Надежная артерия. Как новый мост через Припять изменит жизнь жителей двух районов",
        "Строительство нового моста продолжается. Пока паром не ходит по расписанию, жителям приходится ехать в длинный объезд. "
        "Объект свяжет два района и улучшит транспортное сообщение.",
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "инфраструктур" in result.reason


def test_routine_restaurant_fire_report_is_excluded():
    result = decision(
        "В ресторане загорелась вытяжка: МЧС ликвидировало задымление",
        "Спасатели прибыли на место. Наблюдались клубы дыма, но после тушения убедились в отсутствии риска повторного возгорания. "
        "Люди вышли самостоятельно, никто не пострадал.",
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant
    assert "происшествен" in result.reason or "спасатель" in result.reason


def test_military_training_ground_construction_is_excluded():
    result = decision(
        "Подтверждено место строительства военного полигона в Гомельской области",
        "Официальный документ подтверждает место будущего военного полигона и строительного городка.",
        source("Гомельская область", "Гомель"),
    )
    assert not result.relevant
    assert "полит" in result.reason


def test_foreign_danube_mine_story_is_excluded():
    result = decision(
        "В Словакии в обмелевшем Дунае нашли мину времен Второй мировой войны",
        "В обмелевшей части Дуная обнаружили британскую мину. Низкий уровень воды ограничивает судоходство.",
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "иностран" in result.reason or "кримин" in result.reason


def test_forest_fire_with_working_emergency_response_is_excluded():
    result = decision(
        "Лесной пожар тушат с воздуха десятками тонн воды",
        "Авиаторы МЧС ликвидируют лесной пожар. Спасатели выполнили 18 сбросов воды. "
        "Тушение осложняет отсутствие подъездных путей, но работы продолжаются.",
        source("Минская область", "Минская область"),
    )
    assert not result.relevant
    assert "происшествен" in result.reason or "спасатель" in result.reason


def test_neutral_property_auction_is_excluded():
    result = decision(
        "Заброшенный дом отдыха с 10 гектарами земли продают с торгов",
        "Бывший дом отдыха не работает с 2008 года и выставлен на торги. К объекту ведет аварийная дорога с разбитым асфальтом. "
        "Лот включает постройки и землю, объект ищет нового владельца.",
        source("Минская область", "Минская область"),
    )
    assert not result.relevant
    assert "торг" in result.reason or "продаж" in result.reason


def test_unpaid_internship_and_workplace_toilet_complaint_is_work_category():
    result = decision(
        "Беларуска пожаловалась на платный туалет на работе",
        "Жительница Мозыря четыре дня бесплатно стажировалась в магазине, но получила отказ в трудоустройстве. "
        "Работники должны за свой счет пользоваться платным туалетом, и она пожаловалась директору.",
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Работа, зарплаты и доходы"


def test_rusty_tap_water_is_classified_as_housing_utility():
    result = decision(
        "Вода уже три месяца ржавая — жители Пинска снова жалуются",
        "Жители говорят, что из крана идет ржавая вода ненадлежащего качества. "
        "Проблема сохраняется до сих пор, коммунальные службы предложили перерасчет платы за воду.",
        source("Брестская область", "Пинск"),
    )
    assert result.relevant
    assert result.category == "ЖКХ и состояние жилья"


def test_missing_bins_on_public_alley_is_classified_as_beautification():
    result = decision(
        "Жители жаловались на мусор: на аллее наконец поставили урны",
        "На всей набережной была только одна урна. Жители жаловались, что отсутствие инфраструктуры превращает аллею в свалку.",
        source("Минская область", "Слуцк"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"

# --- Связь, интернет, телевидение и доступность поликлиник. ---

def test_mobile_operator_unexplained_charge_is_included():
    result = decision(
        "Абоненты жалуются на произвольные списания",
        (
            "Мобильный оператор списал деньги за платную подписку, "
            "которую пользователи не подключали."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Связь, интернет и телевидение"


def test_mobile_operator_imposed_service_is_included():
    result = decision(
        "Клиенту навязали платную услугу",
        (
            "Оператор мобильной связи подключил услугу без согласия абонента. "
            "Отключить ее через приложение не получается."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Связь, интернет и телевидение"


def test_bad_mobile_connection_is_included():
    result = decision(
        "Жители жалуются на плохую мобильную связь",
        (
            "В деревне постоянно обрываются звонки, пропадает сеть "
            "и невозможно дозвониться."
        ),
        source("Гомельская область", "Мозырский район"),
    )
    assert result.relevant
    assert result.category == "Связь, интернет и телевидение"


def test_low_mobile_internet_speed_is_included():
    result = decision(
        "Пользователи недовольны качеством мобильного интернета",
        (
            "Скорость упала почти до нуля, соединение постоянно обрывается, "
            "а техподдержка не отвечает."
        ),
        source("Беларусь", "Минск"),
    )
    assert result.relevant
    assert result.category == "Связь, интернет и телевидение"


def test_home_internet_outage_is_included():
    result = decision(
        "Домашний интернет не работает несколько дней",
        (
            "Абоненты Белтелекома направили обращения провайдеру, "
            "но проблему до сих пор не устранили."
        ),
        source("Минская область", "Борисов"),
    )
    assert result.relevant
    assert result.category == "Связь, интернет и телевидение"


def test_paid_iptv_channels_unavailable_is_included():
    result = decision(
        "Провайдер списывает плату за недоступные IPTV-каналы",
        (
            "Оплаченные каналы недоступны уже пять дней. "
            "Поддержка закрывает обращения без решения проблемы."
        ),
        source("Беларусь", "Минск"),
    )
    assert result.relevant
    assert result.category == "Связь, интернет и телевидение"


def test_collective_tv_antenna_failure_is_included():
    result = decision(
        "Жители дома две недели остаются без телевидения",
        (
            "Телевизионный сигнал пропал из-за неисправности коллективной антенны. "
            "Заявки обслуживающей организации результата не дали."
        ),
        source("Витебская область", "Орша"),
    )
    assert result.relevant
    assert result.category == "Связь, интернет и телевидение"


def test_clinic_no_appointments_is_included():
    result = decision(
        "Пациенты не могут записаться к врачу",
        (
            "В поликлинике нет талонов к неврологу, "
            "а ближайшие талоны предлагают только через два месяца."
        ),
        source("Беларусь", "Минск"),
    )
    assert result.relevant
    assert result.category == "Здравоохранение"


def test_clinic_electronic_booking_failure_is_included():
    result = decision(
        "Электронная запись в поликлинике не работает",
        (
            "Пациенты жалуются, что телефон регистратуры постоянно занят "
            "и записаться на обследование невозможно."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert result.relevant
    assert result.category == "Здравоохранение"


def test_new_mobile_tariff_without_complaint_is_excluded():
    result = decision(
        "Оператор представил новый тариф",
        "В новый тариф включен увеличенный пакет мобильного интернета.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_new_tv_channel_without_complaint_is_excluded():
    result = decision(
        "В пакете появился новый телеканал",
        "Провайдер добавил познавательный канал о природе и путешествиях.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_planned_mobile_network_maintenance_is_excluded():
    result = decision(
        "Оператор предупредил о технических работах",
        (
            "Ночью возможны кратковременные перерывы мобильной связи "
            "из-за планового обслуживания сети."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_expanded_mobile_coverage_without_problem_is_excluded():
    result = decision(
        "Оператор расширил покрытие сети",
        "Новая базовая станция улучшила мобильную связь в нескольких деревнях.",
        source("Минская область", "Минский район"),
    )
    assert not result.relevant


def test_general_internet_commentary_is_excluded():
    result = decision(
        "Люди разучились пользоваться интернетом",
        "Эксперт рассуждает о цифровых привычках и социальных сетях.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_smartphone_review_is_excluded():
    result = decision(
        "Обзор нового смартфона с быстрым интернетом",
        "Редакция протестировала камеру, экран и работу устройства в сети.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_tv_program_content_criticism_without_service_problem_is_excluded():
    result = decision(
        "Зрители обсуждают содержание телепередачи",
        "Автор критикует сюжет программы, но телевизионный сигнал работает нормально.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_neutral_clinic_schedule_is_excluded():
    result = decision(
        "Поликлиника опубликовала расписание врачей",
        "Обновленное расписание приема действует со следующей недели.",
        source("Беларусь", "Минск"),
    )
    assert not result.relevant

# --- Регрессии dry-run social-report-14. ---

def test_phrase_v_svoyu_ochered_does_not_mean_queue():
    result = decision(
        "МТС предложил абонентам безлимитный интернет",
        (
            "Абоненты смогут подключить новую услугу. "
            "В свою очередь действующие клиенты могут активировать ее в приложении."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_mts_unlimited_5g_promo_is_excluded():
    result = decision(
        "МТС предложил всем абонентам безлимитный интернет на скорости 5G",
        (
            "При подключении или смене тарифа клиенты получают безлимит интернета без ограничений. "
            "Услуга предоставляется без дополнительной платы до конца года."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_gai_internet_monitoring_is_not_telecom_category():
    result = decision(
        "Как ГАИ сокращает очереди в МРЭО",
        (
            "В ходе мониторинга интернет-ресурсов была найдена публикация об очередях "
            "в регистрационном подразделении ГАИ. Ведомство увеличило число сотрудников."
        ),
        source("Беларусь", "Минск"),
    )
    assert result.relevant
    assert result.category == "Государственные и административные услуги"


def test_phishing_site_warning_is_excluded_as_crime():
    result = decision(
        "Белорусов предупредили об опасном фейковом сайте",
        (
            "В сети обнаружили качественный фишинговый ресурс. "
            "Мошенники создали сайт, который имитирует систему электронной очереди "
            "таможенного оформления, поэтому пользователям опасно вводить данные."
        ),
        source("Беларусь", "Минск"),
    )
    assert not result.relevant
    assert "кримин" in result.reason


def test_british_fire_without_belarus_context_is_excluded():
    result = decision(
        "Десять домов сгорело после неудачного барбекю",
        (
            "Пожар произошел в Великобритании. Британские спасатели сообщили "
            "о высокой пожарной опасности и призвали жителей Уэльса убирать мусор."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "иностран" in result.reason



# --- Общественно значимые противоправные практики и обсуждение законов. ---

def test_belarusian_recruitment_for_foreign_war_is_included():
    result = decision(
        "Как граждан РБ в интернете вербуют на войну",
        (
            "Беларусам предлагают деньги за участие в боевых действиях и службу "
            "в иностранной армии. Вербовка распространяется через локальные чаты "
            "белорусских городов."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Общественная безопасность и противоправные практики"
    assert "противоправ" in result.signal_type


def test_foreign_recruitment_without_belarus_context_is_excluded():
    result = decision(
        "Жителей России вербуют для участия в войне",
        "Вербовка проходит через российские социальные сети и московские чаты.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_belarusian_dog_law_public_reaction_is_included():
    result = decision(
        "В сети обсуждают ужесточение закона для владельцев собак",
        (
            "Белорусы активно обсуждают изменения правил выгула собак. "
            "Одних возмущает запрет брать питомцев в магазины и кафе, "
            "другие поддерживают более строгие требования к владельцам."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Законы, права и общественное регулирование"
    assert "общественная реакция" in result.signal_type


def test_positive_or_mixed_reaction_to_belarusian_draft_law_is_included():
    result = decision(
        "Белорусы обсуждают проект закона о защите потребителей",
        (
            "Проект закона в Беларуси вынесен на общественное обсуждение. "
            "Часть граждан поддерживает новые правила, другие предлагают доработать требования."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Законы, права и общественное регулирование"


def test_neutral_belarusian_law_announcement_without_public_reaction_is_excluded():
    result = decision(
        "В Беларуси подготовили проект закона",
        "Законопроект предусматривает технические изменения и вступит в силу после публикации.",
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_discussion_of_foreign_law_is_not_treated_as_belarusian_regulation():
    result = decision(
        "Белорусы обсуждают новый закон Литвы",
        (
            "В Литве изменили правила содержания животных. "
            "Белорусы в социальных сетях обсуждают штрафы, действующие в Вильнюсе."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


# --- Регрессии dry-run social-report-15. ---

def test_free_nanny_explainer_is_excluded():
    result = decision(
        "Когда семья может воспользоваться услугами бесплатной няни, рассказала консультант из Минтруда",
        (
            "Для молодых родителей в стране действуют меры поддержки. "
            "Консультант разъяснила, в какой ситуации родители могут воспользоваться "
            "услугой и какие документы для этого нужно предоставить."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_historical_shopping_tours_story_is_excluded():
    result = decision(
        "Шоп-туры 1991-го: как белорусы проявляли чудеса изобретательности",
        (
            "Шоп-туры конца 1991 года проходили в условиях тотального дефицита в СССР. "
            "Материал рассказывает, как граждане соседних республик вывозили товары."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "историчес" in result.reason


def test_belarusian_foreign_property_purchase_story_is_excluded():
    result = decision(
        "Беларус купил таунхаус в Вильнюсе — его наблюдения",
        (
            "Искали долго: цены на квартиры в Литве были высокими. "
            "Покупатель рассказал о переводе наличных евро из Беларуси."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_future_school_goods_monitoring_announcement_is_excluded():
    result = decision(
        "Белорусы смогут сообщить о нехватке школьных товаров — куда обращаться",
        (
            "В августе представители профсоюзов проведут мониторинг магазинов "
            "и школьных базаров. В список проверки войдут одежда, обувь и канцтовары."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant


def test_private_apartment_gift_dispute_is_excluded():
    result = decision(
        "Бабушка подарила внучкам квартиру, а потом передумала. Что решил суд?",
        (
            "Договор дарения жительница Гомеля заключила со своими внучками. "
            "Позднее она решила оспорить договор и вернуть подаренную квартиру."
        ),
        source("Гомельская область", "Гомель"),
    )
    assert not result.relevant


def test_children_passports_border_story_is_regulatory_rights():
    result = decision(
        "Без паспортов мы их не выпустим. Беларуска передумала везти детей на родину",
        (
            "По словам беларуски, оба ребенка родились в Германии и имеют немецкое "
            "и российское гражданства, полученные по отцу. Перед поездкой на родину "
            "она обратилась за консультацией в беларусское консульство. "
            "Пограничники предупредили: без паспортов детей их не выпустят."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Законы, права и общественное регулирование"


def test_plaster_falling_after_capital_repairs_is_housing():
    result = decision(
        "В Могилеве с многоэтажки после капремонта начала осыпаться штукатурка",
        (
            "Жительница дома № 7 по улице Народного ополчения сообщила об аварийном "
            "состоянии фасада: прямо над входом в подъезд осыпается штукатурка. "
            "Капитальный ремонт фасада завершили в прошлом году."
        ),
        source("Могилёвская область", "Могилёв"),
    )
    assert result.relevant
    assert result.category == "ЖКХ и состояние жилья"


def test_hotel_condition_complaints_are_service_quality():
    result = decision(
        "Гостиница в Лоеве попала на видео: реакция и отзывы беларусов",
        (
            "Пользователи жалуются на элементарное состояние помещений и номеров. "
            "После отзывов гостей в гостинице признали проблему."
        ),
        source("Гомельская область", "Лоев"),
    )
    assert result.relevant
    assert result.category == "Качество товаров и услуг"


def test_public_consultation_without_information_is_regulatory():
    result = decision(
        "Былую бальніцу №2 у Гродне могуць рэканструяваць пад жытло. Праект вынеслі на грамадскія абмеркаванні, але інфармацыі няма",
        (
            "Гродзенскі комплекс будынкаў былой гарадской бальніцы №2 плануецца "
            "рэканструяваць пад шматкватэрныя жылыя дамы. Праект вынеслі на "
            "грамадскія абмеркаванні, але інфармацыі няма."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert result.relevant
    assert result.category == "Законы, права и общественное регулирование"
    assert "общественного обсуждения" in result.signal_type


def test_same_event_from_two_sources_is_one_card_with_resonance():
    first = make_result("https://source-one.example/event", "Брест")
    second = make_result("https://source-two.example/event", "Брест")
    first.title = "На рынке выявили нарушения торговли"
    second.title = "На рынке выявили нарушения торговли"
    first.source_name = "Источник один"
    second.source_name = "Источник два"
    results = social_monitor.deduplicate_results([first, second])
    assert len(results) == 1
    assert len(results[0].related_coverage) == 1


# --- Архитектура сбора: независимые RSS, sitemap и главная страница. ---

def test_deduplicate_candidates_preserves_all_discovery_channels():
    src = source()
    feed_candidate = Candidate(
        source=src,
        url="https://example.com/news/1?utm_source=rss",
        title="Короткий заголовок",
        published_at="2026-08-06T08:00:00+00:00",
        discovered_via="feed:https://example.com/rss",
    )
    homepage_candidate = Candidate(
        source=src,
        url="https://example.com/news/1",
        title="Более полный заголовок публикации",
        summary="Краткое описание публикации",
        discovered_via="homepage",
    )

    merged = social_monitor.deduplicate_candidates(
        [feed_candidate, homepage_candidate]
    )

    assert len(merged) == 1
    assert merged[0].url == "https://example.com/news/1"
    assert merged[0].title == "Более полный заголовок публикации"
    assert merged[0].summary == "Краткое описание публикации"
    assert merged[0].published_at == "2026-08-06T08:00:00+00:00"
    assert social_monitor.candidate_discovery_channels(merged[0]) == {
        "feed", "homepage"
    }


def test_balanced_selection_prevents_feed_from_suppressing_other_channels():
    src = source()
    feed = [
        Candidate(
            source=src,
            url=f"https://example.com/feed/{index}",
            title=f"Публикация RSS номер {index}",
            published_at=f"2026-08-06T{index % 24:02d}:00:00+00:00",
            discovered_via="feed:https://example.com/rss",
        )
        for index in range(40)
    ]
    homepage = [
        Candidate(
            source=src,
            url=f"https://example.com/home/{index}",
            title=f"Материал главной страницы номер {index}",
            discovered_via="homepage",
        )
        for index in range(10)
    ]
    sitemap = [
        Candidate(
            source=src,
            url=f"https://example.com/map/{index}",
            title=f"Материал карты сайта номер {index}",
            published_at="2026-08-06T09:00:00+00:00",
            discovered_via="sitemap:https://example.com/sitemap.xml",
        )
        for index in range(5)
    ]

    selected = social_monitor.select_balanced_source_candidates(
        [*feed, *homepage, *sitemap],
        35,
        feed_reserve=20,
        homepage_reserve=10,
        sitemap_reserve=5,
    )

    assert len(selected) == 35
    assert sum(
        "feed" in social_monitor.candidate_discovery_channels(item)
        for item in selected
    ) >= 20
    assert sum(
        "homepage" in social_monitor.candidate_discovery_channels(item)
        for item in selected
    ) >= 10
    assert sum(
        "sitemap" in social_monitor.candidate_discovery_channels(item)
        for item in selected
    ) >= 5


def test_collect_source_candidates_always_collects_all_website_channels(
    monkeypatch,
):
    src = source()
    settings = copy.deepcopy(SETTINGS)
    settings["monitor"]["per_source_candidate_limit"] = 35
    settings["discovery"]["per_channel_candidate_limit"] = 40
    settings["discovery"]["feed_candidate_reserve"] = 20
    settings["discovery"]["homepage_candidate_reserve"] = 10
    settings["discovery"]["sitemap_candidate_reserve"] = 5
    calls = []

    monkeypatch.setattr(
        social_monitor,
        "HttpClient",
        lambda _settings: object(),
    )
    monkeypatch.setattr(
        social_monitor,
        "discover_endpoints",
        lambda *_args, **_kwargs: {
            "feeds": ["https://example.com/rss"],
            "sitemaps": ["https://example.com/sitemap.xml"],
        },
    )

    def fake_feed(_source, _url, _client, _cutoff, _limit):
        calls.append("feed")
        return [
            Candidate(
                source=src,
                url=f"https://example.com/feed/{index}",
                title=f"RSS публикация {index}",
                published_at=f"2026-08-06T{index % 24:02d}:00:00+00:00",
                discovered_via="feed:https://example.com/rss",
            )
            for index in range(35)
        ]

    def fake_sitemap(
        _source, _url, _client, _cutoff, _limit, _max_children
    ):
        calls.append("sitemap")
        return [
            Candidate(
                source=src,
                url=f"https://example.com/map/{index}",
                title=f"Sitemap публикация {index}",
                published_at="2026-08-06T09:00:00+00:00",
                discovered_via="sitemap:https://example.com/sitemap.xml",
            )
            for index in range(5)
        ]

    def fake_homepage(_source, _client, _limit):
        calls.append("homepage")
        return [
            Candidate(
                source=src,
                url=f"https://example.com/home/{index}",
                title=f"Публикация главной страницы {index}",
                discovered_via="homepage",
            )
            for index in range(10)
        ]

    monkeypatch.setattr(social_monitor, "collect_from_feed", fake_feed)
    monkeypatch.setattr(social_monitor, "collect_from_sitemap", fake_sitemap)
    monkeypatch.setattr(social_monitor, "collect_from_homepage", fake_homepage)

    selected, error, metrics = social_monitor.collect_source_candidates(
        src,
        settings,
        {},
        dt.datetime(2026, 8, 2, tzinfo=UTC),
    )

    assert error is None
    assert metrics.feed_candidates == 35
    assert metrics.sitemap_candidates == 5
    assert metrics.homepage_candidates == 10
    assert metrics.merged_candidates == 50
    assert metrics.selected_candidates == 35
    assert metrics.source_limit_hit
    assert metrics.clipped_candidates == 15
    assert calls == ["feed", "sitemap", "homepage"]
    assert len(selected) == 35
    selected_channels = [
        social_monitor.candidate_discovery_channels(item)
        for item in selected
    ]
    assert sum("homepage" in channels for channels in selected_channels) >= 10
    assert sum("sitemap" in channels for channels in selected_channels) >= 5


def test_restored_belaes_unit_without_outage_is_excluded():
    result = decision(
        "Энергоблок № 2 Белорусской АЭС включён в работу",
        (
            "Энергоблок был возвращён в сеть после плановой диагностики. "
            "Отключение проходило в штатном режиме и не связано с аварийной ситуацией. "
            "Перебоев с электроснабжением потребителей зафиксировано не было."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "штатное восстановление" in result.reason


def test_actual_power_outage_complaint_is_not_suppressed_by_restoration_rule():
    result = decision(
        "После аварии электроснабжение восстановили, но жители снова без света",
        (
            "Жители деревни жалуются, что электричество пропадало три дня. "
            "После сообщения о восстановлении работа сети снова нарушилась, "
            "а обращения в службу результата не дали."
        ),
        source("Минская область", "Дзержинск"),
    )
    assert result.relevant
    assert result.category == "ЖКХ и состояние жилья"


def test_foreign_motorcycle_trip_to_pamir_is_excluded():
    result = decision(
        "Как белорусский мотоциклист штурмовал Памир",
        (
            "Путешествие проходило по маршруту Бишкек — Ош в Кыргызстане. "
            "Мотоциклист доехал до Токтогульского водохранилища, "
            "а затем продолжил высокогорный маршрут."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant


def test_neutral_pension_rules_explainer_is_excluded():
    result = decision(
        "Кто сможет выйти на пенсию в Беларуси в 2027 году и какой нужен стаж",
        (
            "Напоминаем, какой пенсионный возраст будет действовать и какие "
            "условия назначения пенсии предусмотрены законодательством. "
            "Если стажа не хватает, назначается социальная пенсия."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert not result.relevant
    assert "пенсионное разъяснение" in result.reason


def test_public_reaction_to_pension_law_change_is_kept():
    result = decision(
        "Белорусы обсуждают проект изменения пенсионного закона",
        (
            "В Беларуси опубликован проект закона. Он вызвал споры: "
            "граждане опасаются повышения необходимого стажа и направили "
            "предложения в Палату представителей."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant
    assert result.category == "Законы, права и общественное регулирование"


def test_neutral_bridge_construction_progress_is_excluded():
    result = decision(
        "Строительство путепровода по улице Железнодорожной",
        (
            "В городе ведется строительство кольцевой автодороги. "
            "Возведение объекта разбито на 24 очереди. "
            "Работы по строительству выполняет мостостроительная организация."
        ),
        source("Могилёвская область", "Могилёв"),
    )
    assert not result.relevant
    assert "ходе строительства" in result.reason


def test_delayed_bridge_construction_complaint_is_kept():
    result = decision(
        "Жители жалуются на затянувшееся строительство путепровода",
        (
            "В городе ведется строительство путепровода, однако сроки сорваны. "
            "Жители несколько месяцев жалуются на опасный проход и отсутствие "
            "удобного объезда."
        ),
        source("Могилёвская область", "Могилёв"),
    )
    assert result.relevant


def test_urban_greenery_discussion_is_categorized_as_beautification():
    result = decision(
        "Брестчане жалуются на вырубку деревьев и озеленение города",
        (
            "Жители обсуждают вырубку взрослых деревьев и жалуются, что город "
            "теряет тенистые улицы. Власти обещали компенсационные посадки "
            "возле гостиницы, но брестчане опасаются, что молодые саженцы "
            "не заменят взрослые деревья."
        ),
        source("Брестская область", "Брест"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_hotel_complaint_still_uses_quality_category():
    result = decision(
        "Гости пожаловались на состояние гостиницы",
        (
            "Постояльцы рассказали о грязном номере, плесени и отсутствии "
            "горячей воды в гостинице. Отзывы гостей вызвали проверку."
        ),
        source("Гомельская область", "Лоев"),
    )
    assert result.relevant
    assert result.category == "Качество товаров и услуг"


def test_jkh_access_dispute_is_categorized_as_housing():
    result = decision(
        "Спор о доступе коммунальников в квартиру дошел до суда",
        (
            "Жительница не предоставила доступ коммунальникам для проведения "
            "капремонта и замены системы горячего водоснабжения. Возник спор: "
            "она заявила, что работники ЖКХ могут разбить отделку и не "
            "восстановить повреждения."
        ),
        source("Минск", "Минск"),
    )
    assert result.relevant
    assert result.category == "ЖКХ и состояние жилья"


def test_worker_housing_complaint_is_categorized_as_housing():
    result = decision(
        "Жилье для работников совхоза вызвало споры",
        (
            "Беларуска показала жилье для молодых специалистов. "
            "Она пожаловалась на отсутствие горячей воды, грязь, "
            "сломанную кровать и общее состояние жилья."
        ),
        source("Минская область", "Узда"),
    )
    assert result.relevant
    assert result.category == "ЖКХ и состояние жилья"


def test_source_coverage_contains_collection_limit_telemetry():
    src = source("Минск", "Минск")
    metrics = social_monitor.SourceCollectionMetrics(
        feed_candidates=80,
        sitemap_candidates=60,
        homepage_candidates=25,
        merged_candidates=120,
        selected_candidates=35,
        selected_feed=20,
        selected_sitemap=8,
        selected_homepage=12,
        source_limit=35,
        source_limit_hit=True,
        clipped_candidates=85,
    )
    rows = social_monitor.build_source_coverage(
        [src],
        [],
        [],
        [],
        [],
        {(src.country, src.name): metrics},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["merged_candidates"] == 120
    assert row["source_limit_hit"] is True
    assert row["clipped_candidates"] == 85
    assert row["feed_candidates"] == 80
    assert row["homepage_candidates"] == 25


def test_foreign_motorcycle_trip_with_breakdown_is_still_excluded():
    result = decision(
        "«Земля уходила из-под колёс»: как белорусский мотоциклист штурмовал Памир",
        (
            "Путешествие проходило по трассе Бишкек — Ош в Кыргызстане. "
            "Маршрут вел к Токтогульскому водохранилищу и озеру Каракуль. "
            "После границы у путешественника начались проблемы с мотоциклом, "
            "но он продолжил высокогорный маршрут."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant
    assert "туристическая" in result.reason


def test_neutral_biohacking_forum_announcement_is_excluded():
    result = decision(
        "С каких анализов начинается здоровье: в Минске пройдет форум «Код Биохакинга»",
        (
            "Программу разделили на три лекционных потока. "
            "Эксперты расскажут, как читать скрытые дефициты, "
            "управлять энергией и состоянием."
        ),
        source("Минск", "Минск"),
    )
    assert not result.relevant
    assert "анонс мероприятия" in result.reason


def test_foreign_moldova_water_crisis_without_belarus_impact_is_excluded():
    result = decision(
        "Молдова начала использовать стратегический запас воды из-за обмеления Днестра",
        (
            "Министр окружающей среды Молдовы призвал граждан экономить воду. "
            "Операторам водоснабжения и канализации поручено адаптировать "
            "инфраструктуру, чтобы обеспечить подачу воды без перебоев."
        ),
        source("Брестская область", "Брест"),
    )
    assert not result.relevant
    assert "иностранная тема" in result.reason


def test_neutral_hotline_announcement_is_excluded():
    result = decision(
        "КГК проведет горячую линию по вопросам обслуживания домов",
        (
            "На горячую линию можно сообщить о недостатках содержания жилья, "
            "некачественном выполнении заявок, водоснабжении и благоустройстве. "
            "Звонки будут принимать 6 августа."
        ),
        source("Гомельская область", "Гомель"),
    )
    assert not result.relevant
    assert (
        "анонс горячей линии" in result.reason
        or "канал обращений" in result.reason
    )


def test_actual_complaints_reported_during_hotline_are_kept():
    result = decision(
        "После горячей линии КГК выявили нарушения в обслуживании домов",
        (
            "Жители массово пожаловались на протекающие крыши и отсутствие воды. "
            "Проверка установила нарушения, а коммунальным службам выдали предписания."
        ),
        source("Гомельская область", "Гомель"),
    )
    assert result.relevant


def test_neutral_watermelon_market_overview_is_excluded():
    result = decision(
        "На рынке в Молодечно узнали цены на арбузы",
        (
            "Продавцы рассказали, сколько стоят арбузы и дыни. "
            "Некоторые плоды везут из Дагестана недозрелыми, чтобы они "
            "не испортились в дороге."
        ),
        source("Минская область", "Молодечно"),
    )
    assert not result.relevant


def test_neutral_family_budget_profile_is_excluded():
    result = decision(
        "Бюджет 12 000 BYN: как живет белорусская семья, которая копит по 5 тысяч",
        (
            "Семья рассказала о ежемесячных расходах. Коммуналка вместе "
            "с интернетом стоит 180 рублей, мобильная связь на четверых — 160. "
            "Супруга работает из дома и почти не ездит на транспорте."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant


def test_neutral_sleep_health_advice_is_excluded():
    result = decision(
        "11 вопросов о том, что мешает нам спать и как с этим бороться",
        (
            "Врач объяснил причины синдрома беспокойных ног. "
            "Иногда он возникает из-за дефицита железа, приема лекарств "
            "или нарушения обмена дофамина."
        ),
        source("Брестская область", "Пинск"),
    )
    assert not result.relevant


def test_neutral_gardening_advice_is_excluded():
    result = decision(
        "Уход за сливой после сбора урожая: пять дел в августе и сентябре",
        (
            "Если погода засушливая, без воды сливе не обойтись. "
            "На десять литров воды берут сульфат калия и суперфосфат, "
            "чтобы восполнить дефицит питания."
        ),
        source("Минская область", "Минск"),
    )
    assert not result.relevant
    assert "садовый" in result.reason



def test_street_lighting_problem_remains_included():
    result = decision(
        "На улице месяцами не горят фонари",
        (
            "Жители Минска жалуются, что уличное освещение не работает "
            "уже несколько месяцев. В темное время суток дорога опасна."
        ),
        source("Минск", "Минск"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_park_mowing_and_cleaning_complaint_is_included():
    result = decision(
        "Парк зарос травой и мусором",
        (
            "Жители жалуются: траву не косят, мусор не убирают, "
            "а дорожки в городском парке остаются запущенными."
        ),
        source("Гомельская область", "Гомель"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_neutral_park_mowing_report_is_excluded():
    result = decision(
        "В городском парке завершили покос травы",
        (
            "Коммунальные службы скосили траву и убрали территорию. "
            "Работы выполнены по плану, жалоб от жителей нет."
        ),
        source("Минская область", "Борисов"),
    )
    assert not result.relevant


def test_cemetery_maintenance_complaint_is_beautification():
    result = decision(
        "На кладбище не косят траву и не вывозят мусор",
        (
            "Жители жалуются, что кладбище заросло травой, контейнеры "
            "переполнены, а мусор не убирают уже несколько недель."
        ),
        source("Могилёвская область", "Могилёв"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_war_memorial_neglect_complaint_is_included():
    result = decision(
        "Воинский мемориал разрушается и зарос травой",
        (
            "Жители жалуются: памятник Великой Отечественной войны "
            "неухожен, облицовка осыпается, а Вечный огонь не работает."
        ),
        source("Витебская область", "Витебск"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_neutral_war_memorial_restoration_is_excluded():
    result = decision(
        "К 9 Мая восстановили памятник Великой Отечественной войны",
        (
            "На мемориале заменили плитку, обновили надписи и благоустроили "
            "территорию. Работы завершены в срок."
        ),
        source("Брестская область", "Брест"),
    )
    assert not result.relevant


def test_river_pollution_complaint_is_ecology():
    result = decision(
        "Жители жалуются на загрязнение реки",
        (
            "В реку сбрасывают сточные воды, на поверхности появилась пена, "
            "а местные жители сообщили о гибели рыбы."
        ),
        source("Гомельская область", "Речица"),
    )
    assert result.relevant
    assert result.category == "Экология и санитарные проблемы"


def test_lake_pollution_finding_is_ecology():
    result = decision(
        "Прокуратура выявила загрязнение озера нефтепродуктами",
        (
            "Проверка установила сброс отходов в озеро. Зафиксирована гибель "
            "рыбы, виновным предписано устранить нарушения."
        ),
        source("Минская область", "Мядель"),
    )
    assert result.relevant
    assert result.category == "Экология и санитарные проблемы"


def test_illegal_shore_fence_is_land_and_water_access():
    result = decision(
        "Берег озера незаконно огородили забором",
        (
            "Жители жалуются, что предприниматель перекрыл проход к берегу "
            "водоема и поставил забор до воды. Доступ к озеру ограничен."
        ),
        source("Минская область", "Мядель"),
    )
    assert result.relevant
    assert result.category == "Земля, водоёмы и доступ к природе"


def test_neutral_legal_shore_improvement_is_excluded():
    result = decision(
        "На берегу озера благоустроили место отдыха",
        (
            "У воды установили лавочки и урны, расчистили проход и "
            "оборудовали площадку для отдыха."
        ),
        source("Минская область", "Мядель"),
    )
    assert not result.relevant


def test_belarusian_cemetery_and_memorial_complaint_is_included():
    result = decision(
        "Жыхары скардзяцца на занядбаныя могілкі і мемарыял",
        (
            "На могілках не косяць траву і не прыбіраюць смецце. "
            "Мемарыял Вялікай Айчыннай вайны разбураецца, вечны агонь не гарыць."
        ),
        source("Гродненская область", "Лида"),
    )
    assert result.relevant
    assert result.category == "Дороги и благоустройство"


def test_hrodna_animal_volunteer_water_problem_remains_included():
    result = decision(
        "У сувязі са спякотай у Гродне валанцёры просяць падтрымаць бяздомных жывёл",
        (
            "У Гродне стаіць спякота да +31℃. У валанцёраў пункта часовага "
            "ўтрымання бяздомных жывёл няма сталага доступу да праточнай вады, "
            "з-за гэтага хутчэй распаўсюджваецца інфекцыя. Таксама шчанюкам і "
            "старым сабакам патрэбна ежа і папяровыя ручнікі."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert result.relevant


def test_mozyr_regional_profit_analysis_is_excluded_as_macroeconomics():
    result = decision(
        "Почти половину прибыли Гомельской области обеспечивает Мозырский район",
        (
            "По итогам первого полугодия валовой региональный продукт Гомельской "
            "области вырос на 1,4%. Ускорение финансовых показателей района "
            "совпало с нарастанием дефицита топлива в России. Организации района "
            "обеспечили почти половину всей чистой прибыли Гомельской области. "
            "Динамика связана с ситуацией на российском топливном рынке."
        ),
        source("Гомельская область", "Гомель"),
    )
    assert not result.relevant
    assert "макроэконом" in result.reason


def test_full_pamir_travelogue_excerpt_is_excluded_despite_word_problems():
    result = decision(
        "«Земля уходила из-под колёс»: как белорусский мотоциклист штурмовал Памир",
        (
            "Я решил использовать это время, чтобы наконец доехать до "
            "Токтогульского водохранилища. Обмелевшее водохранилище. Маршрут "
            "проходил по трассе Бишкек — Ош, одной из самых красивых дорог "
            "Кыргызстана. Несмотря на асфальт, скучать здесь невозможно. "
            "Следующей целью стал участок грунтовых дорог. По этим дорогам "
            "практически никто не ездит. Озеро Каракуль и первые проблемы с "
            "мотоциклом. После границы путешественник доехал до озера Каракуль."
        ),
        source("Гродненская область", "Гродно"),
    )
    assert not result.relevant
    assert "путевой очерк" in result.reason


def test_domestic_business_losses_are_not_suppressed_only_by_political_name():
    result = decision(
        "Атаки на Wildberries: Лукашенко попросит Путина компенсировать потери бизнеса?",
        (
            "Белорусский бизнес потерял товары после уничтожения складов. "
            "Предприниматели из Беларуси требуют компенсации ущерба и сообщают "
            "о крупных финансовых потерях."
        ),
        source("Беларусь", "Беларусь"),
    )
    assert result.relevant


def test_source_candidate_limit_uses_name_override():
    settings = copy.deepcopy(SETTINGS)
    src = source()
    src = Source(**{**src.__dict__, "name": "Zerkalo.io", "domain": "zerkalo.io"})
    assert social_monitor.source_candidate_limit(src, settings) == 65


def test_source_candidate_limit_keeps_default_for_small_source():
    settings = copy.deepcopy(SETTINGS)
    assert social_monitor.source_candidate_limit(source(), settings) == 35


def test_candidate_processing_capacity_is_raised_by_one_thousand_and_reserves_growth():
    settings = copy.deepcopy(SETTINGS)
    sources = [
        Source(**{**source().__dict__, "name": f"Источник {index}", "rank": index + 1})
        for index in range(40)
    ]
    assert settings["monitor"]["max_candidates_per_run"] == 4000
    assert settings["monitor"]["planned_source_reserve"] == 8
    assert social_monitor.candidate_processing_capacity(sources, settings) == 4000


def test_browser_headers_are_used_for_protected_candidate_domains():
    client = social_monitor.HttpClient(SETTINGS)
    headers = client.headers_for_url("https://reform.news/category/news")
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    ordinary = client.headers_for_url("https://example.com/news")
    assert ordinary["User-Agent"] == SETTINGS["monitor"]["user_agent"]


def test_telegram_title_skips_service_only_lines():
    title = social_monitor.telegram_title_from_text(
        "🚨\nБЫСТРО\nЖители Бреста жалуются на перекрытый доступ к берегу реки."
    )
    assert title.startswith("Жители Бреста")


def test_telegram_linked_site_prefers_original_article_url():
    src = Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name="Reform.news",
        media_type="telegram",
        domain="reform.news",
        start_url="https://t.me/reformby",
        language="ru",
        adapter="telegram_linked_site",
    )
    soup = social_monitor.BeautifulSoup(
        '<div><a href="https://reform.news/zhiteli-minska-zhaluyutsya-na-vodu/">Материал</a></div>',
        "html.parser",
    )
    url = social_monitor.telegram_linked_article_url(
        soup.div, src, "https://t.me/reformby/12"
    )
    assert url == "https://reform.news/zhiteli-minska-zhaluyutsya-na-vodu"


def test_belsat_adapter_uses_meta_description_when_body_is_short():
    src = Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name="Белсат",
        media_type="website",
        domain="ru.belsat.eu",
        start_url="https://ru.belsat.eu",
        language="ru",
        adapter="belsat_article",
    )
    item = Candidate(
        source=src,
        url="https://ru.belsat.eu/123/test",
        title="Проблема белорусского бизнеса",
        summary="",
    )
    html = """
    <html><head>
      <meta property="og:title" content="Проблема белорусского бизнеса">
      <meta property="og:description" content="Белорусские предприниматели сообщили о потерях товаров и потребовали компенсацию ущерба после уничтожения склада.">
    </head><body><main><p>Коротко.</p></main></body></html>
    """
    extracted = extract_article_from_html(item, html)
    assert "предприниматели" in extracted.text
    assert len(extracted.text) >= 80


def direct_web_source(name: str, domain: str, start_url: str, adapter: str) -> Source:
    return Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name=name,
        media_type="website",
        domain=domain,
        start_url=start_url,
        language="ru",
        adapter=adapter,
    )


def test_charter97_profile_uses_only_verified_direct_feeds_without_probe():
    src = direct_web_source(
        "Хартия-97", "charter97.org", "https://charter97.org/ru/news/", "protected_article"
    )

    class NoProbeClient:
        def get(self, _url, *args, **kwargs):
            raise AssertionError("exact profile must not probe homepage/robots/common paths")

    endpoints = social_monitor.discover_endpoints(src, SETTINGS, {}, NoProbeClient())
    assert endpoints["feeds"] == [
        "https://charter97.org/ru/rss/economics/",
        "https://charter97.org/ru/rss/society/",
    ]
    assert endpoints["sitemaps"] == []
    assert endpoints["listing_pages"] == []
    assert endpoints["skip_homepage"] is True


def test_profiled_article_url_shapes_reject_service_pages():
    charter = direct_web_source(
        "Хартия-97", "charter97.org", "https://charter97.org/ru/news/", "protected_article"
    )
    pozirk = direct_web_source(
        "Позірк", "pozirk.online", "https://pozirk.online/ru/news/", "protected_article"
    )
    belsat = direct_web_source(
        "Белсат", "ru.belsat.eu", "https://ru.belsat.eu/", "belsat_article"
    )
    assert social_monitor.is_source_article_url(
        "https://charter97.org/ru/news/2026/8/7/693893/", charter
    )
    assert not social_monitor.is_source_article_url(
        "https://charter97.org/ru/news/", charter
    )
    assert social_monitor.is_source_article_url(
        "https://pozirk.online/ru/news/151234/", pozirk
    )
    assert not social_monitor.is_source_article_url(
        "https://pozirk.online/ru/news/", pozirk
    )
    assert social_monitor.is_source_article_url(
        "https://ru.belsat.eu/94722291/belwest", belsat
    )
    assert not social_monitor.is_source_article_url(
        "https://ru.belsat.eu/programs", belsat
    )


def test_pozirk_collection_uses_curated_listing_and_skips_generic_homepage(monkeypatch):
    src = direct_web_source(
        "Позірк", "pozirk.online", "https://pozirk.online/ru/news/", "protected_article"
    )
    calls = []

    monkeypatch.setattr(social_monitor, "HttpClient", lambda _settings: object())

    def fake_listing(source_arg, listing_url, _client, _limit):
        calls.append(listing_url)
        return [Candidate(
            source=source_arg,
            url="https://pozirk.online/ru/news/151234/test-material/",
            title="Жители белорусского города сообщили о проблеме с водой",
            discovered_via=f"listing:{listing_url}",
        )]

    monkeypatch.setattr(social_monitor, "collect_from_listing_page", fake_listing)
    monkeypatch.setattr(
        social_monitor,
        "collect_from_homepage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic homepage must be skipped for Pozirk")
        ),
    )

    selected, error, metrics = social_monitor.collect_source_candidates(
        src,
        copy.deepcopy(SETTINGS),
        {},
        dt.datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert error is None
    assert calls == ["https://pozirk.online/ru/news/"]
    assert len(selected) == 1
    assert metrics.listing_candidates == 1
    assert metrics.homepage_candidates == 0
    assert "listing" in social_monitor.candidate_discovery_stages(selected[0])
    # Legacy balancing still treats listing as the homepage/web-page reserve.
    assert "homepage" in social_monitor.candidate_discovery_channels(selected[0])


def test_belsat_profile_has_verified_direct_endpoints():
    src = direct_web_source(
        "Белсат", "ru.belsat.eu", "https://ru.belsat.eu/", "belsat_article"
    )

    class NoProbeClient:
        def get(self, _url, *args, **kwargs):
            raise AssertionError("exact profile must not probe generic endpoints")

    endpoints = social_monitor.discover_endpoints(src, SETTINGS, {}, NoProbeClient())
    assert endpoints["feeds"] == [
        "https://ru.belsat.eu/rss",
        "https://ru.belsat.eu/shared/feed/google_news.php",
    ]
    assert endpoints["sitemaps"] == ["https://ru.belsat.eu/sitemap-full_index.xml"]
    assert endpoints["skip_homepage"] is True


def test_belsat_embedded_next_json_article_body_is_extracted():
    src = direct_web_source(
        "Белсат", "ru.belsat.eu", "https://ru.belsat.eu/", "belsat_article"
    )
    item = Candidate(
        source=src,
        url="https://ru.belsat.eu/94722291/belwest",
        title="Новости",
    )
    body = (
        "<p>Белорусские работники сообщили о задержке выплат и обратились к руководству предприятия.</p>"
        "<p>Сотрудники рассказали, что проблема сохраняется уже несколько недель и затрагивает многие семьи.</p>"
        "<p>Представитель предприятия сообщил, что задолженность обещают погасить после поступления средств.</p>"
    )
    payload = {
        "props": {
            "pageProps": {
                "article": {
                    "title": "Работники сообщили о задержке выплат",
                    "articleContent": body,
                    "publishedAt": "2026-08-07T08:15:00+00:00",
                }
            }
        }
    }
    html_doc = (
        "<html><head><script id='__NEXT_DATA__' type='application/json'>"
        + social_monitor.json.dumps(payload, ensure_ascii=False)
        + "</script></head><body><main><p>Короткая карточка.</p></main></body></html>"
    )
    extracted = extract_article_from_html(item, html_doc)
    assert extracted.title == "Работники сообщили о задержке выплат"
    assert "задолженность обещают погасить" in extracted.text
    assert len(extracted.text) > 250
    assert extracted.published_at.startswith("2026-08-07T08:15:00")
    assert extracted.date_source == "embedded-json"


def test_belsat_prefers_long_article_container_over_short_teaser():
    src = direct_web_source(
        "Белсат", "ru.belsat.eu", "https://ru.belsat.eu/", "belsat_article"
    )
    item = Candidate(
        source=src,
        url="https://ru.belsat.eu/94723094/pogranichnaya-zona",
        title="Жители рассказали о проблеме",
    )
    html_doc = """
    <html><body>
      <div data-testid="article-teaser">
        <p>Жители рассказали о проблеме, которая сохраняется в их районе уже несколько дней.</p>
        <p>Короткая карточка содержит только небольшой анонс публикации для главной страницы.</p>
      </div>
      <article>
        <p>Жители белорусского района сообщили о длительных перебоях с коммунальной услугой и многочисленных обращениях.</p>
        <p>По словам местных жителей, проблема сохраняется несколько недель и создаёт бытовые трудности для семей.</p>
        <p>Люди направили коллективное обращение в местную организацию и попросили назвать точные сроки устранения неполадок.</p>
        <p>Представители службы ответили, что специалисты уже обследуют участок и планируют завершить необходимые работы.</p>
      </article>
    </body></html>
    """
    extracted = extract_article_from_html(item, html_doc)
    assert "коллективное обращение" in extracted.text
    assert "специалисты уже обследуют участок" in extracted.text
    assert len(extracted.text) > 400


def test_charter97_article_uses_mirror_when_official_is_blocked(monkeypatch):
    src = Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name="Хартия-97",
        media_type="website",
        domain="charter97.org",
        start_url="https://charter97.org/ru/news/",
        language="ru",
        adapter="protected_article",
    )
    item = Candidate(
        source=src,
        url="https://charter97.org/ru/news/2026/8/7/700001/",
        title="Жители жалуются на отсутствие воды",
        published_at="2026-08-07T08:00:00+00:00",
    )

    class Response:
        def __init__(self, url):
            self.url = url
            self.content = """
            <html><head><meta property='og:title'
            content='Жители жалуются на отсутствие воды'></head>
            <body><div class='article_text'>
            <p>Жители нескольких домов жалуются на отсутствие воды.</p>
            <p>Коммунальные службы сообщили, что устраняют аварию.</p>
            </div></body></html>
            """.encode("utf-8")

    calls = []
    def fake_get(self, url, retries=1):
        calls.append((url, retries))
        if "charter97.org" in url:
            return None
        if "charter97.link" in url:
            return Response(url)
        return None

    monkeypatch.setattr(social_monitor.HttpClient, "get", fake_get)
    extracted = social_monitor.extract_article(item, SETTINGS)
    assert "отсутствие воды" in extracted.text
    assert calls[0][0].startswith("https://charter97.org/")
    assert calls[1][0].startswith("https://charter97.link/")


def test_pozirk_preclean_container_survives_height_for_sidebar_class():
    src = Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name="Позірк",
        media_type="website",
        domain="pozirk.online",
        start_url="https://pozirk.online/ru/news/",
        language="ru",
        adapter="protected_article",
    )
    item = Candidate(
        source=src,
        url="https://pozirk.online/ru/news/123456/example",
        title="Жители жалуются на качество воды",
    )
    html = """
    <html><head><meta property="og:title"
    content="Жители жалуются на качество воды"></head>
    <body>
      <div class="wrapperEditor height-for-sidebar">
        <div class="single-news__content">
          <p>Жители района жалуются на неприятный запах воды уже несколько дней.</p>
          <p>Люди обращались в коммунальные службы и ждут устранения проблемы.</p>
        </div>
        <aside class="sidebar"><p>Популярные новости и реклама.</p></aside>
      </div>
    </body></html>
    """
    extracted = extract_article_from_html(item, html)
    assert "неприятный запах воды" in extracted.text
    assert "Популярные новости" not in extracted.text


def test_belsat_short_static_html_uses_rendered_fallback(monkeypatch):
    src = Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name="Белсат",
        media_type="website",
        domain="ru.belsat.eu",
        start_url="https://ru.belsat.eu/",
        language="ru",
        adapter="belsat_article",
    )
    item = Candidate(
        source=src,
        url="https://ru.belsat.eu/94738461/test",
        title="Жители жалуются на проблему",
    )

    class Response:
        url = item.url
        content = """
        <html><head>
          <meta property='og:title' content='Жители жалуются на проблему'>
          <meta property='og:description'
          content='Жители сообщили о проблеме и ждут ответа коммунальных служб.'>
        </head><body></body></html>
        """.encode("utf-8")

    rendered = """
    <html><head><meta property='og:title'
    content='Жители жалуются на проблему'></head>
    <body><article><div class='article__content'>
      <p>Жители Минска жалуются на отсутствие воды в нескольких домах.</p>
      <p>Проблема сохраняется несколько дней, люди обращались в коммунальные службы.</p>
      <p>В организации сообщили, что ремонтная бригада уже работает на месте.</p>
    </div></article></body></html>
    """

    monkeypatch.setattr(
        social_monitor.HttpClient, "get",
        lambda *args, **kwargs: Response(),
    )
    calls = []
    monkeypatch.setattr(
        social_monitor,
        "render_belsat_article_html",
        lambda url, settings: calls.append(url) or rendered,
    )
    extracted = social_monitor.extract_article(item, SETTINGS)
    assert calls == [item.url]
    assert len(extracted.text) > 180
    assert "ремонтная бригада" in extracted.text


def test_belsat_does_not_render_when_static_body_is_already_full(monkeypatch):
    src = Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name="Белсат",
        media_type="website",
        domain="ru.belsat.eu",
        start_url="https://ru.belsat.eu/",
        language="ru",
        adapter="belsat_article",
    )
    item = Candidate(
        source=src,
        url="https://ru.belsat.eu/94738461/test",
        title="Жители жалуются на проблему",
    )

    long_text = (
        "Жители Минска жалуются на отсутствие воды в нескольких домах. "
        "Проблема сохраняется уже несколько дней, люди неоднократно обращались "
        "в коммунальные службы и просят назвать сроки восстановления. "
        "В организации сообщили, что ремонтная бригада работает на месте. "
        "После завершения работ подачу воды обещают восстановить."
    )

    class Response:
        url = item.url
        content = (
            "<html><body><article><div class='article__content'><p>"
            + long_text
            + "</p></div></article></body></html>"
        ).encode("utf-8")

    monkeypatch.setattr(
        social_monitor.HttpClient, "get",
        lambda *args, **kwargs: Response(),
    )
    monkeypatch.setattr(
        social_monitor,
        "render_belsat_article_html",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("JS render should not run")
        ),
    )
    extracted = social_monitor.extract_article(item, SETTINGS)
    assert len(extracted.text) >= 250


# --- Architecture Core 1: centralized profiles and stage telemetry. ---

def test_architecture_core1_registry_contains_strategic_protected_sources():
    for domain in (
        "charter97.org",
        "pozirk.online",
        "ru.belsat.eu",
        "reform.news",
        "gazetaby.com",
    ):
        profile = social_monitor.source_profile_for_domain(domain, SETTINGS)
        assert profile["protected"] is True
        assert profile["transport_order"]
        assert profile["extraction_order"] == [
            "source_specific", "embedded_json", "json_ld", "generic_html",
        ]
    assert social_monitor.source_profile_for_domain(
        "charter97.org", SETTINGS
    )["transport_order"] == ["requests", "official_mirror"]
    assert social_monitor.source_profile_for_domain(
        "gazetaby.com", SETTINGS
    )["transport_order"][0] == "chromium"


def test_architecture_core1_url_classification_distinguishes_shapes():
    assert social_monitor.classify_source_url(
        "https://example.com/news/real-story-123", "example.com", {}
    ) == "article"
    assert social_monitor.classify_source_url(
        "https://example.com/category/economics", "example.com", {}
    ) == "rubric"
    assert social_monitor.classify_source_url(
        "https://example.com/video/report", "example.com", {}
    ) == "video"
    assert social_monitor.classify_source_url(
        "https://example.com/photos/report", "example.com", {}
    ) == "gallery"
    assert social_monitor.classify_source_url(
        "https://example.com/archive/2025", "example.com", {}
    ) == "archive"
    assert social_monitor.classify_source_url(
        "https://example.com/login", "example.com", {}
    ) == "service"
    assert social_monitor.classify_source_url(
        "https://other.example/news/story", "example.com", {}
    ) == "external"


def test_architecture_core1_profile_rule_promotes_known_article_shape():
    profile = social_monitor.source_profile_for_domain("pozirk.online", SETTINGS)
    assert social_monitor.classify_source_url(
        "https://pozirk.online/ru/news/123456/example",
        "pozirk.online",
        profile,
    ) == "article"
    assert social_monitor.classify_source_url(
        "https://pozirk.online/ru/news/",
        "pozirk.online",
        profile,
    ) == "unknown"


def test_architecture_core1_metadata_prefilter_is_non_destructive():
    strong = candidate(
        title="Жители Минска жалуются на отсутствие воды",
        summary="В доме несколько дней нет воды.",
    )
    unknown = candidate(
        title="Что произошло в одном из районов Минска",
        summary="Редакция выяснила подробности ситуации.",
    )
    assert social_monitor.metadata_prefilter(strong, SETTINGS).status == "strong"
    assert social_monitor.metadata_prefilter(unknown, SETTINGS).status == "needs_text"


def test_architecture_core1_listing_and_telegram_channels_have_own_telemetry():
    listing_item = Candidate(
        source=source(),
        url="https://example.com/news/story-123",
        title="Жители жалуются на отсутствие воды",
        discovered_via="listing:https://example.com/news/",
    )
    telegram_item = Candidate(
        source=source(media_type="telegram"),
        url="https://t.me/test/42",
        title="Жители жалуются на отсутствие воды",
        discovered_via="telegram:https://t.me/s/test",
        inline_text="Жители жалуются на отсутствие воды.",
    )
    assert social_monitor.candidate_discovery_stages(listing_item) == {"listing"}
    assert social_monitor.candidate_discovery_stages(telegram_item) == {"telegram"}


def test_architecture_core1_extraction_cascade_prefers_source_specific():
    src = Source(
        enabled=True,
        country="Брестская область",
        country_code="BY-BR",
        locality="Брест",
        rank=1,
        priority="A",
        name="BGmedia",
        media_type="website",
        domain="bgmedia.site",
        start_url="https://bgmedia.site/",
        language="ru",
        adapter="robust_article",
    )
    item = Candidate(
        source=src,
        url="https://bgmedia.site/news/test-article",
        title="Карточка",
    )
    html = """
    <html><head><script type="application/ld+json">
    {"@type":"NewsArticle","headline":"JSON заголовок",
     "articleBody":"JSON-LD резервный текст статьи. Он достаточно длинный, но source-specific должен иметь приоритет перед ним при корректном селекторе."}
    </script></head><body>
      <div class="article__content">
        <p>Жители Бреста жалуются на отсутствие воды в нескольких домах.</p>
        <p>Коммунальные службы пока не назвали срок восстановления водоснабжения.</p>
      </div>
    </body></html>
    """
    extracted = extract_article_from_html(item, html)
    assert extracted.extraction_strategy == "source_specific"
    assert "Жители Бреста" in extracted.text
    assert "JSON-LD резервный" not in extracted.text


def test_architecture_core1_embedded_json_is_traced_for_belsat():
    src = direct_web_source(
        "Белсат", "ru.belsat.eu", "https://ru.belsat.eu/", "belsat_article"
    )
    item = Candidate(
        source=src,
        url="https://ru.belsat.eu/94722291/test",
        title="Карточка",
    )
    payload = {
        "props": {
            "pageProps": {
                "article": {
                    "title": "Embedded заголовок",
                    "articleContent": (
                        "<p>Жители сообщили о длительной проблеме с водой, которая сохраняется уже несколько недель и затрагивает несколько домов.</p>"
                        "<p>Люди неоднократно обращались в коммунальные службы, направляли заявки и просили назвать точные сроки устранения неполадок.</p>"
                        "<p>Редакция получила комментарий обслуживающей организации, где сообщили о проведении обследования и подготовке ремонтных работ.</p>"
                    ),
                }
            }
        }
    }
    html_doc = (
        "<html><head><script id='__NEXT_DATA__' type='application/json'>"
        + social_monitor.json.dumps(payload, ensure_ascii=False)
        + "</script></head><body></body></html>"
    )
    extracted = extract_article_from_html(item, html_doc)
    assert extracted.extraction_strategy == "embedded_json"
    assert "коммунальные службы" in extracted.text


def test_architecture_core1_metadata_fallback_is_explicit_in_trace():
    src = direct_web_source(
        "Белсат", "ru.belsat.eu", "https://ru.belsat.eu/", "belsat_article"
    )
    item = Candidate(
        source=src,
        url="https://ru.belsat.eu/94722291/test",
        title="Карточка",
        summary="",
    )
    html = """
    <html><head>
      <meta property="og:description"
      content="Жители сообщили о проблеме с коммунальной услугой и ждут ответа обслуживающей организации.">
    </head><body></body></html>
    """
    extracted = extract_article_from_html(item, html)
    assert extracted.extraction_strategy == "metadata_description"
    assert extracted.metadata_summary
    assert extracted.text == extracted.metadata_summary


def test_architecture_core1_process_candidate_detailed_keeps_editorial_result(monkeypatch):
    item = candidate(
        title="Карточка новости",
        summary="",
        published_at="2026-07-30T07:00:00+00:00",
    )
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda *_args, **_kwargs: ArticleExtraction(
            title="Жители Минска жалуются на разбитую дорогу",
            text=(
                "Жители несколько лет не могут добиться ремонта дороги. "
                "На улице глубокие ямы."
            ),
            published_at=item.published_at,
            extraction_strategy="generic_html",
            transport="requests",
            transport_status="ok",
        ),
    )
    result, trace = social_monitor.process_candidate_detailed(
        item,
        SETTINGS,
        dt.datetime(2026, 7, 29, tzinfo=UTC),
        True,
    )
    assert result is not None
    assert trace.final_stage == "included"
    assert trace.relevance_passed is True
    assert trace.transport == "requests"
    assert trace.extraction_strategy == "generic_html"


def test_result_integrity_rejection_reason_is_exposed_in_trace(monkeypatch):
    item = candidate(
        title="Как пить чай из черной смородины",
        summary="",
        published_at="2026-08-18T07:00:00+00:00",
    )
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda *_args, **_kwargs: ArticleExtraction(
            title=item.title,
            text=(
                "Специалист объяснил, как заварить листья и сколько чашек "
                "напитка можно выпить в течение дня."
            ),
            published_at=item.published_at,
            extraction_strategy="generic_html",
            transport="requests",
            transport_status="ok",
        ),
    )
    result, trace = social_monitor.process_candidate_detailed(
        item,
        SETTINGS,
        dt.datetime(2026, 8, 17, tzinfo=UTC),
        True,
    )
    assert result is None
    assert trace.final_stage == "relevance_rejected"
    assert trace.rejection_reason == "нет связки социальной темы и проблемы"


def test_architecture_core1_source_processing_metrics_aggregate_stages():
    metrics = social_monitor.SourceProcessingMetrics()
    metrics.add(social_monitor.CandidateProcessingTelemetry(
        prefilter_status="strong",
        transport="requests",
        transport_status="ok",
        extraction_strategy="json_ld",
        text_length=900,
        relevance_passed=True,
        publication_allowed=True,
        excerpt_built=True,
        final_stage="included",
    ))
    metrics.add(social_monitor.CandidateProcessingTelemetry(
        prefilter_status="needs_text",
        transport="official_mirror",
        transport_status="failed",
        extraction_strategy="empty",
        text_length=0,
        extraction_failed=True,
        final_stage="relevance_rejected",
    ))
    assert metrics.processed == 2
    assert metrics.prefilter_strong == 1
    assert metrics.fetch_ok == 1
    assert metrics.fetch_failed == 1
    assert metrics.extraction_json_ld == 1
    assert metrics.extraction_failed == 1
    assert metrics.included == 1


def test_architecture_core1_source_coverage_exposes_profiles_and_processing_stages():
    src = source("Минск", "Минск")
    cmetrics = social_monitor.SourceCollectionMetrics(
        feed_candidates=4,
        listing_candidates=3,
        homepage_candidates=2,
        merged_candidates=7,
        selected_candidates=5,
        selected_listing=2,
        source_limit=35,
    )
    pmetrics = social_monitor.SourceProcessingMetrics()
    pmetrics.add(social_monitor.CandidateProcessingTelemetry(
        prefilter_status="possible",
        transport="requests",
        transport_status="ok",
        extraction_strategy="generic_html",
        text_length=500,
        relevance_passed=False,
        final_stage="relevance_rejected",
    ))
    rows = social_monitor.build_source_coverage(
        [src], [], [], [], [],
        {(src.country, src.name): cmetrics},
        {(src.country, src.name): pmetrics},
        SETTINGS,
    )
    row = rows[0]
    assert row["listing_candidates"] == 3
    assert row["processed"] == 1
    assert row["prefilter_possible"] == 1
    assert row["extraction_generic_html"] == 1
    assert row["relevance_rejected"] == 1
    assert row["source_region"] == "Минск"
    assert row["event_region"] == ""
    assert "generic_html" in row["extraction_order"]


# --- Architecture Core 2: recovery, circuit breaker and degraded queue. ---


def test_architecture_core2_http_failure_classes_are_differentiated():
    assert social_monitor.classify_http_failure(403) == "permanent_http"
    assert social_monitor.classify_http_failure(429) == "rate_limited"
    assert social_monitor.classify_http_failure(503) == "transient_http"
    assert social_monitor.classify_http_failure(418) == "client_http"


def test_architecture_core2_permanent_http_is_not_retried(monkeypatch):
    calls = []

    class Response:
        status_code = 403
        content = b"blocked"
        url = "https://example.com/news/blocked"

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr(social_monitor.requests, "get", fake_get)
    client = social_monitor.HttpClient(SETTINGS)
    assert client.get("https://example.com/news/blocked", retries=3) is None
    assert len(calls) == 1
    observation = client.observation_for("https://example.com/news/blocked")
    assert observation is not None
    assert observation.status_code == 403
    assert observation.attempts == 1
    assert observation.failure_class == "permanent_http"


def test_architecture_core2_endpoint_circuit_breaker_and_tail_probe():
    state = {}
    src = source()
    now = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    controller = social_monitor.RecoveryController(state, SETTINGS, now)
    endpoint = "https://example.com/rss"
    failed = social_monitor.HttpObservation(
        url=endpoint,
        status_code=503,
        attempts=2,
        outcome="failed",
        failure_class="transient_http",
    )
    for _ in range(3):
        controller.record_endpoint(src, "feed", endpoint, failed, 0)
    assert controller.endpoint_decision(src, "feed", endpoint) == "skip"

    later = social_monitor.RecoveryController(
        state, SETTINGS, now + dt.timedelta(hours=7)
    )
    assert later.endpoint_decision(src, "feed", endpoint) == "tail_probe"
    ok = social_monitor.HttpObservation(
        url=endpoint, status_code=200, attempts=1, outcome="ok"
    )
    later.record_endpoint(src, "feed", endpoint, ok, 12, "tail_probe")
    assert later.endpoint_decision(src, "feed", endpoint) == "normal"


def test_architecture_core2_endpoint_history_detects_sharp_candidate_drop():
    state = {}
    src = source()
    endpoint = "https://example.com/rss"
    base = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    ok = social_monitor.HttpObservation(
        url=endpoint, status_code=200, attempts=1, outcome="ok"
    )
    for day, count in enumerate((52, 48, 55)):
        controller = social_monitor.RecoveryController(
            state, SETTINGS, base + dt.timedelta(days=day)
        )
        assert controller.record_endpoint(
            src, "feed", endpoint, ok, count
        ) == "ok"
    controller = social_monitor.RecoveryController(
        state, SETTINGS, base + dt.timedelta(days=3)
    )
    assert controller.record_endpoint(src, "feed", endpoint, ok, 2) == "degraded"


def test_architecture_core2_telegram_profile_matches_actual_transport():
    ordinary = source(media_type="telegram")
    linked = Source(**{
        **ordinary.__dict__,
        "name": "Reform.news",
        "domain": "reform.news",
        "start_url": "https://t.me/reformby",
        "adapter": "telegram_linked_site",
    })
    assert social_monitor.effective_source_profile(
        ordinary, SETTINGS
    )["transport_order"] == ["telegram_inline"]
    assert social_monitor.effective_source_profile(
        linked, SETTINGS
    )["transport_order"] == ["telegram_inline"]


def test_architecture_core2_metadata_only_cannot_enter_report(monkeypatch):
    item = candidate(
        title="Жители жалуются на отсутствие воды",
        summary="Жители несколько дней ждут восстановления водоснабжения.",
        published_at="2026-08-07T08:00:00+00:00",
    )
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda *args, **kwargs: ArticleExtraction(
            title=item.title,
            text="Жители несколько дней ждут восстановления водоснабжения.",
            published_at=item.published_at,
            extraction_strategy="metadata_description",
            transport="requests",
            transport_status="ok",
        ),
    )
    result, trace = social_monitor.process_candidate_detailed(
        item,
        SETTINGS,
        dt.datetime(2026, 8, 6, tzinfo=UTC),
        True,
    )
    assert result is None
    assert trace.final_stage == "degraded_queued"
    assert trace.degraded_reason == "metadata_only"
    assert trace.relevance_passed is False


def test_architecture_core2_successful_retry_is_marked_recovered(monkeypatch):
    item = candidate(
        title="Жители жалуются на разбитую дорогу",
        url="https://example.com/2026/08/07/retry-story",
        published_at="2026-08-07T08:00:00+00:00",
    )
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda *args, **kwargs: ArticleExtraction(
            title=item.title,
            text=(
                "Жители несколько лет не могут добиться ремонта дороги. "
                "На улице глубокие ямы, обращения не помогли."
            ),
            published_at=item.published_at,
            extraction_strategy="generic_html",
            transport="requests",
            transport_status="ok",
        ),
    )
    result, trace = social_monitor.process_candidate_detailed(
        item,
        SETTINGS,
        dt.datetime(2026, 8, 6, tzinfo=UTC),
        True,
        recovery_retry=True,
    )
    assert result is not None
    assert trace.recovery_retry is True
    assert trace.recovery_recovered is True
    assert trace.final_stage == "included"


def test_architecture_core2_degraded_queue_retries_on_schedule():
    state = {}
    src = source()
    item = Candidate(
        source=src,
        url="https://example.com/news/degraded-story",
        title="Жители жалуются на отсутствие воды",
        summary="Короткий официальный анонс",
        published_at="2026-08-07T08:00:00+00:00",
        discovered_via="feed:https://example.com/rss",
    )
    now = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    controller = social_monitor.RecoveryController(state, SETTINGS, now)
    trace = social_monitor.CandidateProcessingTelemetry(
        final_stage="degraded_queued",
        degraded_reason="metadata_only",
        transport="requests",
        extraction_strategy="metadata_description",
        metadata_only=True,
    )
    assert controller.queue_degraded(item, trace) == "active"
    assert controller.should_defer_url(item.url) is True
    assert controller.due_candidates([src]) == []

    later = social_monitor.RecoveryController(
        state, SETTINGS, now + dt.timedelta(hours=7)
    )
    due = later.due_candidates([src])
    assert len(due) == 1
    assert due[0].url == canonicalize_url(item.url)


def test_architecture_core2_degraded_queue_is_bounded():
    state = {}
    src = source()
    item = Candidate(
        source=src,
        url="https://example.com/news/always-empty",
        title="Карточка",
    )
    trace = social_monitor.CandidateProcessingTelemetry(
        final_stage="degraded_queued",
        degraded_reason="extraction_failed",
        extraction_failed=True,
    )
    base = dt.datetime(2026, 8, 7, 8, 0, tzinfo=UTC)
    status = ""
    for index in range(3):
        controller = social_monitor.RecoveryController(
            state, SETTINGS, base + dt.timedelta(days=index)
        )
        status = controller.queue_degraded(item, trace)
    assert status == "exhausted"
    final = social_monitor.RecoveryController(
        state, SETTINGS, base + dt.timedelta(days=3)
    )
    assert final.due_candidates([src]) == []
    assert final.should_defer_url(item.url) is True


def test_architecture_core2_transport_circuit_skips_expensive_retry(monkeypatch):
    state = {}
    src = Source(
        enabled=True, country="Беларусь", country_code="BY",
        locality="Беларусь", rank=1, priority="A", name="Позірк",
        media_type="website", domain="pozirk.online",
        start_url="https://pozirk.online/ru/news/", language="ru",
        adapter="protected_article",
    )
    item = Candidate(
        source=src,
        url="https://pozirk.online/ru/news/123456/test-story",
        title="Карточка",
    )
    now = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    controller = social_monitor.RecoveryController(state, SETTINGS, now)
    for _ in range(3):
        controller.record_transport(
            src, "requests", False, 503, "transient_http"
        )

    monkeypatch.setattr(
        social_monitor.HttpClient,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("open circuit must skip request")
        ),
    )
    extracted = social_monitor.extract_article(item, SETTINGS, controller)
    assert extracted.transport_status == "failed"
    assert extracted.transport_circuit_skipped is True
    assert extracted.transport_failure_class == "circuit_open"


def test_architecture_core2_processing_metrics_expose_recovery_stages():
    metrics = social_monitor.SourceProcessingMetrics()
    metrics.add(social_monitor.CandidateProcessingTelemetry(
        recovery_retry=True,
        recovery_recovered=True,
        transport="requests",
        transport_status="ok",
        extraction_strategy="generic_html",
        text_length=500,
        final_stage="relevance_rejected",
    ))
    metrics.add(social_monitor.CandidateProcessingTelemetry(
        transport="requests",
        transport_status="failed",
        extraction_failed=True,
        degraded_reason="transport_failed",
        transport_circuit_skipped=True,
        final_stage="degraded_queued",
    ))
    assert metrics.recovery_retried == 1
    assert metrics.recovery_recovered == 1
    assert metrics.degraded_queued == 1
    assert metrics.transport_circuit_skipped == 1


def test_architecture_core2_source_coverage_exposes_recovery_health():
    src = source()
    recovery_state = {}
    recovery = social_monitor.RecoveryController(
        recovery_state, SETTINGS, dt.datetime(2026, 8, 7, 12, tzinfo=UTC)
    )
    recovery.record_transport(src, "requests", False, 503, "transient_http")
    cmetrics = social_monitor.SourceCollectionMetrics(
        endpoint_total=3,
        endpoint_ok=1,
        endpoint_failed=1,
        endpoint_degraded=1,
        endpoint_tail_probes=1,
        merged_candidates=5,
        selected_candidates=5,
    )
    pmetrics = social_monitor.SourceProcessingMetrics(degraded_queued=2)
    rows = social_monitor.build_source_coverage(
        [src], [], [], [], [],
        {(src.country, src.name): cmetrics},
        {(src.country, src.name): pmetrics},
        SETTINGS,
        recovery,
    )
    row = rows[0]
    assert row["endpoint_total"] == 3
    assert row["endpoint_failed"] == 1
    assert row["endpoint_degraded"] == 1
    assert row["endpoint_tail_probes"] == 1
    assert row["degraded_queued"] == 2


def test_architecture_core2_ordinary_source_transport_is_never_circuit_skipped():
    state = {}
    src = source()
    now = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    controller = social_monitor.RecoveryController(state, SETTINGS, now)
    for _ in range(5):
        controller.record_transport(src, "requests", False, 503, "transient_http")
    assert controller.transport_decision(src, "requests") == "normal"


def test_architecture_core2_belsat_can_recover_directly_with_chromium(monkeypatch):
    src = Source(
        enabled=True, country="Беларусь", country_code="BY",
        locality="Беларусь", rank=1, priority="A", name="Белсат",
        media_type="website", domain="ru.belsat.eu",
        start_url="https://ru.belsat.eu/", language="ru",
        adapter="belsat_article",
    )
    item = Candidate(
        source=src,
        url="https://ru.belsat.eu/94722291/test-story",
        title="Карточка",
    )
    monkeypatch.setattr(social_monitor.HttpClient, "get", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        social_monitor,
        "render_belsat_article_html",
        lambda *_args, **_kwargs: (
            "<html><body><div class='article__content'>"
            "<p>Жители жалуются на длительную коммунальную проблему.</p>"
            "<p>Обращения в обслуживающую организацию пока не помогли.</p>"
            "</div></body></html>"
        ),
    )
    extracted = social_monitor.extract_article(item, SETTINGS)
    assert extracted.transport == "chromium"
    assert extracted.transport_status == "ok"
    assert "коммунальную проблему" in extracted.text


# --- Architecture Core 3: event geography and Event Echo. ---


def test_architecture_core3_event_geography_is_article_based_not_source_based(monkeypatch):
    src = source("Брестская область", "Брест")
    item = Candidate(
        source=src,
        url="https://example.com/2026/08/07/orsha-road",
        title="Жители Орши жалуются на разбитую дорогу",
        summary="В Орше дорога покрыта глубокими ямами.",
        published_at="2026-08-07T10:00:00+00:00",
    )
    monkeypatch.setattr(
        social_monitor,
        "extract_article",
        lambda candidate, settings: ArticleExtraction(
            title=candidate.title,
            text=(
                "Жители Орши жалуются на разбитую дорогу. "
                "Дорога покрыта глубокими ямами, обращения не помогли."
            ),
            transport="requests",
            transport_status="ok",
            extraction_strategy="generic_html",
        ),
    )
    result, trace = social_monitor.process_candidate_detailed(
        item,
        SETTINGS,
        dt.datetime(2026, 8, 6, tzinfo=UTC),
        True,
    )
    assert result is not None
    assert result.country == "Брестская область"
    assert result.locality == "Брест"
    assert result.event_region == "Витебская область"
    assert result.event_locality == "Орша"
    assert trace.event_locality == "Орша"


def test_architecture_core3_never_falls_back_to_source_locality():
    region, locality = social_monitor.infer_event_geography(
        "Жители жалуются на разбитую дорогу",
        "Проблема сохраняется несколько месяцев.",
        "Обращения жителей пока не помогли.",
    )
    assert region == ""
    assert locality == ""


def test_architecture_core3_ambiguous_localities_are_not_collapsed():
    region, locality = social_monitor.infer_event_geography(
        "В Орше и Витебске обсуждают состояние дорог",
        "",
        "Жители двух городов рассказали о проблемах.",
    )
    assert region == "Витебская область"
    assert locality == ""


def test_architecture_core3_builds_conservative_event_signature():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Жители Орши жалуются на разбитую дорогу",
        "Дорога покрыта ямами.",
        "Жители несколько месяцев просят отремонтировать проезжую часть.",
    )
    assert fingerprint.region == "Витебская область"
    assert fingerprint.locality == "Орша"
    assert fingerprint.object_key == "road"
    assert fingerprint.problem_key == "damage"
    assert fingerprint.signature == "орша|road|damage"


def test_architecture_core3_signature_requires_problem_not_just_place_and_object():
    fingerprint = social_monitor.infer_event_fingerprint(
        "В Орше открыли новую дорогу",
        "Движение началось сегодня.",
        "Новая дорога связала два района города.",
    )
    assert fingerprint.locality == "Орша"
    assert fingerprint.object_key == "road"
    assert fingerprint.problem_key == ""
    assert fingerprint.signature == ""


def _core3_event_result(url: str, source_name: str, published_at: str) -> social_monitor.ArticleResult:
    result = make_result(url, "Брест")
    result.source_name = source_name
    result.published_at = published_at
    result.event_region = "Витебская область"
    result.event_locality = "Орша"
    result.event_object = "дорога/улица"
    result.event_problem = "повреждение/плохое состояние"
    result.event_signature = "орша|road|damage"
    return result


def test_architecture_core3_current_cross_source_results_get_event_echo():
    first = _core3_event_result(
        "https://a.example/story", "Источник A", "2026-08-07T10:00:00+00:00"
    )
    second = _core3_event_result(
        "https://b.example/story", "Источник B", "2026-08-07T11:00:00+00:00"
    )
    social_monitor.apply_event_echo({}, [first, second], None, SETTINGS)
    assert first.event_echo is True
    assert second.event_echo is True
    assert first.event_echo_anchor == "current"
    assert first.event_echo_sources == "Источник B"


def test_architecture_core3_same_source_is_not_event_echo():
    first = _core3_event_result(
        "https://a.example/one", "Источник A", "2026-08-07T10:00:00+00:00"
    )
    second = _core3_event_result(
        "https://a.example/two", "Источник A", "2026-08-07T11:00:00+00:00"
    )
    social_monitor.apply_event_echo({}, [first, second], None, SETTINGS)
    assert first.event_echo is False
    assert second.event_echo is False


def test_architecture_core3_event_echo_respects_time_window():
    first = _core3_event_result(
        "https://a.example/old", "Источник A", "2026-08-01T10:00:00+00:00"
    )
    second = _core3_event_result(
        "https://b.example/new", "Источник B", "2026-08-07T10:00:00+00:00"
    )
    social_monitor.apply_event_echo({}, [first, second], None, SETTINGS)
    assert first.event_echo is False
    assert second.event_echo is False


def test_architecture_core3_degraded_echo_gets_recovery_priority():
    anchor = _core3_event_result(
        "https://a.example/story", "Источник A", "2026-08-07T10:00:00+00:00"
    )
    src = Source(
        enabled=True, country="Витебская область", country_code="BY-VI",
        locality="Витебск", rank=1, priority="A", name="Источник B",
        media_type="website", domain="b.example",
        start_url="https://b.example/", language="ru", adapter="standard",
    )
    item = Candidate(
        source=src,
        url="https://b.example/story",
        title="В Орше разбитая дорога",
        published_at="2026-08-07T11:00:00+00:00",
    )
    trace = social_monitor.CandidateProcessingTelemetry(
        final_stage="degraded_queued",
        degraded_reason="metadata_only",
        event_region="Витебская область",
        event_locality="Орша",
        event_object="дорога/улица",
        event_problem="повреждение/плохое состояние",
        event_signature="орша|road|damage",
        event_published_at=item.published_at,
    )
    outcomes = {social_monitor.canonicalize_url(item.url): (item, trace)}
    social_monitor.apply_event_echo(outcomes, [anchor], None, SETTINGS)
    assert trace.event_echo is True
    assert trace.event_echo_anchor == "current"
    assert trace.event_echo_sources == ("Источник A",)
    assert trace.event_echo_priority is True
    assert trace.final_stage == "degraded_queued"


def test_architecture_core3_degraded_echo_can_use_persisted_seed():
    state = {}
    now = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    controller = social_monitor.RecoveryController(state, SETTINGS, now)
    anchor = _core3_event_result(
        "https://a.example/story", "Источник A", "2026-08-07T10:00:00+00:00"
    )
    controller.remember_event_seed(anchor)

    src = Source(
        enabled=True, country="Витебская область", country_code="BY-VI",
        locality="Витебск", rank=1, priority="A", name="Источник B",
        media_type="website", domain="b.example",
        start_url="https://b.example/", language="ru", adapter="standard",
    )
    item = Candidate(
        source=src,
        url="https://b.example/story",
        published_at="2026-08-07T11:00:00+00:00",
    )
    trace = social_monitor.CandidateProcessingTelemetry(
        final_stage="degraded_queued",
        degraded_reason="extraction_failed",
        event_signature="орша|road|damage",
        event_published_at=item.published_at,
    )
    outcomes = {social_monitor.canonicalize_url(item.url): (item, trace)}
    social_monitor.apply_event_echo(outcomes, [], controller, SETTINGS)
    assert trace.event_echo is True
    assert trace.event_echo_anchor == "state"
    assert trace.event_echo_sources == ("Источник A",)
    assert trace.event_echo_priority is True


def test_architecture_core3_event_echo_accelerates_degraded_retry():
    state = {}
    now = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    controller = social_monitor.RecoveryController(state, SETTINGS, now)
    item = Candidate(
        source=source(),
        url="https://example.com/echo-degraded",
        title="В Орше разбитая дорога",
        published_at="2026-08-07T11:00:00+00:00",
    )
    trace = social_monitor.CandidateProcessingTelemetry(
        final_stage="degraded_queued",
        degraded_reason="metadata_only",
        event_echo=True,
        event_echo_priority=True,
        event_signature="орша|road|damage",
    )
    controller.queue_degraded(item, trace)
    record = state["recovery"]["degraded_queue"][
        social_monitor.canonicalize_url(item.url)
    ]
    next_retry = social_monitor.parse_datetime(record["next_retry_at"])
    assert next_retry is not None
    assert next_retry - now == dt.timedelta(hours=1)
    assert record["event_echo_priority"] is True


def test_architecture_core3_processing_metrics_track_geo_and_echo():
    metrics = social_monitor.SourceProcessingMetrics()
    trace = social_monitor.CandidateProcessingTelemetry(
        event_region="Витебская область",
        event_locality="Орша",
        event_signature="орша|road|damage",
        event_echo=True,
        event_echo_anchor="current",
        event_echo_priority=True,
        final_stage="degraded_queued",
    )
    metrics.add(trace)
    assert metrics.event_geo_resolved == 1
    assert metrics.event_signature_ready == 1
    assert metrics.event_echo_hits == 1
    assert metrics.event_echo_current == 1
    assert metrics.event_echo_degraded_prioritized == 1
    assert metrics.event_regions == {"Витебская область"}
    assert metrics.event_localities == {"Орша"}


def test_architecture_core3_source_coverage_exposes_resolved_event_geography():
    src = source("Брестская область", "Брест")
    pmetrics = social_monitor.SourceProcessingMetrics()
    pmetrics.event_regions.add("Витебская область")
    pmetrics.event_localities.update({"Орша", "Витебск"})
    pmetrics.event_geo_resolved = 2
    pmetrics.event_signature_ready = 1
    pmetrics.event_echo_hits = 1
    rows = social_monitor.build_source_coverage(
        [src], [], [], [], [],
        processing_metrics={(src.country, src.name): pmetrics},
        settings=SETTINGS,
    )
    row = rows[0]
    assert row["source_region"] == "Брестская область"
    assert row["source_locality"] == "Брест"
    assert row["event_region"] == "Витебская область"
    assert row["event_locality"] == "Витебск; Орша"
    assert row["event_geo_resolved"] == 2
    assert row["event_echo_hits"] == 1


def test_architecture_core3_article_csv_contains_event_echo_fields(tmp_path):
    result = _core3_event_result(
        "https://a.example/story", "Источник A", "2026-08-07T10:00:00+00:00"
    )
    result.event_echo = True
    result.event_echo_anchor = "current"
    result.event_echo_sources = "Источник B"
    path = tmp_path / "articles.csv"
    social_monitor.write_csv_report(path, [result])
    text = path.read_text(encoding="utf-8-sig")
    header = text.splitlines()[0]
    assert "event_region" in header
    assert "event_locality" in header
    assert "event_signature" in header
    assert "event_echo" in header
    assert "Источник B" in text


def test_html_hides_key_signals_while_csv_preserves_subcategory(tmp_path):
    result = make_result("https://example.com/human-report", "Минск")
    result.subcategory = "internal-subcategory-marker-39"

    report = social_monitor.build_html_report(
        [result], [], SETTINGS, warmup=False, coverage=[]
    )
    assert "Ключевые признаки" not in report
    assert "internal-subcategory-marker-39" not in report
    assert 'class="subcategory"' not in report

    csv_path = tmp_path / "articles.csv"
    social_monitor.write_csv_report(csv_path, [result])
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "internal-subcategory-marker-39" in csv_text


def test_architecture_core3_national_headline_does_not_inherit_example_locality():
    region, locality = social_monitor.infer_event_geography(
        "Белорусам объяснили новое правило",
        "В качестве примера приводится житель Пинска.",
        "",
    )
    assert region == ""
    assert locality == ""


def test_architecture_core3_parking_is_not_collapsed_into_road_object():
    fingerprint = social_monitor.infer_event_fingerprint(
        "В Минске покупателям предлагают сразу два машино-места",
        "Жители спорят из-за стоимости парковки.",
        "",
    )
    assert fingerprint.locality == "Минск"
    assert fingerprint.object_key == "parking"


def test_architecture_core3_specific_station_storage_object_beats_generic_transport():
    fingerprint = social_monitor.infer_event_fingerprint(
        "В Минске большие очереди в камеры хранения на вокзале",
        "Пассажиры жалуются на очереди к камерам хранения.",
        "",
    )
    assert fingerprint.locality == "Минск"
    assert fingerprint.object_key == "station_storage"
    assert fingerprint.problem_key == "queue_delay"
    assert fingerprint.signature == "минск|station_storage|queue_delay"


# --- Architecture Core 3.1: conservative fingerprint boundaries. ---


def test_architecture_core31_expensive_mushrooms_are_not_a_road_object():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Дорогие лисички! Белоруска показала цены",
        "Покупатели спорят, дорого ли продавать грибы по 15 рублей.",
        "Комментаторы обсуждают слишком дорогие лисички и магазинные цены.",
    )
    assert fingerprint.object_key != "road"
    assert fingerprint.signature == ""


def test_architecture_core31_housing_waiting_status_is_not_queue_delay():
    fingerprint = social_monitor.infer_event_fingerprint(
        "В Минске квартиру предлагают вместе с двумя машино-местами",
        "Новостройка предназначена для очередников.",
        (
            "Претендовать могут граждане, состоящие на учете нуждающихся "
            "в улучшении жилищных условий. Достаточно справки о том, что "
            "вы стоите в очереди."
        ),
    )
    assert fingerprint.locality == "Минск"
    assert fingerprint.object_key == "parking"
    assert fingerprint.problem_key != "queue_delay"
    assert fingerprint.signature == ""


def test_architecture_core31_construction_phase_is_not_queue_delay():
    fingerprint = social_monitor.infer_event_fingerprint(
        "В Минске началась первая очередь строительства нового квартала",
        "Возведение объекта разбито на 24 очереди.",
        "Следующая очередь строительства стартует осенью.",
    )
    assert fingerprint.locality == "Минск"
    assert fingerprint.problem_key != "queue_delay"
    assert fingerprint.signature == ""


def test_architecture_core31_amusement_park_social_text_is_not_damage():
    fingerprint = social_monitor.infer_event_fingerprint(
        "В главном парке Бобруйска скоро заработает новый аттракцион",
        "Монтаж завершат после получения необходимых разрешений.",
        "Новостью можно поделиться с друзьями. Аттракцион скоро откроют.",
    )
    assert fingerprint.locality == "Бобруйск"
    assert fingerprint.object_key == "greenery"
    assert fingerprint.problem_key != "damage"
    assert fingerprint.signature == ""


def test_architecture_core31_real_road_pits_still_make_damage_signature():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Жители Орши жалуются на разбитую дорогу",
        "На дороге глубокие ямы.",
        "Люди месяцами ждут ремонта, обращения не помогли.",
    )
    assert fingerprint.object_key == "road"
    assert fingerprint.problem_key == "damage"
    assert fingerprint.signature == "орша|road|damage"


# --- Architecture Core 3.1.1: restore observed road-marking damage. ---


def test_architecture_core311_erased_road_marking_makes_damage_signature():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Гомельчане жалуются на стертую дорожную разметку на улицах города",
        (
            "В этом году дорожную разметку в Гомеле обновили частично лишь "
            "на центральных улицах, пропустив второстепенные улицы и сложные "
            "перекрестки и развязки."
        ),
        "Жители называют ситуацию удручающей и ждут обновления разметки.",
    )
    assert fingerprint.region == "Гомельская область"
    assert fingerprint.locality == "Гомель"
    assert fingerprint.object_key == "road"
    assert fingerprint.problem_key == "damage"
    assert fingerprint.signature == "гомель|road|damage"


# --- Architecture Core 3.2a: passive access and cost telemetry. ---


def test_architecture_core32a_http_observation_records_cost_without_retry_change(
    monkeypatch,
):
    calls = []

    class Response:
        status_code = 403
        content = b"blocked"
        url = "https://example.com/news/blocked"

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    ticks = iter((100.0, 100.25))
    monkeypatch.setattr(social_monitor.requests, "get", fake_get)
    monkeypatch.setattr(social_monitor.time, "perf_counter", lambda: next(ticks))
    client = social_monitor.HttpClient(SETTINGS)
    assert client.get("https://example.com/news/blocked", retries=3) is None
    observations = client.observations_since(0)
    assert len(calls) == 1
    assert len(observations) == 1
    assert observations[0].failure_class == "permanent_http"
    assert observations[0].attempts == 1
    assert observations[0].seconds == 0.25


def test_architecture_core32a_clipped_pool_is_measured_but_not_admitted(
    monkeypatch,
):
    src = source()
    cutoff = dt.datetime(2026, 8, 7, tzinfo=UTC)
    items = [
        Candidate(
            source=src,
            url=f"https://example.com/2026/08/08/item-{index}",
            title=f"Материал {index}",
            published_at="2026-08-08T10:00:00+00:00",
            discovered_via="feed:https://example.com/rss",
        )
        for index in range(3)
    ]
    monkeypatch.setattr(social_monitor, "source_candidate_limit", lambda *_: 1)
    monkeypatch.setattr(
        social_monitor,
        "discover_endpoints",
        lambda *args, **kwargs: {
            "feeds": ["https://example.com/rss"],
            "sitemaps": [],
            "listing_pages": [],
            "skip_homepage": True,
        },
    )
    monkeypatch.setattr(
        social_monitor, "collect_from_feed", lambda *args, **kwargs: list(items)
    )
    selected, error, metrics = social_monitor.collect_source_candidates(
        src, SETTINGS, {}, cutoff, seen_urls=set()
    )
    assert error is None
    assert len(selected) == 1
    assert metrics.merged_candidates == 3
    assert metrics.clipped_candidates == 2
    assert metrics.clipped_fresh == 2
    assert metrics.clipped_unseen == 2
    assert (
        metrics.clipped_prefilter_strong
        + metrics.clipped_prefilter_possible
        + metrics.clipped_prefilter_needs_text
    ) == 2


def test_architecture_core32a_processing_costs_aggregate_by_source():
    metrics = social_monitor.SourceProcessingMetrics()
    metrics.add(social_monitor.CandidateProcessingTelemetry(
        processing_seconds=3.5,
        http_seconds=1.25,
        extraction_seconds=0.4,
        chromium_seconds=1.8,
        chromium_attempts=1,
        http_attempts=2,
    ))
    metrics.add(social_monitor.CandidateProcessingTelemetry(
        processing_seconds=1.0,
        http_seconds=0.75,
        extraction_seconds=0.2,
        http_attempts=1,
    ))
    assert metrics.processing_seconds == 4.5
    assert metrics.processing_max_seconds == 3.5
    assert metrics.http_seconds == 2.0
    assert round(metrics.extraction_seconds, 6) == 0.6
    assert metrics.chromium_seconds == 1.8
    assert metrics.chromium_attempts == 1
    assert metrics.http_attempts == 3


def test_architecture_core32a_coverage_classifies_extraction_blindness():
    src = source()
    cmetrics = social_monitor.SourceCollectionMetrics(
        merged_candidates=4,
        selected_candidates=4,
        endpoint_total=1,
        endpoint_ok=1,
        discovery_seconds=1.2,
    )
    pmetrics = social_monitor.SourceProcessingMetrics(
        processed=4,
        fetch_ok=4,
        extraction_failed=4,
        processing_seconds=8.0,
        http_seconds=5.0,
    )
    row = social_monitor.build_source_coverage(
        [src], [], [], [], [],
        {(src.country, src.name): cmetrics},
        {(src.country, src.name): pmetrics},
        SETTINGS,
    )[0]
    assert row["blind_zone_status"] == "extraction_blind"
    assert row["discovery_seconds"] == 1.2
    assert row["processing_seconds"] == 8.0
    assert row["http_seconds"] == 5.0


def test_architecture_core32a_access_csv_contains_failure_url_and_run_timing(
    tmp_path,
):
    src = source()
    endpoint = social_monitor.EndpointTelemetry(
        channel="feed",
        endpoint="https://example.com/rss",
        outcome="failed",
        status_code=503,
        failure_class="transient_http",
        attempts=2,
        seconds=3.25,
    )
    cmetrics = social_monitor.SourceCollectionMetrics(
        endpoint_total=1,
        endpoint_failed=1,
        endpoint_observations=(endpoint,),
    )
    coverage = social_monitor.build_source_coverage(
        [src], [], [], [], [],
        {(src.country, src.name): cmetrics},
        settings=SETTINGS,
    )
    path = tmp_path / "access.csv"
    social_monitor.write_access_telemetry_csv(
        path,
        [src],
        {(src.country, src.name): cmetrics},
        {},
        coverage,
        {"discovery_seconds": 4.5},
    )
    text = path.read_text(encoding="utf-8-sig")
    assert "run_stage" in text
    assert "https://example.com/rss" in text
    assert "transient_http" in text
    assert "3.25" in text


def test_architecture_core32a_dry_run_writes_telemetry_without_persisting_state(
    tmp_path, monkeypatch,
):
    src = source()
    item = Candidate(
        source=src,
        url="https://example.com/2026/08/08/diagnostic",
        title="Диагностический материал",
        published_at="2026-08-08T10:00:00+00:00",
        discovered_via="feed:https://example.com/rss",
    )
    project = tmp_path
    (project / "data").mkdir()
    state_path = project / "data" / "state.json"
    cache_path = project / "data" / "discovery_cache.json"
    state_before = '{"initialized": true, "seen": {}, "marker": "keep"}'
    cache_before = '{"marker": "keep"}'
    state_path.write_text(state_before, encoding="utf-8")
    cache_path.write_text(cache_before, encoding="utf-8")

    settings = copy.deepcopy(SETTINGS)
    settings["monitor"]["warmup_on_first_run"] = False
    monkeypatch.setattr(social_monitor, "load_settings", lambda _path: settings)
    monkeypatch.setattr(social_monitor, "load_sources", lambda _path: [src])
    monkeypatch.setattr(
        social_monitor,
        "collect_source_candidates",
        lambda *args, **kwargs: (
            [item],
            None,
            social_monitor.SourceCollectionMetrics(
                feed_candidates=1,
                merged_candidates=1,
                selected_candidates=1,
                endpoint_total=1,
                endpoint_ok=1,
                discovery_seconds=0.1,
            ),
        ),
    )
    monkeypatch.setattr(
        social_monitor,
        "process_candidate_detailed",
        lambda *args, **kwargs: (
            None,
            social_monitor.CandidateProcessingTelemetry(
                transport="requests",
                transport_status="ok",
                extraction_strategy="generic_html",
                text_length=400,
                processing_seconds=0.2,
                http_seconds=0.1,
                final_stage="relevance_rejected",
            ),
        ),
    )
    monkeypatch.setattr(social_monitor, "build_html_report", lambda *args: "ok")
    monkeypatch.setattr(
        social_monitor,
        "write_csv_report",
        lambda path, _results: path.write_text("ok", encoding="utf-8"),
    )

    summary = social_monitor.run_monitor(project, dry_run=True)
    assert state_path.read_text(encoding="utf-8") == state_before
    assert cache_path.read_text(encoding="utf-8") == cache_before
    telemetry_path = social_monitor.Path(summary["access_telemetry_report"])
    assert telemetry_path.exists()
    assert "article_processing" in telemetry_path.read_text(encoding="utf-8-sig")
    assert summary["email_sent"] is False
    assert summary["telegram_sent"] is False


# --- Architecture Core 3.2a.1: discovery access recovery. ---


def test_architecture_core32a1_website_collects_configured_telegram_fallback(
    monkeypatch,
):
    src = source()
    src = social_monitor.replace(src, telegram_url="https://t.me/example_news")
    fallback = Candidate(
        source=src,
        url="https://example.com/2026/08/08/fallback",
        title="Материал из официальной резервной ленты",
        published_at="2026-08-08T10:00:00+00:00",
        discovered_via="telegram:https://t.me/s/example_news",
        inline_text="Полный текст официальной публикации из резервной ленты.",
    )
    monkeypatch.setattr(social_monitor, "HttpClient", lambda _settings: object())
    monkeypatch.setattr(
        social_monitor,
        "discover_endpoints",
        lambda *_args, **_kwargs: {
            "feeds": [], "sitemaps": [], "listing_pages": [],
            "skip_homepage": True,
        },
    )
    monkeypatch.setattr(
        social_monitor,
        "collect_from_telegram_fallback",
        lambda *_args, **_kwargs: [fallback],
    )
    selected, error, metrics = social_monitor.collect_source_candidates(
        src,
        SETTINGS,
        {},
        dt.datetime(2026, 8, 7, tzinfo=UTC),
    )
    assert error is None
    assert [item.url for item in selected] == [fallback.url]
    assert metrics.telegram_candidates == 1
    assert metrics.selected_telegram == 1
    assert metrics.endpoint_total == 1


def test_architecture_core32a1_fallback_keeps_website_source_identity(
    monkeypatch,
):
    src = source()
    fallback_candidate = Candidate(
        source=social_monitor.replace(
            src,
            media_type="telegram",
            start_url="https://t.me/example_news",
            adapter="telegram_linked_site",
        ),
        url="https://example.com/2026/08/08/original",
        title="Оригинальная публикация",
        discovered_via="telegram:https://t.me/s/example_news",
        inline_text="Достаточно длинный текст официальной публикации.",
    )
    monkeypatch.setattr(
        social_monitor,
        "collect_from_telegram",
        lambda *_args, **_kwargs: [fallback_candidate],
    )
    items = social_monitor.collect_from_telegram_fallback(
        src,
        "https://t.me/example_news",
        object(),
        dt.datetime(2026, 8, 7, tzinfo=UTC),
        10,
    )
    assert len(items) == 1
    assert items[0].source is src
    assert items[0].url == "https://example.com/2026/08/08/original"
    assert items[0].inline_text


def test_architecture_core32a1_transport_failure_precedes_extraction_status():
    src = source()
    cmetrics = social_monitor.SourceCollectionMetrics(
        merged_candidates=4,
        selected_candidates=4,
        endpoint_total=1,
        endpoint_ok=1,
    )
    pmetrics = social_monitor.SourceProcessingMetrics(
        processed=4,
        fetch_failed=4,
        extraction_failed=4,
    )
    row = social_monitor.build_source_coverage(
        [src], [], [], [], [],
        {(src.country, src.name): cmetrics},
        {(src.country, src.name): pmetrics},
        SETTINGS,
    )[0]
    assert row["access_status"] == "transport_blocked"
    assert row["blind_zone_status"] == "transport_blocked"


def test_architecture_core32a1_access_admission_and_partial_loss_are_separate():
    src = source()
    cmetrics = social_monitor.SourceCollectionMetrics(
        merged_candidates=9,
        selected_candidates=4,
        clipped_candidates=5,
        endpoint_total=1,
        endpoint_ok=1,
    )
    pmetrics = social_monitor.SourceProcessingMetrics(
        processed=4,
        fetch_ok=4,
        extraction_full=2,
        extraction_failed=2,
    )
    row = social_monitor.build_source_coverage(
        [src], [], [], [], [],
        {(src.country, src.name): cmetrics},
        {(src.country, src.name): pmetrics},
        SETTINGS,
    )[0]
    assert row["access_status"] == "healthy_active"
    assert row["admission_status"] == "source_clipped"
    assert row["partial_extraction_loss"] == 2
    assert row["blind_zone_status"] == "source_clipped"


# --- Architecture Core 3.2a.2: content access recovery. ---


def test_architecture_core32a2_zerkalo_uses_only_official_feed_lead_fallback(
    monkeypatch,
):
    src = zerkalo_source()
    item = Candidate(
        source=src,
        url="https://news.zerkalo.io/life/133999.html",
        title="Жители пожаловались на перебои с водой",
        summary=(
            "Жильцы нескольких домов рассказали о длительных перебоях "
            "с водой и обращениях в коммунальную службу."
        ),
        published_at="2026-08-08T10:00:00+00:00",
        discovered_via="feed:https://news.zerkalo.io/rss/life.rss",
    )
    monkeypatch.setattr(social_monitor.HttpClient, "get", lambda *args, **kwargs: None)
    extracted = social_monitor.extract_article(item, SETTINGS)
    assert extracted.transport == "feed_metadata"
    assert extracted.extraction_strategy == "feed_summary"
    assert "коммунальную службу" in extracted.text

    ordinary = social_monitor.replace(
        item,
        source=source(),
        url="https://example.com/2026/08/08/story",
    )
    not_recovered = social_monitor.extract_article(ordinary, SETTINGS)
    assert not_recovered.transport_status == "failed"
    assert not not_recovered.text


def test_architecture_core32a2_vgr_empty_shell_uses_amp_fallback(monkeypatch):
    src = Source(
        enabled=True, country="Гродненская область", country_code="BY-HR",
        locality="Гродно", rank=4, priority="B", name="ВГР",
        media_type="website", domain="vgr.by", start_url="https://vgr.by/",
        language="ru", adapter="standard",
    )
    item = Candidate(
        source=src,
        url="https://vgr.by/2026/08/08/zhiteli-pozhalovalis-na-vodu",
        title="Жители пожаловались на воду",
    )

    class Response:
        def __init__(self, url, content):
            self.url = url
            self.content = content.encode("utf-8")

    calls = []

    def fake_get(_self, url, retries=1):
        calls.append((url, retries))
        if url.endswith("/amp/"):
            return Response(url, """
                <html><body><div class='amp-wp-article-content'>
                <p>Жители района жалуются на длительное отсутствие воды.</p>
                <p>Коммунальная служба сообщила, что ремонт уже начался.</p>
                </div></body></html>
            """)
        return Response(url, "<html><body><div id='app'></div></body></html>")

    monkeypatch.setattr(social_monitor.HttpClient, "get", fake_get)
    extracted = social_monitor.extract_article(item, SETTINGS)
    assert extracted.transport == "amp"
    assert extracted.extraction_strategy == "source_specific"
    assert "ремонт уже начался" in extracted.text
    assert calls[1][0].endswith("/amp/")


def test_architecture_core32a2_vgr_full_primary_page_does_not_call_amp(
    monkeypatch,
):
    src = Source(
        enabled=True, country="Гродненская область", country_code="BY-HR",
        locality="Гродно", rank=4, priority="B", name="ВГР",
        media_type="website", domain="vgr.by", start_url="https://vgr.by/",
        language="ru", adapter="standard",
    )
    item = Candidate(
        source=src,
        url="https://vgr.by/2026/08/08/zhiteli-pozhalovalis-na-vodu",
        title="Жители пожаловались на воду",
    )

    class Response:
        url = item.url
        content = """
          <html><body><div class='edgtf-post-text-main'>
          <p>Жители района жалуются на длительное отсутствие воды.</p>
          <p>Коммунальная служба сообщила, что ремонт уже начался.</p>
          </div></body></html>
        """.encode("utf-8")

    calls = []
    monkeypatch.setattr(
        social_monitor.HttpClient,
        "get",
        lambda _self, url, retries=1: calls.append(url) or Response(),
    )
    extracted = social_monitor.extract_article(item, SETTINGS)
    assert extracted.transport == "requests"
    assert len(calls) == 1
    assert "ремонт уже начался" in extracted.text


def test_architecture_core32a2_hrodna_exact_container_excludes_sidebar():
    src = social_monitor.replace(
        source("Гродненская область", "Гродно"),
        name="Hrodna.life",
        domain="hrodna.life",
    )
    item = Candidate(source=src, url="https://hrodna.life/2026/08/08/problem")
    extracted = social_monitor.extract_article_from_html(item, """
      <html><body><main>
        <div class='post-content description cf entry-content content-normal'>
          <p>Жыхары раёна скардзяцца на працяглыя перабоі з вадой.</p>
          <p>Камунальныя службы паведамілі, што рамонт ужо пачаўся.</p>
        </div>
        <aside class='sidebar'><p>Апошнія запісы і папулярныя навіны.</p></aside>
      </main></body></html>
    """)
    assert "рамонт ужо пачаўся" in extracted.text
    assert "Апошнія запісы" not in extracted.text


def test_architecture_core32a2_masheka_full_text_supports_div_only_archive():
    src = social_monitor.replace(
        source("Могилёвская область", "Могилёв"),
        name="MASHEKA",
        domain="masheka.by",
        adapter="robust_article",
    )
    item = Candidate(source=src, url="https://masheka.by/history/1234-story.html")
    extracted = social_monitor.extract_article_from_html(item, """
      <html><body>
        <div class='full-text'>
          <div>Жители Могилёва рассказали о многолетней проблеме с дорогой.</div>
          <div>После коллективных обращений специалисты начали ремонт участка.</div>
          <div>Редакция получила официальный ответ городской организации.</div>
        </div>
        <div class='latest-news'><p>Совсем другая популярная публикация.</p></div>
      </body></html>
    """)
    assert "специалисты начали ремонт" in extracted.text
    assert "популярная публикация" not in extracted.text


def test_architecture_core32a2_bobr_advertisements_are_structurally_filtered():
    src = social_monitor.replace(
        source("Могилёвская область", "Бобруйск"),
        name="Вечерний Бобруйск",
        domain="bobr.by",
    )
    advert = "https://bobr.by/advertisement/sell/animal/sell-dog/4017191"
    news = "https://bobr.by/news/ispolkom/198363"
    assert social_monitor.classify_source_url(
        advert, src.domain, social_monitor.source_adapter_profile(src)
    ) == "service"
    assert not social_monitor.is_source_article_url(advert, src)
    assert social_monitor.is_source_article_url(news, src)


def test_architecture_core32a21_bobr_url_guard_accepts_only_newsroom_ids():
    src = social_monitor.replace(
        source("Могилёвская область", "Бобруйск"),
        name="Вечерний Бобруйск",
        domain="bobr.by",
    )
    accepted = (
        "https://bobr.by/news/city/198366",
        "https://bobr.by/news/ispolkom/198363/",
        "https://bobr.by/NEWS/gai/198371?utm_source=homepage",
    )
    rejected = (
        "https://bobr.by/city/Online",
        "https://bobr.by/city/help?curPos=3",
        "https://bobr.by/communication/lastcomments",
        "https://bobr.by/job/vacancy/3871523",
        "https://bobr.by/poster/seminars/18407",
        "https://bobr.by/photofact/5810",
        "https://bobr.by/advertisement/sell/animal/sell-dog/4017191",
        "https://bobr.by/news/city/not-a-numeric-id",
        "https://bobr.by/news/198366",
    )

    assert all(social_monitor.is_source_article_url(url, src) for url in accepted)
    assert all(not social_monitor.is_source_article_url(url, src) for url in rejected)


def test_architecture_core32a21_allowlist_guard_is_bobr_only():
    src = social_monitor.replace(
        source("Минская область", "Минск"),
        name="Обычный источник",
        domain="example.com",
    )

    assert social_monitor.is_source_article_url(
        "https://example.com/city/social-story-198366", src
    )


# --- Architecture Core 3.2a.2.2: Nasha Niva comments guard. ---


def test_architecture_core32a22_nasha_niva_comments_guard_covers_all_locales():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Наша Ніва",
        domain="nashaniva.com",
    )
    already_admitted_localized_articles = (
        "https://nashaniva.com/ru/401729",
        "https://nashaniva.com/be_latn/401729/",
    )
    rejected = (
        "https://nashaniva.com/401638/comments",
        "https://nashaniva.com/401697/comments/",
        "https://nashaniva.com/RU/401716/COMMENTS?from=homepage#latest",
        "https://nashaniva.com/be_latn/401729/comments",
        "https://nashaniva.com/ru/401729/comments/page/2",
    )

    assert all(
        social_monitor.is_source_article_url(url, src)
        for url in already_admitted_localized_articles
    )
    # Keep the pre-update admission boundary: the single numeric Belarusian
    # route remains unknown until its extraction/duplication behavior is
    # deliberately diagnosed.  This hotfix only removes comments pages.
    assert social_monitor.classify_source_url(
        "https://nashaniva.com/401729",
        src.domain,
        social_monitor.source_adapter_profile(src),
    ) == "unknown"
    assert all(not social_monitor.is_source_article_url(url, src) for url in rejected)
    assert all(
        social_monitor.classify_source_url(
            url, src.domain, social_monitor.source_adapter_profile(src)
        ) == "service"
        for url in rejected
    )


def test_architecture_core32a22_nasha_niva_report29_comments_are_all_blocked():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Наша Ніва",
        domain="nashaniva.com",
    )
    report29_ids = (
        401638, 401697, 401716, 401717, 401719, 401721, 401725, 401726,
        401728, 401729, 401735, 401736, 401737, 401743, 401744, 401749,
        401753, 401754, 401755, 401757, 401762, 401769, 401772,
    )

    assert len(report29_ids) == 23
    assert all(
        not social_monitor.is_source_article_url(
            f"https://nashaniva.com/{article_id}/comments", src
        )
        for article_id in report29_ids
    )


def test_architecture_core32a22_homepage_pair_admits_neither_root_article_nor_comments(
    monkeypatch,
):
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Наша Ніва",
        domain="nashaniva.com",
        start_url="https://nashaniva.com/",
    )
    html = """
    <html><body><main><h2>
      <a href="/401729">Пять охранников прикрывали Лукашенко во время визита</a>
      <a href="/401729/comments">23 комментария</a>
    </h2></main></body></html>
    """.encode("utf-8")

    class Response:
        content = html
        status_code = 200
        url = "https://nashaniva.com/"

    client = social_monitor.HttpClient(SETTINGS)
    monkeypatch.setattr(client, "get", lambda *args, **kwargs: Response())

    assert social_monitor.collect_from_homepage(src, client, 10) == []


def test_architecture_core32a22_comments_guard_keeps_telegram_inline_fallback(
    monkeypatch,
):
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Наша Ніва",
        domain="nashaniva.com",
        telegram_url="https://t.me/nashaniva",
    )
    html = """
    <html><body>
      <div class="tgme_widget_message_wrap">
        <div class="tgme_widget_message" data-post="nashaniva/119967"></div>
        <div class="tgme_widget_message_text">
          Жители Минска жалуются на отсутствие уличного освещения.
          <a href="https://nashaniva.com/401729/comments">23 комментария</a>
        </div>
        <time datetime="2026-08-08T08:00:00+00:00"></time>
      </div>
    </body></html>
    """.encode("utf-8")

    class Response:
        content = html
        status_code = 200
        url = "https://t.me/s/nashaniva"

    client = social_monitor.HttpClient(SETTINGS)
    monkeypatch.setattr(client, "get", lambda *args, **kwargs: Response())
    items = social_monitor.collect_from_telegram_fallback(
        src,
        "https://t.me/nashaniva",
        client,
        dt.datetime(2026, 8, 7, tzinfo=UTC),
        10,
    )

    assert len(items) == 1
    assert items[0].source is src
    assert items[0].url == "https://t.me/nashaniva/119967"
    assert "освещения" in items[0].inline_text


def test_architecture_core32a22_comments_guard_is_nasha_niva_only():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Обычный источник",
        domain="example.com",
    )

    assert social_monitor.is_source_article_url(
        "https://example.com/401729/comments", src
    )


# --- Architecture Core 3.2b: Soft Admission. ---


def test_architecture_core32b_admission_tiers_are_structural_and_date_aware():
    cutoff = dt.datetime(2026, 8, 7, tzinfo=UTC)
    fresh = candidate(
        title="Свежая публикация RSS",
        url="https://example.com/news/fresh-rss-story",
        published_at="2026-08-08T08:00:00+00:00",
    )
    current = candidate(
        title="Материал на главной странице",
        url="https://example.com/news/current-homepage-story",
        published_at="",
        discovered_via="homepage",
    )
    undated_sitemap = candidate(
        title="Архивная запись карты сайта",
        url="https://example.com/news/undated-sitemap-story",
        published_at="",
        discovered_via="sitemap:https://example.com/sitemap.xml",
    )
    stale_url = candidate(
        title="Карта сайта ошибочно получила свежий lastmod",
        url="https://example.com/2020/04/03/old-story",
        published_at="2026-08-08T08:00:00+00:00",
        discovered_via="sitemap:https://example.com/sitemap.xml",
    )
    service_like = candidate(
        title="Комментарии",
        url="https://example.com/news/123/comments",
        published_at="2026-08-08T08:00:00+00:00",
        discovered_via="homepage",
    )

    assert social_monitor.candidate_admission_decision(fresh, cutoff).status == "fresh"
    assert social_monitor.candidate_admission_decision(current, cutoff).status == "current"
    assert social_monitor.candidate_admission_decision(
        undated_sitemap, cutoff
    ).reason == "undated_sitemap_only"
    assert social_monitor.candidate_admission_decision(
        stale_url, cutoff
    ).reason == "known_stale_url"
    assert social_monitor.candidate_admission_decision(
        service_like, cutoff
    ).reason == "service_like_url"


def test_architecture_core32b_report30_nasha_niva_keeps_telegram_plus_soft_tail():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Наша Ніва",
        domain="nashaniva.com",
        telegram_url="https://t.me/nashaniva",
    )
    cutoff = dt.datetime(2026, 8, 7, tzinfo=UTC)
    telegram = [
        Candidate(
            source=src,
            url=f"https://t.me/nashaniva/{120000 + index}",
            title=f"Свежая Telegram-публикация номер {index}",
            published_at=f"2026-08-08T{index % 18:02d}:00:00+00:00",
            discovered_via="telegram:https://t.me/s/nashaniva",
            inline_text=(
                f"Свежая Telegram-публикация номер {index}. "
                "Жители подробно описывают городскую проблему."
            ),
            title_generated=True,
        )
        for index in range(18)
    ]
    sitemap = [
        Candidate(
            source=src,
            url=f"https://nashaniva.com/ru/{400000 + index}",
            title=f"Недатированный sitemap-материал номер {index}",
            discovered_via="sitemap:https://nashaniva.com/sitemap.xml",
        )
        for index in range(100)
    ]

    selected = social_monitor.select_balanced_source_candidates(
        [*telegram, *sitemap],
        60,
        sitemap_reserve=5,
        cutoff=cutoff,
        settings=SETTINGS,
    )
    statuses = [
        social_monitor.candidate_admission_decision(item, cutoff).status
        for item in selected
    ]

    assert len(selected) == 23
    assert statuses.count("fresh") == 18
    assert statuses.count("soft") == 5
    assert statuses[:18] == ["fresh"] * 18
    assert social_monitor.soft_admission_tail_budget(
        [*telegram, *sitemap], 60, 5, cutoff
    ) == 5


def test_architecture_core32b_soft_tail_never_displaces_a_full_fresh_budget():
    src = source()
    fresh = [
        Candidate(
            source=src,
            url=f"https://example.com/news/{index}",
            title=f"Свежий материал номер {index}",
            published_at="2026-08-08T08:00:00+00:00",
            discovered_via=(
                "feed:https://example.com/rss" if index < 25 else "homepage"
            ),
        )
        for index in range(40)
    ]
    soft = [
        Candidate(
            source=src,
            url=f"https://example.com/archive/{index}",
            title=f"Недатированный архивный материал {index}",
            discovered_via="sitemap:https://example.com/sitemap.xml",
        )
        for index in range(20)
    ]

    selected = social_monitor.select_balanced_source_candidates(
        [*fresh, *soft],
        35,
        cutoff=dt.datetime(2026, 8, 7, tzinfo=UTC),
        settings=SETTINGS,
    )

    assert len(selected) == 35
    assert all(
        social_monitor.candidate_admission_decision(
            item, dt.datetime(2026, 8, 7, tzinfo=UTC)
        ).status != "soft"
        for item in selected
    )


def test_architecture_core32b_sitemap_only_source_keeps_existing_capacity():
    src = source()
    sitemap = [
        Candidate(
            source=src,
            url=f"https://example.com/archive/{index}",
            title=f"Материал sitemap-only источника {index}",
            discovered_via="sitemap:https://example.com/sitemap.xml",
        )
        for index in range(40)
    ]

    selected = social_monitor.select_balanced_source_candidates(
        sitemap,
        35,
        cutoff=dt.datetime(2026, 8, 7, tzinfo=UTC),
        settings=SETTINGS,
    )

    assert len(selected) == 35
    assert social_monitor.soft_admission_tail_budget(
        sitemap, 35, 5, dt.datetime(2026, 8, 7, tzinfo=UTC)
    ) == 35


def test_architecture_core32b_deduplicates_one_clear_telegram_site_pair():
    src = source()
    site = Candidate(
        source=src,
        url="https://example.com/news/dangerous-road-near-school",
        title=(
            "Почему опасная дорога возле школы в Минске остается без освещения: "
            "рассказали жители"
        ),
        published_at="2026-08-08T09:00:00+00:00",
        discovered_via="feed:https://example.com/rss",
    )
    telegram = Candidate(
        source=src,
        url="https://t.me/example_news/1001",
        title=(
            "Жители Минска рассказали, почему опасная дорога возле школы "
            "остается без освещения"
        ),
        published_at="2026-08-08T10:00:00+00:00",
        discovered_via="telegram:https://t.me/s/example_news",
        inline_text="Жители подробно рассказали о дороге и отсутствии освещения.",
        title_generated=True,
    )

    merged, telegram_site_duplicates = social_monitor.deduplicate_candidates_with_stats(
        [site, telegram]
    )

    assert telegram_site_duplicates == 1
    assert len(merged) == 1
    assert merged[0].url == site.url
    assert merged[0].inline_text == telegram.inline_text
    assert social_monitor.candidate_discovery_channels(merged[0]) == {
        "feed", "telegram"
    }


def test_architecture_core32b_counts_exact_linked_telegram_site_duplicate():
    src = source()
    canonical_url = "https://example.com/news/exact-linked-story"
    site = Candidate(
        source=src,
        url=canonical_url,
        title="Жители рассказали о проблеме с дорогой возле школы",
        published_at="2026-08-08T09:00:00+00:00",
        discovered_via="feed:https://example.com/rss",
    )
    telegram = Candidate(
        source=src,
        url=canonical_url + "?utm_source=telegram",
        title="Жители рассказали о проблеме с дорогой возле школы",
        published_at="2026-08-08T09:05:00+00:00",
        discovered_via="telegram:https://t.me/s/example_news",
        inline_text="Встроенный текст официальной Telegram-публикации.",
        title_generated=True,
    )

    merged, telegram_site_duplicates = social_monitor.deduplicate_candidates_with_stats(
        [site, telegram]
    )

    assert telegram_site_duplicates == 1
    assert len(merged) == 1
    assert merged[0].url == canonical_url
    assert merged[0].inline_text == telegram.inline_text


def test_architecture_core32b_ambiguous_telegram_match_is_preserved():
    src = source()
    shared_title = (
        "Жители Минска рассказали почему опасная дорога возле школы "
        "остается без освещения"
    )
    sites = [
        Candidate(
            source=src,
            url=f"https://example.com/news/story-{index}",
            title=shared_title,
            published_at="2026-08-08T09:00:00+00:00",
            discovered_via="homepage",
        )
        for index in range(2)
    ]
    telegram = Candidate(
        source=src,
        url="https://t.me/example_news/1001",
        title=shared_title,
        published_at="2026-08-08T10:00:00+00:00",
        discovered_via="telegram:https://t.me/s/example_news",
        inline_text="Полный встроенный текст Telegram-публикации.",
        title_generated=True,
    )

    merged, telegram_site_duplicates = social_monitor.deduplicate_candidates_with_stats(
        [*sites, telegram]
    )

    assert telegram_site_duplicates == 0
    assert len(merged) == 3


def test_architecture_core32b_soft_only_clipping_is_not_reported_as_blind_zone():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Наша Ніва",
        domain="nashaniva.com",
    )
    metrics = social_monitor.SourceCollectionMetrics(
        sitemap_candidates=100,
        telegram_candidates=18,
        merged_candidates=118,
        selected_candidates=23,
        selected_sitemap=5,
        selected_telegram=18,
        selected_fresh=18,
        selected_soft=5,
        soft_tail_budget=5,
        clipped_candidates=95,
        clipped_soft=95,
        clipped_undated=95,
        endpoint_total=2,
        endpoint_ok=2,
    )

    rows = social_monitor.build_source_coverage(
        [src],
        [],
        [],
        [],
        [],
        {(src.country, src.name): metrics},
        settings=SETTINGS,
    )

    assert rows[0]["admission_status"] == "soft_admission_limited"
    assert rows[0]["access_status"] == "healthy_active"
    assert rows[0]["blind_zone_status"] == "healthy_active"


# --- Architecture Core 3.2b.1: Admission Stabilization. ---


def test_architecture_core32b1_onliner_marketplace_routes_are_soft_not_blocked():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Onlíner",
        domain="onliner.by",
    )
    urls = (
        "https://ab.onliner.by/chevrolet/malibu/5066274",
        "https://catalog.onliner.by/cat/building",
        "https://catalog.onliner.by/promo/kupit-pk",
        "https://auto.onliner.by/go/1259982?hash=example",
        "https://sport.onliner.by/GO/29013?hash=example",
    )

    for url in urls:
        item = Candidate(
            source=src,
            url=url,
            title="Ссылка с главной страницы Onlíner",
            discovered_via="homepage",
        )
        decision = social_monitor.candidate_admission_decision(item)
        assert decision.status == "soft"
        assert decision.reason == "service_like_url"
        assert decision.service_like is True
        assert social_monitor.classify_source_url(url, src.domain) == "article"


def test_architecture_core32b1_onliner_editorial_routes_keep_their_tiers():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Onlíner",
        domain="onliner.by",
    )
    cutoff = dt.datetime(2026, 8, 7, tzinfo=UTC)
    fresh = Candidate(
        source=src,
        url="https://auto.onliner.by/2026/08/08/remont-mosta-3",
        title="Редакционная публикация Onlíner",
        published_at="2026-08-08T08:00:00+00:00",
        discovered_via="feed:https://www.onliner.by/feed",
    )
    homepage_dated = Candidate(
        source=src,
        url="https://realt.onliner.by/2026/08/08/v-sekretnom-dome",
        title="Редакционная публикация с главной страницы",
        discovered_via="homepage",
    )
    current = Candidate(
        source=src,
        url="https://people.onliner.by/news/editorial-story",
        title="Недатированная редакционная публикация",
        discovered_via="homepage",
    )

    assert social_monitor.candidate_admission_decision(fresh, cutoff).status == "fresh"
    assert social_monitor.candidate_admission_decision(
        homepage_dated, cutoff
    ).status == "fresh"
    assert social_monitor.candidate_admission_decision(current, cutoff).status == "current"


def test_architecture_core32b1_onliner_soft_routes_do_not_displace_editorial_budget():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Onlíner",
        domain="onliner.by",
    )
    cutoff = dt.datetime(2026, 8, 7, tzinfo=UTC)
    fresh = [
        Candidate(
            source=src,
            url=f"https://people.onliner.by/2026/08/08/editorial-{index}",
            title=f"Свежая редакционная публикация Onlíner номер {index}",
            published_at="2026-08-08T08:00:00+00:00",
            discovered_via="feed:https://www.onliner.by/feed",
        )
        for index in range(30)
    ]
    current = [
        Candidate(
            source=src,
            url=f"https://people.onliner.by/news/editorial-{index}",
            title=f"Текущая редакционная публикация Onlíner номер {index}",
            discovered_via="homepage",
        )
        for index in range(12)
    ]
    stale = [
        Candidate(
            source=src,
            url=f"https://people.onliner.by/2026/08/06/archive-{index}",
            title=f"Старый материал Onlíner номер {index}",
            published_at="2026-08-06T08:00:00+00:00",
            discovered_via="homepage",
        )
        for index in range(39)
    ]
    marketplace = [
        Candidate(
            source=src,
            url=f"https://ab.onliner.by/kia/model/{5000000 + index}",
            title=f"Автомобильное объявление номер {index}",
            discovered_via="homepage",
        )
        for index in range(4)
    ] + [
        Candidate(
            source=src,
            url=f"https://catalog.onliner.by/product/example-{index}",
            title=f"Карточка каталога номер {index}",
            discovered_via="homepage",
        )
        for index in range(21)
    ] + [
        Candidate(
            source=src,
            url=f"https://auto.onliner.by/go/{1259982 + index}?hash=example",
            title=f"Рекламный переход номер {index}",
            discovered_via="homepage",
        )
        for index in range(2)
    ]

    selected = social_monitor.select_balanced_source_candidates(
        [*fresh, *current, *stale, *marketplace],
        65,
        cutoff=cutoff,
        settings=SETTINGS,
    )
    statuses = [
        social_monitor.candidate_admission_decision(item, cutoff).status
        for item in selected
    ]

    assert len(selected) == 47
    assert statuses.count("fresh") == 30
    assert statuses.count("current") == 12
    assert statuses.count("soft") == 5
    assert all(
        "ab.onliner.by" not in item.url
        and "catalog.onliner.by" not in item.url
        and "/go/" not in item.url.casefold()
        for item in selected
    )


def test_architecture_core32b1_prefilter_runs_only_for_soft_sort_keys(monkeypatch):
    cutoff = dt.datetime(2026, 8, 7, tzinfo=UTC)
    items = [
        candidate(
            title="Свежий материал RSS",
            url="https://example.com/news/fresh",
            published_at="2026-08-08T08:00:00+00:00",
            discovered_via="feed:https://example.com/rss",
        ),
        candidate(
            title="Текущий материал с главной",
            url="https://example.com/news/current",
            discovered_via="homepage",
        ),
        candidate(
            title="Недатированный материал sitemap",
            url="https://example.com/news/soft",
            discovered_via="sitemap:https://example.com/sitemap.xml",
        ),
    ]
    calls = []

    def counted_prefilter(item, settings):
        calls.append(item.url)
        return social_monitor.MetadataPrefilterDecision(status="needs_text")

    monkeypatch.setattr(social_monitor, "metadata_prefilter", counted_prefilter)
    for item in items:
        social_monitor.candidate_admission_sort_key(item, cutoff, SETTINGS)

    assert calls == ["https://example.com/news/soft"]


# --- Architecture Core 3.2b.1.1: Onlíner Route Completion. ---


def test_architecture_core32b11_onliner_redirect_and_moto_hosts_are_soft():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Onlíner",
        domain="onliner.by",
    )
    urls = (
        "https://go.onliner.by/tiles.acp/redirect/eyJ1cmwiOiJhdXRvIn0%3D",
        "https://go.onliner.by/tiles.acp/redirect/eyJ1cmwiOiJtb25leSJ9",
        "https://go.onliner.by/tiles.acp/redirect/eyJ1cmwiOiJzcG9ydCJ9",
        "https://go.onliner.by/tiles.acp/redirect/eyJ1cmwiOiJwZW9wbGUifQ%3D%3D",
        "https://go.onliner.by/tiles.acp/redirect/eyJ1cmwiOiJyZWFsdCJ9",
        "https://go.onliner.by/tiles.acp/redirect/eyJ1cmwiOiJ0ZWNoLTEifQ%3D%3D",
        "https://go.onliner.by/tiles.acp/redirect/eyJ1cmwiOiJ0ZWNoLTIifQ%3D%3D",
        "https://mb.onliner.by/moto/216152",
    )

    decisions = []
    for url in urls:
        item = Candidate(
            source=src,
            url=url,
            title="Ссылка с главной страницы Onlíner",
            discovered_via="homepage",
        )
        decisions.append(social_monitor.candidate_admission_decision(item))
        assert social_monitor.classify_source_url(url, src.domain) == "article"

    assert len(decisions) == 8
    assert all(item.status == "soft" for item in decisions)
    assert all(item.reason == "service_like_url" for item in decisions)
    assert all(item.service_like is True for item in decisions)


def test_architecture_core32b11_onliner_editorial_hosts_remain_current():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Onlíner",
        domain="onliner.by",
    )
    urls = (
        "https://auto.onliner.by/news/editorial-story",
        "https://money.onliner.by/news/editorial-story",
        "https://people.onliner.by/news/editorial-story",
        "https://realt.onliner.by/news/editorial-story",
        "https://sport.onliner.by/news/editorial-story",
        "https://tech.onliner.by/news/editorial-story",
    )

    decisions = [
        social_monitor.candidate_admission_decision(
            Candidate(
                source=src,
                url=url,
                title="Недатированная редакционная публикация Onlíner",
                discovered_via="homepage",
            )
        )
        for url in urls
    ]

    assert all(item.status == "current" for item in decisions)
    assert all(item.service_like is False for item in decisions)


def test_architecture_core32b11_onliner_completed_routes_do_not_use_trusted_budget():
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Onlíner",
        domain="onliner.by",
    )
    cutoff = dt.datetime(2026, 8, 7, tzinfo=UTC)
    fresh = [
        Candidate(
            source=src,
            url=f"https://people.onliner.by/2026/08/08/editorial-{index}",
            title=f"Свежая редакционная публикация Onlíner номер {index}",
            published_at="2026-08-08T08:00:00+00:00",
            discovered_via="feed:https://www.onliner.by/feed",
        )
        for index in range(30)
    ]
    editorial_soft = [
        Candidate(
            source=src,
            url=f"https://people.onliner.by/2026/08/06/archive-{index}",
            title=f"Старый редакционный материал Onlíner номер {index}",
            published_at="2026-08-06T08:00:00+00:00",
            discovered_via="homepage",
        )
        for index in range(10)
    ]
    completed_routes = [
        Candidate(
            source=src,
            url=f"https://go.onliner.by/tiles.acp/redirect/token-{index}",
            title=f"Рекламный переход Onlíner номер {index}",
            discovered_via="homepage",
        )
        for index in range(7)
    ] + [
        Candidate(
            source=src,
            url="https://mb.onliner.by/moto/216152",
            title="Объявление Мотобарахолки",
            discovered_via="homepage",
        )
    ]

    selected = social_monitor.select_balanced_source_candidates(
        [*fresh, *editorial_soft, *completed_routes],
        65,
        cutoff=cutoff,
        settings=SETTINGS,
    )
    statuses = [
        social_monitor.candidate_admission_decision(item, cutoff).status
        for item in selected
    ]

    assert len(selected) == 35
    assert statuses.count("fresh") == 30
    assert statuses.count("current") == 0
    assert statuses.count("soft") == 5
    assert all("go.onliner.by" not in item.url for item in selected)
    assert all("mb.onliner.by" not in item.url for item in selected)


def test_architecture_core32b11_route_rule_is_limited_to_onliner_source():
    foreign = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name="Другой источник",
        domain="example.com",
    )
    item = Candidate(
        source=foreign,
        url="https://go.onliner.by/tiles.acp/redirect/token",
        title="Ссылка, обнаруженная у другого источника",
        discovered_via="homepage",
    )

    decision = social_monitor.candidate_admission_decision(item)

    assert decision.status == "current"
    assert decision.service_like is False


# --- Update 40: three-day relevance precision, resonance, and delivery. ---

def test_update40_baseline_editorial_noise_is_excluded_by_genre():
    cases = [
        (
            '"Результат говорит за себя": "Арсенал" крупно проигрывает за полтайма',
            "Игроки футбольного клуба провели матч. В составе отсутствовали несколько футболистов.",
        ),
        (
            "Беларус собирает 50 000 монет для прикола",
            "Автор говорит о дефиците мелочи и показывает свою коллекцию монет.",
        ),
        (
            "Под Минском продают два готовых дома дешевле многих квартир",
            "Дома продают с коммуникациями, электричеством и подъездной дорогой.",
        ),
        (
            "Железо у вегетарианцев: риски, о которых стоит знать",
            "Врач объясняет дефицит железа и советует продукты для гемоглобина.",
        ),
        (
            "Где купить оригинальные мужские кеды Brunello Cucinelli в Москве?",
            "Магазин предлагает ассортимент, цены и гарантию качества.",
        ),
        (
            "Кредит под 1%: калькулятор кредита и сколько придется платить",
            "Считаем выплаты по кредиту на квартиру в течение 18 лет.",
        ),
        (
            "Большой парад планет — главное астрономическое событие года",
            "Наблюдать лучше с берега, где отсутствует уличное освещение.",
        ),
        (
            "История Могилева: май 1941 года",
            "В городе были дефицит продуктов и очереди в магазинах.",
        ),
        (
            "Дефицит внешней торговли промышленной продукцией вырос почти в пять раз",
            "Импорт вырос быстрее экспорта, а зарплаты — быстрее производительности труда.",
        ),
        (
            "В Минске завершают строительство 12-километровой велодорожки",
            "Через парки пройдут четыре очереди строительства с асфальтовым покрытием.",
        ),
        (
            "У белорусов могут возникнуть проблемы с банковскими картами",
            "Банк предупредил о возможных перебоях во время плановых технических работ.",
        ),
        (
            "Шершни атаковали: спасатели ликвидировали почти 60 гнезд",
            "Спасатели напоминают, что снимать гнезда самостоятельно опасно.",
        ),
        (
            "В центре Гродно продают ресторан с высоким рейтингом",
            "Готовый бизнес и бар можно купить вместе с оборудованием и парковкой.",
        ),
        (
            "Меню на экране в общепите — как устроены цифровые меню-борды",
            "Экраны показывают цены, ассортимент и помогают обслуживать очередь.",
        ),
        (
            "Памёр Аляксандр Мілінкевіч",
            "Яму былі патрэбныя лекі, урач і рэабілітацыя ў бальніцы.",
        ),
        (
            "От допинга до гендерного тупика — главные скандалы мирового спорта",
            "Спортсменки жаловались на проверки во время чемпионата.",
        ),
        (
            "Комбайнеры пришли на помощь хлеборобам",
            "Работники откликнулись и помогут закончить уборку раньше.",
        ),
        (
            "Стало известно, как белорусы могут сэкономить на коммуналке",
            "Эксперт дал советы по перерасчету и оплате коммунальных услуг.",
        ),
        (
            "В Беларуси задержали российского бизнесмена из-за некачественной тушенки на СВО",
            "Предпринимателя искали за поставки тушенки для армии России.",
        ),
        (
            "Школьник попал в реанимацию после курения вейпа",
            "Врач рассказал о вреде курения и единичном отравлении подростка.",
        ),
        (
            "Соседская собака лает ночами: белорусам рассказали, куда жаловаться",
            "Жительница спросила, куда обратиться из-за частного спора с соседом.",
        ),
        (
            "Молодые специалисты смогут сообщать о проблемах онлайн",
            "Новый сервис объясняет, куда обращаться при возможной задержке зарплаты.",
        ),
    ]
    for title, text in cases:
        result = decision(title, text, source("Беларусь", "Беларусь"))
        assert not result.relevant, (title, result)


def test_update40_real_public_problems_from_same_sections_remain_included():
    cases = [
        (
            "Вышел оплатить парковку: в аэропорту не оградили горячий битум",
            "Пассажир направил претензию: предупреждающие знаки и ограждения отсутствовали, проход был опасен.",
        ),
        (
            "Талоны есть только на осень: пациенты жалуются на стоматологию",
            "Жители Смолевичей не могут записаться к врачу, электронная запись работает нестабильно.",
        ),
        (
            "В магазине нашли 50 кг просроченных продуктов",
            "Проверка КГК выявила нарушения качества и закрыла магазин до их устранения.",
        ),
        (
            "Ошибки в документах могли лишить работников льготной пенсии",
            "Работник обратился за помощью, проверка обнаружила неточности и восстановила его право на пенсию.",
        ),
        (
            "За четыре класса — третий учитель: родители жалуются на текучку кадров",
            "Школа не может обеспечить постоянного классного руководителя, похожие жалобы оставили другие родители.",
        ),
    ]
    for title, text in cases:
        result = decision(title, text, source("Беларусь", "Беларусь"))
        assert result.relevant, (title, result.reason)


def test_update40r1_report37_editorial_noise_is_excluded():
    cases = [
        (
            "В Беларуси расширят просеки возле ЛЭП — аварийных отключений станет меньше",
            "Просеки будут расширять, чтобы снизить риск падения деревьев и аварий.",
        ),
        (
            "«Я и терапевт души, и нумеролог, работаю парикмахером». Жизнь глазами бобруйчан",
            "Героиня рассказала о поездке в горный парк на месте заброшенного карьера.",
        ),
        (
            "«Спасли от инфаркта». 80-летнему архиепископу сделали новую операцию на сердце",
            "Врачи выявили сердечную недостаточность, провели операцию, пациент выписан домой.",
        ),
        (
            "О первых рабочих буднях в поликлинике Гомеля рассказал выпускник медколледжа",
            (
                "Выпускник рассказал о практике. Во время операции обнаружилось, "
                "что у больного есть проблемы с селезенкой. Люди иногда жалуются "
                "на очереди, но каждый имеет право на бесплатную медицину."
            ),
        ),
        (
            "Жителей Заводского района Минска предупредили о повышенном уровне шума 15 и 16 августа 2026 года",
            "Шум ожидается во время обязательных плановых работ на новом паропроводе.",
        ),
        (
            "Как использовать семейный капитал для оплаты обучения в вузе или колледже, 12.08.2026",
            "Нужно подготовить документы и подать заявление в службу Одно окно.",
        ),
        (
            "Однажды в Могилеве. Очки под Леннона и немного мужских слез. Видео",
            "В прошлом хорошие легкие оправы были дефицитом.",
        ),
        (
            "На МКАД-2 женщина решила перебежать дорогу — ее сбил пенсионер",
            "Водитель совершил наезд; ГАИ напоминает правила движения пешеходов.",
        ),
        (
            "В машине пахнет сыростью: виноват кондиционер или где-то стоит вода?",
            "Автовладельцам советуют проверить фильтр и поискать протечку после дождя.",
        ),
        (
            "В Минтруда напомнили, как многодетные семьи могут оплатить обучение в вузе или колледже с помощью семейного капитала",
            "Оплатить можно обучение членов семьи; затем нужно подать заявление.",
        ),
        (
            "Из былого: на пароходе по Днепру из Могилева в Смоленск и Киев",
            "Первый пароход прибыл в Могилев в 1859 году.",
        ),
        (
            "ВІДЭАНАВІНЫ: Драма ў мінскай бальніцы скончылася вялікім скандалам / Лукашэнку просяць разабрацца",
            "Улады спыняюць грузавікі. Даносчыца просіць дапамогі, адказу няма.",
        ),
    ]
    for title, text in cases:
        result = decision(title, text, source("Беларусь", "Беларусь"))
        assert not result.relevant, (title, result)


def test_update40r1_complaint_about_denied_family_capital_remains_included():
    result = decision(
        "Многодетной семье отказывают в покупке дома за материнский капитал",
        (
            "Ольга жалуется, что семье не разрешают потратить материнский капитал. "
            "Сотрудник МЧС проверил дом, но райисполком отказал в покупке."
        ),
        source("Минская область", "Старые Дороги"),
    )
    assert result.relevant, result.reason


def test_update40_tracking_tg_parameter_is_a_technical_duplicate():
    plain = "https://news.zerkalo.io/economics/134072.html"
    assert canonicalize_url(plain + "?tg=6") == plain


def test_update40_priority_tier_wins_primary_source_before_score():
    priority_a = make_result("https://priority.example/story", "Брест")
    priority_a.source_name = "Приоритетный источник"
    priority_a.priority = "A"
    priority_a.score = 5
    priority_b = make_result("https://score.example/story", "Брест")
    priority_b.source_name = "Источник с высоким баллом"
    priority_b.priority = "B"
    priority_b.score = 20
    kept = social_monitor.deduplicate_results([priority_b, priority_a])
    assert len(kept) == 1
    assert kept[0].source_name == "Приоритетный источник"
    assert kept[0].related_coverage == ((
        "Источник с высоким баллом", "https://score.example/story"
    ),)


def test_update40_same_source_rewrite_is_silently_consolidated():
    first = make_result("https://example.com/first", "Брест")
    second = make_result("https://example.com/second", "Брест")
    first.source_name = second.source_name = "Одно СМИ"
    kept = social_monitor.deduplicate_results([first, second])
    assert len(kept) == 1
    assert kept[0].related_coverage == ()


def test_update40_same_source_later_development_remains_separate():
    first = make_result("https://example.com/complaint", "Брест")
    second = make_result("https://example.com/response", "Брест")
    first.source_name = second.source_name = "Одно СМИ"
    first.title = "Жители жалуются на плохую дорогу у дома"
    first.published_at = "2026-08-10T08:00:00+03:00"
    second.title = "После жалоб дорогу у дома отремонтировали"
    second.published_at = "2026-08-11T08:00:00+03:00"
    assert len(social_monitor.deduplicate_results([first, second])) == 2


def test_update40_later_development_remains_a_separate_card():
    first = make_result("https://one.example/complaint", "Брест")
    first.source_name = "Первое СМИ"
    first.title = "Жители жалуются на плохую дорогу у дома"
    first.published_at = "2026-08-10T08:00:00+03:00"
    second = make_result("https://two.example/response", "Брест")
    second.source_name = "Второе СМИ"
    second.title = "После жалоб дорогу у дома отремонтировали"
    second.published_at = "2026-08-11T08:00:00+03:00"
    assert len(social_monitor.deduplicate_results([first, second])) == 2


def _update40_concrete_event(
    url: str,
    source_name: str,
    title: str,
    excerpt: str,
    published_at: str,
    signature: str = "минск|water_supply|outage",
) -> social_monitor.ArticleResult:
    item = make_result(url, "Беларусь")
    item.source_name = source_name
    item.title = title
    item.excerpt = excerpt
    item.published_at = published_at
    item.category = "ЖКХ и состояние жилья"
    item.event_region = "Минск"
    item.event_locality = "Минск"
    item.event_object = "водоснабжение"
    item.event_problem = "отсутствие/перебои"
    item.event_signature = signature
    return item


def test_update40_news_and_analysis_never_merge_even_with_immediate_overlap():
    evidence = (
        "Жители дома 10 на улице Ленина жалуются: вторые сутки нет холодной "
        "воды, аварийная служба пока не решила проблему."
    )
    news = _update40_concrete_event(
        "https://news.example/water", "Новости", "Жители дома остались без воды",
        evidence, "2026-08-12T08:00:00+03:00",
    )
    analysis = _update40_concrete_event(
        "https://analysis.example/water", "Аналитика",
        "Разбираемся, почему городские сети дают сбои",
        evidence + " Эксперт анализирует причины износа сетей.",
        "2026-08-12T10:00:00+03:00",
    )
    assert len(social_monitor.deduplicate_results([news, analysis])) == 2


def test_update40_news_and_development_never_merge_within_six_hours():
    evidence = (
        "Жители дома 10 на улице Ленина жалуются: вторые сутки нет холодной "
        "воды, аварийная служба пока не решила проблему."
    )
    news = _update40_concrete_event(
        "https://news.example/complaint", "Новости",
        "Жители дома остались без воды", evidence,
        "2026-08-12T08:00:00+03:00",
    )
    development = _update40_concrete_event(
        "https://other.example/response", "Другое СМИ",
        "После жалоб воду в доме вернули",
        evidence + " После обращения аварию устранили и воду вернули.",
        "2026-08-12T10:00:00+03:00",
    )
    assert len(social_monitor.deduplicate_results([news, development])) == 2


def test_update40_two_reports_of_same_development_can_consolidate():
    evidence = (
        "После жалоб жителей дома 10 на улице Ленина аварию устранили и "
        "холодную воду вернули утром 12 августа."
    )
    first = _update40_concrete_event(
        "https://one.example/response", "Первое СМИ",
        "После жалоб воду в доме вернули", evidence,
        "2026-08-12T10:00:00+03:00",
    )
    second = _update40_concrete_event(
        "https://two.example/response", "Второе СМИ",
        "После обращения жителей воду в доме вернули", evidence,
        "2026-08-12T10:20:00+03:00",
    )
    kept = social_monitor.deduplicate_results([first, second])
    assert len(kept) == 1
    assert len(kept[0].related_coverage) == 1


def test_update40_same_coarse_signature_different_addresses_stay_separate():
    first = _update40_concrete_event(
        "https://one.example/lenina", "Первое СМИ",
        "На улице Ленина дом остался без воды",
        "Дом 10 на улице Ленина вторые сутки остается без воды.",
        "2026-08-12T08:00:00+03:00",
    )
    second = _update40_concrete_event(
        "https://two.example/pushkina", "Второе СМИ",
        "На улице Пушкина дом остался без воды",
        "Дом 55 на улице Пушкина утром остался без воды.",
        "2026-08-12T09:00:00+03:00",
    )
    assert len(social_monitor.deduplicate_results([first, second])) == 2


def test_update40_materially_different_numeric_facts_stay_separate():
    first = _update40_concrete_event(
        "https://one.example/outage-a", "Первое СМИ",
        "Авария оставила без воды 10 домов",
        "После повреждения трубы без воды остались 10 домов микрорайона.",
        "2026-08-12T08:00:00+03:00",
    )
    second = _update40_concrete_event(
        "https://two.example/outage-b", "Второе СМИ",
        "Авария оставила без воды 200 домов",
        "После повреждения трубы без воды остались 200 домов микрорайона.",
        "2026-08-12T09:00:00+03:00",
    )
    assert len(social_monitor.deduplicate_results([first, second])) == 2


def test_update40r1_cross_category_rewrites_with_shared_event_passage_consolidate():
    shared = (
        "Единственное, хозяин снял обшивку. Приезжал сотрудник МЧС, все "
        "проверил: никаких претензий и вопросов не возникло. Хотя на участке "
        "и баня есть. Возмущается Ольга."
    )
    first = make_result("https://priority.example/family", "Беларусь")
    first.source_name = "Приоритетное СМИ"
    first.priority = "A"
    first.title = (
        "Многодетная семья пытается выбраться из двухкомнатной квартиры, "
        "но ей отказывают в покупке дома"
    )
    first.category = "Законы, права и общественное регулирование"
    first.excerpt = shared + " Оценка дома стоит больше 1000 рублей."
    first.published_at = "2026-08-12T08:00:00+03:00"

    second = make_result("https://regional.example/family", "Беларусь")
    second.source_name = "Региональное СМИ"
    second.priority = "B"
    second.title = (
        "Почему многодетным из Старых Дорог не разрешают потратить "
        "материнский капитал?"
    )
    second.category = "Повседневная безопасность"
    second.excerpt = shared + " Семья готова потратить 30 тысяч рублей."
    second.published_at = "2026-08-12T09:00:00+03:00"

    kept = social_monitor.deduplicate_results([second, first])
    assert len(kept) == 1
    assert kept[0].source_name == "Приоритетное СМИ"
    assert kept[0].related_coverage == ((
        "Региональное СМИ", "https://regional.example/family"
    ),)


def test_update40_html_and_csv_show_compact_resonance_links(tmp_path):
    item = make_result("https://primary.example/story", "Брест")
    item.source_name = "Основное СМИ"
    item.related_coverage = (
        ("Второе СМИ", "https://second.example/story"),
        ("Третье СМИ", "https://third.example/story"),
    )
    settings = copy.deepcopy(SETTINGS)
    html_report = social_monitor.build_html_report(
        [item], [], settings, False, []
    )
    assert "Этот сюжет также освещали:" in html_report
    assert '<a href="https://second.example/story">Второе СМИ</a>' in html_report
    assert "сюжетов: <strong>1</strong>" in html_report
    assert "публикаций с учётом резонанса:" in html_report
    csv_path = tmp_path / "report.csv"
    social_monitor.write_csv_report(csv_path, [item])
    rows = list(social_monitor.csv.DictReader(
        csv_path.open(encoding="utf-8-sig")
    ))
    assert rows[0]["also_covered_by"] == "Второе СМИ, Третье СМИ"
    assert rows[0]["also_covered_urls"] == (
        "https://second.example/story | https://third.example/story"
    )


def test_update40_partial_smtp_refusal_is_masked_and_recorded(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO", logger="social_monitor")
    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, *args):
            return None

        def send_message(self, message):
            return {"bpc@ghu.by": (550, b"mailbox rejected")}

    monkeypatch.setenv("SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("REPORT_TO", "bpc@ghu.by")
    monkeypatch.setattr(social_monitor.smtplib, "SMTP_SSL", FakeSMTP)
    settings = {
        "monitor": {"timezone": "Europe/Minsk"},
        "report": {"email_recipients": ["vladreuth@gmail.com"]},
    }
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("a,b\n", encoding="utf-8")
    diagnostics = []
    sent = social_monitor.send_email(
        "<p>test</p>", csv_path, None, 1, False, settings, diagnostics
    )
    assert sent is False
    assert len(diagnostics) == 1
    assert "частичный отказ" in diagnostics[0]
    assert "b***@g***.by" in diagnostics[0]
    assert "bpc@ghu.by" not in diagnostics[0]
    assert "v***@g***.com=accepted" in caplog.text
    assert "bpc@ghu.by" not in caplog.text


def test_update40_all_smtp_recipients_accepted(monkeypatch, tmp_path):
    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, *args):
            return None

        def send_message(self, message):
            return {}

    monkeypatch.setenv("SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("REPORT_TO", "bpc@ghu.by")
    monkeypatch.setattr(social_monitor.smtplib, "SMTP_SSL", FakeSMTP)
    settings = {
        "monitor": {"timezone": "Europe/Minsk"},
        "report": {"email_recipients": ["vladreuth@gmail.com"]},
    }
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("a,b\n", encoding="utf-8")
    assert social_monitor.send_email(
        "<p>test</p>", csv_path, None, 1, False, settings, []
    ) is True


# --- Core 3.3: title-aware ranking and bounded soft source limits. ---

def _core33_candidate(
    name: str,
    priority: str,
    url: str,
    title: str,
    published_at: str = "2026-08-14T10:00:00+03:00",
    discovered_via: str = "feed:https://example.com/feed",
) -> Candidate:
    src = social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name=name,
        priority=priority,
        rank={"A": 1, "B": 2, "C": 3}.get(priority, 9),
        domain=url.split("//", 1)[-1].split("/", 1)[0],
    )
    return Candidate(
        source=src,
        url=url,
        title=title,
        published_at=published_at,
        discovered_via=discovered_via,
    )


def test_core33_lower_priority_protected_title_outranks_generic_a_source():
    cutoff = social_monitor.parse_datetime("2026-08-14T00:00:00+03:00")
    generic_a = _core33_candidate(
        "Источник A", "A", "https://a.example/generic",
        "Новости района за сегодняшний день",
    )
    protected_c = _core33_candidate(
        "Источник C", "C", "https://c.example/water",
        "Жители жалуются: в доме нет горячей воды",
    )
    ranked, contracts = social_monitor.rank_candidates_core33(
        [generic_a, protected_c], cutoff, SETTINGS
    )
    assert ranked[0].url == protected_c.url
    contract = next(
        item for item in contracts if item.canonical_url == protected_c.url
    )
    assert contract.protected_title_admission is True
    assert contract.ranking_tier == "protected_title"


def test_core33_source_priority_wins_inside_same_tier():
    cutoff = social_monitor.parse_datetime("2026-08-14T00:00:00+03:00")
    priority_a = _core33_candidate(
        "Источник A", "A", "https://a.example/water",
        "Жители жалуются: в доме нет горячей воды",
    )
    priority_c = _core33_candidate(
        "Источник C", "C", "https://c.example/water",
        "Жители жалуются: в доме нет горячей воды",
    )
    ranked, _contracts = social_monitor.rank_candidates_core33(
        [priority_c, priority_a], cutoff, SETTINGS
    )
    assert [item.source.priority for item in ranked] == ["A", "C"]


def test_core33_fresh_candidate_always_outranks_stale_soft_candidate():
    cutoff = social_monitor.parse_datetime("2026-08-14T00:00:00+03:00")
    stale = _core33_candidate(
        "Архив", "A", "https://a.example/archive",
        "Жители жалуются: в доме нет горячей воды",
        published_at="2026-08-01T10:00:00+03:00",
    )
    fresh = _core33_candidate(
        "Свежий", "C", "https://c.example/current",
        "Новости района за сегодняшний день",
    )
    ranked, contracts = social_monitor.rank_candidates_core33(
        [stale, fresh], cutoff, SETTINGS
    )
    assert ranked[0].url == fresh.url
    stale_contract = next(
        item for item in contracts if item.canonical_url == stale.url
    )
    assert stale_contract.admission_status == "soft"
    assert stale_contract.protected_title_admission is False


def test_core33_integrity_flags_are_diagnostic_and_do_not_drop_candidate():
    cutoff = social_monitor.parse_datetime("2026-08-14T00:00:00+03:00")
    incomplete = _core33_candidate(
        "Неполный", "B", "https://b.example/unknown", "",
        published_at="", discovered_via="sitemap:https://b.example/sitemap.xml",
    )
    complete = _core33_candidate(
        "Полный", "B", "https://b.example/complete",
        "Жители жалуются: в доме нет горячей воды",
    )
    ranked, contracts = social_monitor.rank_candidates_core33(
        [incomplete, complete], cutoff, SETTINGS
    )
    assert {item.url for item in ranked} == {incomplete.url, complete.url}
    contract = next(
        item for item in contracts if item.canonical_url == incomplete.url
    )
    assert {"missing_title", "undated", "soft_admission"} <= set(
        contract.integrity_flags
    )


def test_core33_channel_soft_scan_prefers_relevant_headline_from_tail():
    cutoff = social_monitor.parse_datetime("2026-08-14T00:00:00+03:00")
    generic = [
        _core33_candidate(
            "Один источник", "A", f"https://a.example/generic-{index}",
            f"Новости района, выпуск {index}",
        )
        for index in range(3)
    ]
    protected = _core33_candidate(
        "Один источник", "A", "https://a.example/protected",
        "Жители жалуются: в доме нет горячей воды",
    )
    selected = social_monitor.trim_channel_candidates(
        [*generic, protected], 2, cutoff, SETTINGS
    )
    assert protected.url in {item.url for item in selected}


def test_core33_source_limit_is_soft_only_for_protected_headlines():
    cutoff = social_monitor.parse_datetime("2026-08-14T00:00:00+03:00")
    protected = [
        _core33_candidate(
            "Один источник", "A", f"https://a.example/problem-{index}",
            f"Жители жалуются: в доме {index} дней нет горячей воды",
        )
        for index in range(4)
    ]
    stale = _core33_candidate(
        "Один источник", "A", "https://a.example/stale",
        "Жители жалуются: в доме нет горячей воды",
        published_at="2026-08-01T10:00:00+03:00",
    )
    selected = social_monitor.select_balanced_source_candidates(
        [*protected, stale],
        2,
        feed_reserve=2,
        homepage_reserve=0,
        sitemap_reserve=0,
        cutoff=cutoff,
        settings=SETTINGS,
        soft_overflow_limit=2,
    )
    assert len(selected) == 4
    assert stale.url not in {item.url for item in selected}
    assert all("problem-" in item.url for item in selected)


def test_core33_soft_source_ceiling_remains_bounded():
    cutoff = social_monitor.parse_datetime("2026-08-14T00:00:00+03:00")
    protected = [
        _core33_candidate(
            "Один источник", "A", f"https://a.example/problem-{index}",
            f"Жители жалуются: в доме {index} дней нет горячей воды",
        )
        for index in range(8)
    ]
    selected = social_monitor.select_balanced_source_candidates(
        protected,
        3,
        feed_reserve=3,
        homepage_reserve=0,
        sitemap_reserve=0,
        cutoff=cutoff,
        settings=SETTINGS,
        soft_overflow_limit=2,
    )
    assert len(selected) == 5


def test_core33_default_soft_overflow_is_quarter_with_safety_cap():
    settings = copy.deepcopy(SETTINGS)
    settings["monitor"]["per_source_candidate_limit"] = 40
    settings["monitor"]["source_candidate_limits"] = {}
    src = source("Беларусь", "Беларусь")
    assert social_monitor.source_soft_overflow_limit(src, settings) == 10
    settings["monitor"]["per_source_candidate_limit"] = 100
    assert social_monitor.source_soft_overflow_limit(src, settings) == 15


def test_core33_ranking_is_deterministic_for_reversed_input():
    cutoff = social_monitor.parse_datetime("2026-08-14T00:00:00+03:00")
    candidates = [
        _core33_candidate(
            f"Источник {index}", "B", f"https://b.example/{index}",
            f"Новости района, выпуск {index}",
        )
        for index in range(4)
    ]
    first, _ = social_monitor.rank_candidates_core33(
        candidates, cutoff, SETTINGS
    )
    second, _ = social_monitor.rank_candidates_core33(
        list(reversed(candidates)), cutoff, SETTINGS
    )
    assert [item.url for item in first] == [item.url for item in second]


def test_core33_fast_prefilter_matches_existing_metadata_states():
    candidates = [
        _core33_candidate(
            "Тест", "A", "https://a.example/strong",
            "Жители жалуются: в доме нет горячей воды",
        ),
        _core33_candidate(
            "Тест", "A", "https://a.example/possible",
            "В доме проверили горячую воду",
        ),
        _core33_candidate(
            "Тест", "A", "https://a.example/unknown",
            "Новости района за сегодняшний день",
        ),
    ]
    for item in candidates:
        expected = social_monitor.metadata_prefilter(item, SETTINGS)
        actual = social_monitor.candidate_ranking_prefilter(item, SETTINGS)
        assert actual.status == expected.status
        assert actual.title_signal == expected.title_signal


def _core33_source_for_domain(name: str, domain: str) -> Source:
    return social_monitor.replace(
        source("Беларусь", "Беларусь"),
        name=name,
        domain=domain,
        start_url=f"https://{domain}/",
    )


def test_core33r_onliner_marketplace_routes_are_service_pages():
    src = _core33_source_for_domain("Onlíner", "onliner.by")
    urls = (
        "http://baraholka.onliner.by/fleamarketposting.php",
        "https://baraholka.onliner.by/viewforum.php?f=288",
    )
    for url in urls:
        item = Candidate(source=src, url=url, title="Объявления")
        assert social_monitor.classify_source_url(
            url, src.domain, social_monitor.source_adapter_profile(src)
        ) == "service"
        assert social_monitor.candidate_service_like(item) is True


def test_core33r_onliner_forum_routes_are_service_pages():
    src = _core33_source_for_domain("Onlíner", "onliner.by")
    url = "https://forum.onliner.by/viewtopic.php?t=25682530"
    item = Candidate(source=src, url=url, title="Форум")
    assert social_monitor.classify_source_url(
        url, src.domain, social_monitor.source_adapter_profile(src)
    ) == "service"
    assert social_monitor.candidate_service_like(item) is True


def test_core33r_slutsk_classified_ad_is_a_service_page():
    src = _core33_source_for_domain("Слуцк-Город", "slutsk-gorod.by")
    url = (
        "http://slutsk-gorod.by/obyavleniya-slutsk/"
        "101-uslugi-dlya-stroitelstva/26105-santekhnik"
    )
    item = Candidate(source=src, url=url, title="Сантехник")
    assert social_monitor.classify_source_url(
        url, src.domain, social_monitor.source_adapter_profile(src)
    ) == "service"
    assert social_monitor.candidate_service_like(item) is True


def test_core33r_onliner_newsroom_story_remains_eligible():
    src = _core33_source_for_domain("Onlíner", "onliner.by")
    url = "https://people.onliner.by/2026/08/14/story"
    item = Candidate(
        source=src,
        url=url,
        title="Жители жалуются на отсутствие воды",
        published_at="2026-08-14T10:00:00+03:00",
    )
    assert social_monitor.classify_source_url(
        url, src.domain, social_monitor.source_adapter_profile(src)
    ) == "article"
    assert social_monitor.candidate_service_like(item) is False
