"""Third batch (8 more) of Result Integrity genre-exclusion groups given
Belarusian equivalents.

Three of the eight (`instructional`, `single_incident`,
`event_or_figurative_collision`) were given Belarusian phrases describing a
*different, more general* framing than any existing Russian branch in that
group covers -- not a translation of the specific narrow patterns already
there. For `single_incident` and `event_or_figurative_collision`, a Russian
equivalent of the same new general framing was added alongside the
Belarusian one, since neither language had it before.
"""

import social_monitor as sm


def _matches(group: str, text: str) -> bool:
    patterns = sm.RESULT_INTEGRITY_GENRE_PATTERNS[group]
    folded = sm.normalized_search_text(sm.repair_mojibake(text))
    return any(p.search(folded) for p in patterns)


def test_protocol_or_personnel_belarusian():
    assert _matches("protocol_or_personnel", "Ён прызначаны новым дырэктарам завода")
    assert _matches(
        "protocol_or_personnel", "Кандыдат зацверджаны на пасадзе намесніка старшыні"
    )


def test_preventive_service_expansion_belarusian():
    assert _matches(
        "preventive_service_expansion",
        "Вырашылі пашырыць пералік бясплатных паслуг для пенсіянераў",
    )
    assert _matches(
        "preventive_service_expansion",
        "Дададуць новыя катэгорыі грамадзян, якім належыць дапамога",
    )


def test_private_document_story_belarusian():
    assert _matches(
        "private_document_story", "На гарышчы знайшлі стары ліст пачатку стагоддзя"
    )
    assert _matches(
        "private_document_story",
        "Падчас рамонту выпадкова выявілі архіўную знаходку",
    )


def test_editorial_meta_or_denial_belarusian():
    assert _matches(
        "editorial_meta_or_denial", "Рэдакцыя ўдакладняе акалічнасці здарэння"
    )
    assert _matches(
        "editorial_meta_or_denial",
        "Паведамленая інфармацыя не адпавядае рэчаіснасці",
    )
    assert _matches("editorial_meta_or_denial", "Прэс-служба заявіла: гэта няпраўда")


def test_aesthetic_or_symbolic_opinion_belarusian():
    assert _matches(
        "aesthetic_or_symbolic_opinion",
        "Новы плот выглядае непрыгожа, лічаць жыхары",
    )
    assert _matches(
        "aesthetic_or_symbolic_opinion", "Архітэктар прызнаў: гэта проста непрыгожа"
    )


def test_instructional_belarusian_new_framing():
    assert _matches(
        "instructional", "Інструкцыя, як аформіць дакументы самастойна"
    )
    assert _matches("instructional", "Пакрокавае кіраўніцтва для пачаткоўцаў")
    assert _matches("instructional", "Майстар-клас для дзяцей у бібліятэцы")


def test_single_incident_new_general_framing_both_languages():
    for text in (
        "Гэта адзінкавы выпадак, кажуць у міністэрстве",
        "Улады заявілі, што гэта выключэнне з правілаў",
        "Это единичный случай, заявили в министерстве",
        "Власти заявили, что это исключение из правил",
    ):
        assert _matches("single_incident", text), text


def test_event_or_figurative_collision_new_general_framing_both_languages():
    for text in (
        "Сказана было ў пераносным сэнсе, тлумачаць аўтары",
        "Вобразна кажучы, гэта была бура ў шклянцы вады",
        "Это было сказано в переносном смысле",
        "Образно говоря, это буря в стакане воды",
    ):
        assert _matches("event_or_figurative_collision", text), text


def test_original_russian_branches_unchanged_batch3():
    # Spot-check that adding new branches didn't disturb pre-existing
    # coverage in the same groups.
    assert _matches(
        "protocol_or_personnel",
        "Посол аккредитовал нового военного атташе в Минске",
    )
    assert _matches(
        "aesthetic_or_symbolic_opinion",
        "Жителей раздражает обилие странной символики на фасаде",
    )
    assert _matches(
        "single_incident", "Спасатели помогли снять с дерева кота"
    )
