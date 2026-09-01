"""Update 75: Result Integrity genre gates and the event-object classifier
were audited for one-sided (Russian-only) coverage after a confirmed leak.

Context (from the 2026-08-31 report-29 artefact): update 74 added a
Russian-only regex to reject aggregated state-sector credit-arrears
statistics ("Просроченная задолженность госсектора по кредитам..."). The
next production run showed the *same* Pozirk story, published in parallel
at a Belarusian-language URL, sailing through untouched and landing in the
final HTML report — because "просроченный"/"пратэрмінаваны",
"задолженность"/"запазычанасць" and "госсектор"/"дзяржсектар" are different
roots, not just different endings, so the shared [а-яёіў] suffix class used
throughout this file cannot bridge them.

These tests pin the fix using the real leaked headline (in both languages)
rather than synthetic examples, plus a second high-risk pattern from the
same update, plus an unrelated but co-discovered classifier bug where a
laundry-pricing story was mistagged "waste" because it mentioned a
sanitary-epidemiological inspection service.
"""

from pathlib import Path

import social_monitor


SETTINGS = social_monitor.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source(language: str = "ru") -> social_monitor.Source:
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
        language=language,
        adapter="standard",
    )


def decision(title: str, text: str, language: str = "ru"):
    return social_monitor.evaluate_relevance(title, "", text, source(language), SETTINGS)


def test_aggregate_credit_debt_statistic_rejected_in_russian():
    # The RU mirror that was already correctly rejected before this patch;
    # kept here so a future refactor of the group cannot silently drop it.
    ru = decision(
        "Просроченная задолженность госсектора по кредитам в годовом "
        "измерении выросла в 4,4 раза",
        "На 1 августа задолженность госсектора по кредитам составила "
        "33 млрд рублей. Просроченная задолженность выросла в 4,4 раза.",
    )
    assert not ru.relevant, ru.reason


def test_aggregate_credit_debt_statistic_rejected_in_belarusian():
    # The exact headline that leaked through in report-29
    # (pozirk.online/be/news/201827), discovered via the outlet's Telegram
    # channel. Must now be rejected the same as its RU mirror above.
    be = decision(
        "Пратэрмінаваная запазычанасць дзяржсектара па крэдытах",
        "На 1 жніўня запазычанасць дзяржсектара па крэдытах склала "
        "33 млрд рублёў. Пратэрмінаваная запазычанасць вырасла ў 4,4 разы.",
        language="be",
    )
    assert not be.relevant, be.reason


def test_positive_public_infrastructure_opening_rejected_in_belarusian():
    be = decision(
        "У Магілёве адкрываецца новая падземка на праспекце Міра — паглядзелі, як яна выглядае",
        "Аб'ект прайшоў прыёмку і пачне функцыянаваць заўтра. У падземнага "
        "перахода тры ўваходы і ліфты.",
        language="be",
    )
    assert not be.relevant, be.reason


def test_waste_object_no_longer_fires_on_bare_sanitary_mention():
    # The report-29 case: a municipal-procurement story about laundry
    # pricing mentioned a "sanitary-epidemiological service" in passing and
    # was mistagged event_object="waste" purely because "санитар" was an
    # unanchored substring match.
    fingerprint = social_monitor.infer_event_fingerprint(
        "В Гродненской области социальные учреждения платили за стирку "
        "белья по ценам выше, чем у госорганизаций",
        "",
        "К качеству оказываемых отдельными коммерческими организациями "
        "услуг имелись претензии как со стороны потребителей, так и со "
        "стороны санитарно-эпидемиологических служб.",
    )
    assert fingerprint.object_key != "waste"


def test_waste_object_still_fires_on_genuine_antisanitary_condition():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Жители пожаловались на антисанитарное состояние возле рынка",
        "",
        "Несколько дней подряд там стоят нечистоты, санитарное состояние "
        "территории жители называют критическим.",
    )
    assert fingerprint.object_key == "waste"


def test_waste_object_still_fires_on_belarusian_antisanitary_condition():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Жыхары паскардзіліся на антысанітарны стан двара",
        "",
        "Санітарны стан тэрыторыі жыхары называюць крытычным.",
    )
    assert fingerprint.object_key == "waste"


def test_greenery_object_no_longer_fires_on_travma():
    # "трав" was bare and matched "травма" ("трав"+"ма"): an injury story
    # was mistagged event_object="greenery" (mowing/greenery) purely
    # because it contained the word "травма" (injury).
    fingerprint = social_monitor.infer_event_fingerprint(
        "Ребёнок получил травму на детской площадке из-за сломанной горки",
        "",
        "Родители жалуются, что горка на площадке сломана уже месяц, "
        "травма ребёнка потребовала обращения к врачу.",
    )
    assert fingerprint.object_key != "greenery"


def test_greenery_object_still_fires_on_genuine_mowing_complaint():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Жители жалуются, что возле детской площадки не косят траву уже два месяца",
        "",
        "Высокая трава мешает детям играть, жильцы просят "
        "коммунальщиков скосить траву у площадки.",
    )
    assert fingerprint.object_key == "greenery"


def test_work_conditions_no_longer_fires_on_spekulyatsiya():
    # "спек" was bare and matched "спекуляция"/"спектакль"/"спектр": a
    # price-speculation story was mistagged event_problem="work_conditions"
    # (heat) purely because it contained the word "спекуляция".
    fingerprint = social_monitor.infer_event_fingerprint(
        "Продавцов заподозрили в спекуляции ценами на овощи на рынке",
        "",
        "Покупатели жалуются, что торговцы завышают цены, налоговая "
        "проверяет факты спекуляции на центральном рынке.",
    )
    assert fingerprint.problem_key != "work_conditions"


def test_work_conditions_still_fires_on_genuine_heat_complaint():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Рабочие жалуются на жару в цехе без кондиционеров",
        "",
        "Температура в цехе превышает норму, спека стоит уже неделю, "
        "условия труда работники называют невыносимыми.",
    )
    assert fingerprint.problem_key == "work_conditions"


def test_greenery_object_no_longer_fires_on_otravilis_real_text():
    # The exact real-world production text from report-30 (2026-08-31):
    # a food-poisoning story in Turkey was mistagged event_object=
    # "greenery" purely because "трав" is a substring of "отравились".
    # This is the case that slipped through the first "трав(?!м)" patch,
    # since that lookahead only guards what follows the match, not what
    # precedes it.
    fingerprint = social_monitor.infer_event_fingerprint(
        "Почти 400 человек отравились на празднике в Турции",
        "",
        "Санитарные службы изъяли образцы пищи, начата проверка для "
        "выяснения обстоятельств и причин произошедшего. Около 400 "
        "человек, которые принимали участие в торжественном мероприятии "
        "в Турецкой провинции Бурса, обратились к врачам с жалобами на "
        "отравление. \"В местные больницы обратились 367 граждан, "
        "которые отравились после употребления курицы с рисом на "
        "мероприятии\".",
    )
    assert fingerprint.object_key != "greenery"


def test_greenery_object_still_fires_on_prefixed_mowing_forms():
    # "покосил/скосил/выкосил" are legitimate perfective forms of mowing
    # and must keep matching even after anchoring "косил" against
    # "закосил" (draft-dodging slang, unrelated).
    for verb in ("покосил", "скосил", "выкосил"):
        fingerprint = social_monitor.infer_event_fingerprint(
            f"Коммунальники наконец {verb} газон, заросший месяц назад",
            "",
            "Жалобы на заросший участок поступали от местных жителей.",
        )
        assert fingerprint.object_key == "greenery", verb


def test_greenery_object_no_longer_fires_on_draft_dodging_slang():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Студент рассказал, как закосил от армии по состоянию здоровья",
        "",
        "История о том, как молодой человек избежал срочной службы.",
    )
    assert fingerprint.object_key != "greenery"


def test_greenery_object_no_longer_fires_on_odereveneli_from_shock():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Женщина одеревенела от испуга, услышав новость о сыне",
        "",
        "Свидетели рассказали, что она несколько минут не могла "
        "пошевелиться от шока.",
    )
    assert fingerprint.object_key != "greenery"


def test_public_transport_no_longer_fires_on_suspended_license():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Суд вынес решение о приостановке лицензии предприятия",
        "",
        "Приостановка деятельности связана с нарушениями при производстве.",
    )
    assert fingerprint.object_key != "public_transport"


def test_public_transport_still_fires_on_genuine_stop_complaint():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Жители жалуются на отсутствие автобусной остановки у школы",
        "",
        "Ближайшая остановка находится в километре, детям приходится "
        "идти вдоль трассы.",
    )
    assert fingerprint.object_key == "public_transport"


def test_pollution_no_longer_fires_on_vostok_istok_listok():
    for text in (
        "Новостройка на востоке города обрастает инфраструктурой",
        "У истоков этого общественного движения стояли местные жители",
        "Ей выписали больничный листок на две недели",
    ):
        fingerprint = social_monitor.infer_event_fingerprint(text, "", text)
        assert fingerprint.problem_key != "pollution", text


def test_pollution_still_fires_on_drain_and_downspout():
    for text in (
        "Во дворе много недель не могут прочистить сток дождевой воды",
        "Жильцы жалуются, что водосток на крыше давно не чистили",
    ):
        fingerprint = social_monitor.infer_event_fingerprint(text, "", text)
        assert fingerprint.problem_key == "pollution", text


def test_housing_no_longer_fires_on_dvorets_kultury():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Во Дворце культуры прошел концерт ко дню города",
        "",
        "Мероприятие собрало несколько сотен зрителей.",
    )
    assert fingerprint.object_key != "housing"


def test_housing_still_fires_on_genuine_yard_complaint():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Жители жалуются на состояние двора и подъезда",
        "",
        "Во дворе давно не убирались, подъезд требует ремонта.",
    )
    assert fingerprint.object_key == "housing"
