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
    region: str = "Брестская область",
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
        published_at="2026-08-25T15:47:35+00:00",
        category="Качество товаров и услуг",
        subcategory="",
        excerpt=excerpt,
        signal_type="критический материал или выявленные нарушения",
        official_response=False,
        score=10,
        matched_terms="",
        discovered_via="feed:test",
        text_length=len(excerpt),
        event_region=fingerprint.region or region,
        event_locality=fingerprint.locality,
        event_object=fingerprint.object_label,
        event_problem=fingerprint.problem_label,
        event_signature=fingerprint.signature,
    )


def test_result_integrity_16_rejects_report_10_noise():
    cases = (
        (
            "Без воды на время работ: жители Бреста и района получили предупреждение 26 августа",
            "Брестводоканал сообщил о временных отключениях. Ограничения связаны с "
            "плановыми ремонтными работами и промывкой сетей по нескольким адресам.",
        ),
        (
            "Самолет Белавиа экстренно сел в Анкаре из-за пассажира. Спасти его не удалось",
            "Пассажир жаловался на головную боль. Самолет экстренно сел, врачи помочь не смогли.",
        ),
        (
            "На красный не ходим, матом не кричим: россиянин рассказал, как себя вести в Беларуси",
            "Автор составил памятку переезжающим. Правила есть правила. Его поддержали читатели.",
        ),
        (
            "Почему белорусы не любят российских туристов",
            "Автор рассуждает о бытовой культуре, парковке и реакции пользователей соцсетей.",
        ),
        (
            "У Гомелі беларус паказаў, як прыпаркаваўся аўтамабіль на расейскіх нумарах",
            "Адзін ролік сабраў каментары. Карыстальнікі прапанавалі звярнуцца ў ДАІ.",
        ),
        (
            "Снесла зеркало, вызвала ГАИ сама и получила штраф: водители спорят о справедливости наказания",
            "Автомобилистка получила штраф. Частная история вызвала обсуждение водителей.",
        ),
        (
            "В Беларуси впервые имплантировали новейший сердечный клапан",
            "Уникальную операцию успешно провели одному пациенту. Это первая такая операция в стране.",
        ),
        (
            "С 1 сентября 2026 года в Беларуси заработает сервис, который упростит поиск репетитора",
            "Запускается добровольный проект для поиска репетиторов, логопедов и музыкальных руководителей.",
        ),
        (
            "Кого теперь будут направлять в соцучреждения по новым правилам?",
            "Вступают в силу изменения направления граждан в социальные пансионаты. "
            "Министерство разъяснило медицинские показания.",
        ),
        (
            "В Оршанском районе",
            "Работники предприятия самостоятельно ликвидировали тление льнотресты. "
            "Пострадавших нет, риски повторного загорания отсутствуют.",
        ),
    )
    for title, text in cases:
        assert not decision(title, text).relevant, title


def test_result_integrity_16_keeps_report_10_public_problem_controls():
    cases = (
        (
            "Жители Орши показали заросшие дворы и разбитые скамейки",
            "Жители разных районов жалуются на мусор и заросли. Месяц ждут покоса, "
            "а обращения в ЖКХ не помогают.",
        ),
        (
            "Рыбки из кафе, которых использовали для декора, спасены",
            "После жалоб аквариумисты подтвердили неправильные условия содержания: "
            "перегрев, отсутствие фильтрации и нехватку воды. Рыбок забрали.",
        ),
        (
            "В продаже в Гомеле нашли более 2 тыс. пар контрафактной обуви",
            "У продавцов не было документов о качестве и безопасности. "
            "Нарушения подтвердили, товар изъяли, виновных оштрафовали.",
        ),
        (
            "Пирожные и рулетики белорусского производителя запретили",
            "Дрожжей было в три раза больше нормы, обнаружены кишечная палочка "
            "и золотистый стафилококк.",
        ),
        (
            "Гомельчанин показал, как критически обмелел Днепр",
            "Жители давно наблюдают обмеление. Данные гидропостов подтверждают "
            "критически низкий уровень воды.",
        ),
    )
    for title, text in cases:
        assert decision(title, text).relevant, title


def test_result_integrity_16_keeps_actual_public_discussion_of_law():
    result = decision(
        "Белорусы обсуждают новые правила направления в социальные учреждения",
        "Семьи людей с инвалидностью направили коллективное обращение: они жалуются, "
        "что новые правила ограничивают доступ к круглосуточному уходу.",
    )
    assert result.relevant
    assert result.category == "Законы, права и общественное регулирование"


def test_result_integrity_16_keeps_unplanned_persistent_water_outage():
    result = decision(
        "Жители третий день остаются без воды после аварии",
        "Жители нескольких домов жалуются: аварийное отключение продолжается трое суток, "
        "срок восстановления водоснабжения не назван.",
        "Брест",
    )
    assert result.relevant


def test_event_integrity_16_fingerprints_report_10_problem_families():
    low_water = social_monitor.infer_event_fingerprint(
        "Гомельчанин показал, как критически обмелел Днепр",
        "",
        "Гидропосты подтверждают низкий уровень воды.",
    )
    assert low_water.locality == "Гомель"
    assert low_water.object_key == "natural_water"
    assert low_water.problem_key == "low_water"
    assert low_water.signature

    counterfeit = social_monitor.infer_event_fingerprint(
        "В продаже в Гомеле нашли более 2 тыс. пар контрафактной обуви",
        "",
        "У продавцов не было документов о качестве и безопасности.",
    )
    assert counterfeit.locality == "Гомель"
    assert counterfeit.object_key == "consumer_goods"
    assert counterfeit.problem_key == "counterfeit"
    assert counterfeit.signature


def test_event_integrity_16_links_full_and_short_confectionery_rewrites():
    full = social_monitor.infer_event_fingerprint(
        "В пирожных из Пинска нашли кишечную палочку и золотистый стафилококк",
        "",
        "Опасную продукцию Пинского кооппрома запретили.",
    )
    short = social_monitor.infer_event_fingerprint(
        "Пирожные и рулетики белорусского производителя запретили",
        "",
        "Дрожжей втрое больше нормы, кишечная палочка и золотистый стафилококк.",
    )
    assert full.signature
    assert full.signature == short.signature

    results = social_monitor.deduplicate_results([
        article(
            "Onliner",
            "В пирожных из Пинска нашли кишечную палочку и золотистый стафилококк",
            "Опасную продукцию Пинского кооппрома запретили.",
            "https://example.by/full",
        ),
        article(
            "Могилёв Online",
            "Пирожные и рулетики белорусского производителя запретили",
            "Дрожжей втрое больше нормы, кишечная палочка и золотистый стафилококк.",
            "https://example.by/short",
        ),
    ])
    assert len(results) == 1
    assert len(results[0].related_coverage) == 1


def test_event_integrity_16_does_not_globalize_single_pathogen_findings():
    brest = social_monitor.infer_event_fingerprint(
        "В пирожных из Пинска нашли стафилококк",
        "",
        "Пинские пирожные запретили.",
    )
    minsk = social_monitor.infer_event_fingerprint(
        "В минских пирожных нашли стафилококк",
        "",
        "Партию десертов запретили.",
    )
    assert brest.signature
    assert minsk.signature
    assert brest.signature != minsk.signature
