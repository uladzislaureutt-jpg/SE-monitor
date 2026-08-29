"""Result Event Integrity 1.11 — rethought the polarity mechanism: instead
of an ever-growing list of exact "positive framing" phrases, a small bounded
vocabulary of DIRECTION markers (increase/decrease, subject-independent) is
combined with a small bounded table of which economic/social SUBJECTS treat
an increase as good news vs a problem. See economic_direction_signal(),
_sentence_direction_polarity(), has_unreversed_negative_outcome().
"""
import social_monitor as sm


def test_new_wordings_not_individually_enumerated_are_still_positive():
    # 2026-08-28 report-20: neither "подскочили на 7,5%" nor "рекордный
    # минимум" appeared in the old fixed phrase list, yet both are handled
    # correctly because polarity is derived compositionally.
    text = (
        "Реальные доходы белорусов подскочили на 7,5%, а безработица "
        "обновила рекордный минимум."
    )
    assert sm.economic_direction_signal(text) is False
    assert sm.has_unreversed_negative_outcome(text) is False


def test_genuine_negative_outcomes_still_detected():
    cases = (
        "Число безработных выросло за последний квартал.",
        "Тарифы на услуги ЖКХ выросли, что вызвало недовольство жителей.",
        "Зарплаты выросли, но безработица тоже выросла.",
    )
    for text in cases:
        assert sm.has_unreversed_negative_outcome(text) is True, text


def test_concessive_clause_does_not_leak_into_the_main_clause():
    # "даже с учётом роста цен покупательная способность стала выше" — the
    # conceded "рост цен" must not register as its own negative verdict.
    text = (
        "Даже с учетом накопленного роста цен покупательная способность "
        "этой зарплаты стала выше."
    )
    assert sm.economic_direction_signal(text) is False


def test_comparative_form_still_resolves_correctly():
    positive = "Рост потребительских цен оказался значительно ниже роста зарплат."
    negative = "Рост потребительских цен оказался значительно выше роста зарплат."
    assert sm.has_unreversed_negative_outcome(positive) is False
    assert sm.has_unreversed_negative_outcome(negative) is True


def test_neutral_text_has_no_signal_either_way():
    assert sm.economic_direction_signal("Открылся новый торговый центр.") is None


def test_genre_rejection_now_triggers_without_the_old_fixed_phrase_list():
    # The gate itself (not just the override check) now also fires from
    # economic_direction_signal() being unambiguously positive, catching
    # wordings the old fixed "positive_income_comparison" regex list never
    # covered.
    title = (
        "Реальные доходы белорусов подскочили на 7,5%, а безработица "
        "обновила рекордный минимум, сообщает ФПБ"
    )
    lead = (
        "В Федерации профсоюзов Беларуси состоялось заседание. "
        "Положительная динамика зафиксирована и в бюджетной сфере."
    )
    reason = sm.result_integrity_genre_rejection(title, lead, lead_findings=True)
    assert "положительная динамика доходов" in reason


def test_genre_rejection_still_lets_genuine_problems_through():
    title = "Реальные доходы жителей района снизились из-за роста цен"
    lead = (
        "Зарплаты жителей района практически не изменились, при этом цены "
        "выросли значительно сильнее. Жители жалуются, что денег стало не "
        "хватать на привычные покупки."
    )
    reason = sm.result_integrity_genre_rejection(title, lead, lead_findings=True)
    assert reason == ""
