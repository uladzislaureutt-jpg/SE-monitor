"""Three new classification bugs found by reviewing report-34/35
(2026-09-01), the first real production data after update75 landed.

1. A bare "работник" in `employment_context_terms` (settings.yaml) gave a
   +14 category-weight boost to "Работа, зарплаты и доходы" for ANY story
   mentioning any kind of worker ("работники МЧС", "медицинские
   работники"...), not just genuine labor/employment stories. Confirmed
   real case: a fire-rescue story ("Работники МЧС... спасли мужчину") was
   categorized as "Работа, зарплаты и доходы" with an EMPTY subcategory
   (no genuine keyword hit), because the bonus channel that won doesn't
   populate the hit list the way keyword-list matches do -- the empty
   subcategory was the tell that something was wrong. Unlike the
   EVENT_OBJECT/PROBLEM bare-token bugs, this wasn't a regex anchoring gap
   -- `term_present()` already anchors single-word terms to word starts.
   It's pure semantic over-breadth: "работник" correctly and grammatically
   means "employee" in "работники МЧС" too, so anchoring can't fix it; the
   term itself was the wrong choice for a labor-context signal and is
   redundant with the other 13, more specific terms already in the list.

2. "дерев" (EVENT_OBJECT "greenery") matched "деревня/деревне/деревень"
   (village) -- a different, very common word sharing the same root,
   word-initial, which the update75 word-start anchor can't distinguish
   from "дерево" (tree). Confirmed real case: "Стрельба в голландской
   деревне" (shooting in a Dutch VILLAGE) mistagged event_object=
   "greenery".

3. "задерж"/"затрым" (EVENT_PROBLEM "queue_delay") matched the verb
   "задержали/затрымалі" (detained a person -- a law-enforcement action),
   not just the noun "задержка/затрымка" (a delay). Confirmed on the same
   shooting story: "Правоохранители задержали несколько человек" mistagged
   event_problem="queue/delay". Narrowed to the noun stem; this is a
   deliberate under-match, not a guess at disambiguating the verb form.
"""

from pathlib import Path

import social_monitor as sm


SETTINGS = sm.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def _source(language: str = "ru") -> sm.Source:
    return sm.Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Гродно",
        rank=1,
        priority="A",
        name="Тестовый источник",
        media_type="website",
        domain="example.by",
        start_url="https://example.by",
        language=language,
        adapter="standard",
    )


def test_employment_category_no_longer_fires_on_generic_workers_real_text():
    # Exact real text from report-35 (2026-09-01), ВГР.
    d = sm.evaluate_relevance(
        "В Гродно на пожаре спасли мужчину",
        "",
        "Из окна квартиры, расположенной на третьем этаже "
        "четырехэтажного строения, выбивался дым. Работники МЧС из "
        "опасной зоны спасли мужчину и передали его медикам. После "
        "осмотра пострадавший был госпитализирован.",
        _source(),
        SETTINGS,
    )
    assert d.category != "Работа, зарплаты и доходы"
    assert d.category == "Повседневная безопасность"


def test_employment_category_still_fires_on_genuine_labor_complaint():
    d = sm.evaluate_relevance(
        "Работники завода жалуются на невыносимые условия труда в цеху",
        "",
        "Сотрудники говорят, что работодатель не обеспечивает средства "
        "защиты, условия труда не соответствуют нормам.",
        _source(),
        SETTINGS,
    )
    assert d.category == "Работа, зарплаты и доходы"


def test_greenery_object_no_longer_fires_on_derevnya_real_text():
    # Exact real text from report-35 (2026-09-01), Виртуальный Брест.
    fingerprint = sm.infer_event_fingerprint(
        "Стрельба в голландской деревне, есть погибший и пострадавшие",
        "",
        "Инцидент произошел в ночь на 1 сентября. Раненых сотрудников "
        "доставили в больницу, их жизни ничего не угрожает. После "
        "случившегося в Оверасселте и его окрестностях началась "
        "масштабная полицейская операция.",
    )
    assert fingerprint.object_key != "greenery"


def test_greenery_object_no_longer_fires_on_derevnya_declensions():
    for text in (
        "Приехали в деревню на выходные",
        "Между деревней и городом построили дорогу",
        "Разговор с жителями деревень области",
    ):
        fingerprint = sm.infer_event_fingerprint(text, "", text)
        assert fingerprint.object_key != "greenery", text


def test_greenery_object_still_fires_on_genuine_tree_stories():
    for text in (
        "Спилили аварийные деревья у дома",
        "Старое дерево упало на провода",
        "Деревянный дом сгорел дотла",
    ):
        fingerprint = sm.infer_event_fingerprint(text, "", text)
        assert fingerprint.object_key == "greenery", text


def test_queue_delay_no_longer_fires_on_detained_a_person_real_text():
    # Exact real text from report-35 (2026-09-01), Виртуальный Брест.
    fingerprint = sm.infer_event_fingerprint(
        "Стрельба в голландской деревне, есть погибший и пострадавшие",
        "",
        "Правоохранители задержали несколько человек. Полиция проводит "
        "обыски, опрашивает жителей и собирает улики.",
    )
    assert fingerprint.problem_key != "queue_delay"


def test_queue_delay_no_longer_fires_on_mass_detention():
    # The Rossony family-gathering mass-detention story reused elsewhere
    # in the test suite for an unrelated (HTML extraction) purpose --
    # confirming this word shape is a real, recurring one in this
    # monitor's actual content, not a one-off.
    fingerprint = sm.infer_event_fingerprint(
        "Под Россонами задержали около 350 участников семейного слёта",
        "",
        "Около 350 человек, включая семьи с детьми, задержали на "
        "берегу озера Белое возле деревни Межно в Россонском районе.",
    )
    assert fingerprint.problem_key != "queue_delay"


def test_queue_delay_still_fires_on_genuine_delay_complaints():
    for text in (
        "Пенсионеры жалуются на задержку пенсии",
        "Работники сообщили о задержке выплат",
        "Жители жалуются на задержку автобуса",
    ):
        fingerprint = sm.infer_event_fingerprint(text, "", text)
        assert fingerprint.problem_key == "queue_delay", text
