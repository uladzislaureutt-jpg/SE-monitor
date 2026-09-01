"""First batch (10 of 46) of Result Integrity genre-exclusion groups given
Belarusian equivalents, reviewed by a native speaker in conversation rather
than machine-translated. Two transcription mistakes were caught while
verifying these against synthetic text before landing: "ў" written where
Belarusian orthography requires "у" (the sandhi rule is that "ў" only
follows a vowel-ending word; "накіроўваць"/"уступаюць" end in a consonant
sound, so the following word must take "у") -- a reminder that even with a
native-speaker-provided phrase, the regex encoding step can introduce its
own errors and needs the same test-before-trust treatment as the original
Russian patterns.

These test at the RESULT_INTEGRITY_GENRE_PATTERNS level directly rather
than through evaluate_relevance end-to-end: reaching this genre-exclusion
check requires first satisfying an earlier "does this look like a bounded
social complaint" gate that is orthogonal to what's being verified here
(the language-coverage of the exclusion phrase itself), and synthetic text
built solely to satisfy that earlier gate would not be representative of
the real advice/explainer genre content these patterns target.
"""

import social_monitor as sm


def _matches(group: str, text: str) -> bool:
    patterns = sm.RESULT_INTEGRITY_GENRE_PATTERNS[group]
    folded = sm.normalized_search_text(sm.repair_mojibake(text))
    return any(p.search(folded) for p in patterns)


def test_medical_or_psychology_advice_belarusian():
    assert _matches(
        "medical_or_psychology_advice",
        "Псіхолаг патлумачыў, чаму дзецям складана вяртацца ў школу",
    )
    assert _matches(
        "medical_or_psychology_advice",
        "Як распазнаць сімптомы стомленасці ў дзіцяці",
    )
    assert _matches(
        "medical_or_psychology_advice",
        "Галаўны боль часта звязаны з магнітнай бурай, кажуць урачы",
    )


def test_lifestyle_advice_or_ranking_belarusian():
    assert _matches(
        "lifestyle_advice_or_ranking",
        "Што класці дзіцяці для перакусу ў школу: 5 варыянтаў",
    )
    assert _matches(
        "lifestyle_advice_or_ranking",
        "Схаваныя сімптомы дэфіцыту магнію, якія лёгка прапусціць",
    )
    assert _matches("lifestyle_advice_or_ranking", "Топ-10 вакансій тыдня ў Мінску")


def test_educational_simulation_belarusian():
    assert _matches(
        "educational_simulation",
        "У ліцэі навучаюць працаваць з разумным лічыльнікам вады",
    )
    assert _matches(
        "educational_simulation",
        "На вучэбным стэндзе прайшло мадэляванне аварыйнай сітуацыі",
    )


def test_positive_local_update_belarusian():
    # Covers both the nominative-derived and genitive-plural stem of
    # "Дажынкі" ("да Дажынак" / "Дажынкаў") -- Belarusian has a
    # stem-alternating declension here that a single suffix class can't
    # bridge, which is why the root is "дажын" rather than "дажынк".
    assert _matches(
        "positive_local_update", "Гатоўнасць раёна да Дажынак праверылі кіраўнікі"
    )
    assert _matches(
        "positive_local_update", "Падрыхтоўка да Дажынкаў ідзе паводле графіка"
    )


def test_personal_foreign_profile_belarusian():
    assert _matches(
        "personal_foreign_profile",
        "Самая прыгожая краіна ў свеце, дзе я пабываў, кажа беларус",
    )
    assert _matches(
        "personal_foreign_profile",
        "Адпачынак у Чылі і праца ў ІТ здалёк — гісторыя беларуса",
    )


def test_benefit_or_application_explainer_belarusian():
    assert _matches(
        "benefit_or_application_explainer",
        "Калі і куды звяртацца па дапамогу расказалі ў міністэрстве",
    )
    assert _matches(
        "benefit_or_application_explainer",
        "Хто можа атрымаць дапамогу і ільготу на дзяцей",
    )
    assert _matches(
        "benefit_or_application_explainer",
        "Каму належыць дапамога і як яе атрымаць",
    )


def test_scheduled_service_notice_belarusian():
    assert _matches(
        "scheduled_service_notice", "На якіх вуліцах не будзе вады 2 верасня"
    )
    assert _matches(
        "scheduled_service_notice",
        "Планавае адключэнне электрычнасці закранае некалькі раёнаў",
    )
    assert _matches(
        "scheduled_service_notice",
        "Без вады на час правядзення работ застануцца два двары",
    )


def test_positive_medical_achievement_belarusian():
    assert _matches(
        "positive_medical_achievement",
        "Упершыню ў Беларусі імплантавалі новы тып клапана",
    )
    assert _matches(
        "positive_medical_achievement",
        "Найноўшая методыка дазволіла скараціць час аперацыі",
    )


def test_neutral_service_launch_belarusian():
    assert _matches(
        "neutral_service_launch",
        "У Беларусі запрацуе сэрвіс пошуку рэпетытара анлайн",
    )
    assert _matches(
        "neutral_service_launch",
        "Запуск праекта для рэпетытараў і лагапедаў плануецца ў верасні",
    )


def test_neutral_regulatory_explainer_belarusian():
    # Both cases previously failed with "ў" typed in place of "у" before a
    # consonant-ending preceding word ("накіроўваць у" / "уступаюць у"),
    # caught only by testing against real Belarusian sandhi rather than
    # trusting the regex-authoring step.
    assert _matches(
        "neutral_regulatory_explainer",
        "Каго цяпер будуць накіроўваць у сацыяльныя ўстановы паводле новых правіл",
    )
    assert _matches(
        "neutral_regulatory_explainer",
        "Уступаюць у сілу змены, якія тычацца сацыяльнага пансіянату",
    )


def test_original_russian_branches_unchanged():
    # Spot-check that adding the Belarusian alternatives didn't disturb the
    # existing Russian coverage in the same groups.
    assert _matches(
        "medical_or_psychology_advice",
        "Врач рассказал, как распознать симптомы переутомления у ребенка",
    )
    assert _matches(
        "scheduled_service_notice",
        "На каких улицах не будет воды 2 сентября из-за плановых работ",
    )
    assert _matches(
        "neutral_regulatory_explainer",
        "Кого теперь будут направлять в соцучреждения по новым правилам",
    )
