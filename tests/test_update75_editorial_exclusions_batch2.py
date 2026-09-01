"""Two exclusion rules added on explicit editorial decision after reviewing
report-32/33 (2026-09-01):

1. "positive_self_organized_initiative" (new group) -- residents fixing
   something themselves with no complaint about official inaction, framed
   as a feel-good story. Real case: the Krupets well-restoration story.
2. A new branch on the existing "benefit_or_application_explainer" group --
   "Новое в законодательстве о пособиях/льготах..." is a distinct title
   framing (a procedural-update genre) that the three existing branches in
   that group don't cover (they require "when/where to apply" / "who can
   get it" / "who's entitled" framings specifically). Real case: the
   Khoiniki care-benefit legislation update.
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


def _decision(title: str, text: str, language: str = "ru"):
    return sm.evaluate_relevance(title, "", text, _source(language), SETTINGS)


def test_krupets_well_story_excluded_real_text():
    # Exact real text from report-33 (2026-09-01), Gomel Today.
    d = _decision(
        "Не чакаюць дапамогі, а робяць самі: жыхары Крупца аднавілі стары "
        "калодзеж і навялі парадак у аграгарадку",
        "Замест таго, каб скардзіцца, вяскоўцы з Добрушчыны самі ўзяліся "
        "за добраўпарадкаванне сваіх вуліц. Асобны гонар – адноўлены "
        "калодзеж па вуліцы Першамайскай. Каб пачысціць яго на глыбіні "
        "17 метраў, мясцовы жыхар Мікалай Ляшчынскі за ўласныя сродкі "
        "знайшоў і наняў спецыялістаў з Оршы. Акрамя гэтага, у "
        "аграгарадку аднавілі яшчэ адзін калодзеж, расчысцілі дарогу "
        "праз мясцовую рачулку і паставілі самаробныя сметнікі, дзякуючы "
        "чаму на вуліцах пануе ідэальны парадак.",
        language="be",
    )
    assert not d.relevant
    assert d.reason == "Editorial Intent: жители самостоятельно решили вопрос без жалобы"


def test_self_help_pattern_does_not_catch_inaction_complaint():
    # If residents did something themselves BECAUSE officials ignored a
    # complaint, that's still a real problem story and must NOT be caught
    # by the new feel-good-framing exclusion.
    for text in (
        "Не дожидаясь помощи от властей, жители сами подали коллективный "
        "иск в суд",
        "Не дождавшись помощи от коммунальников, жители третий месяц "
        "требуют ремонта колодца",
    ):
        assert not any(
            p.search(sm.normalized_search_text(sm.repair_mojibake(text)))
            for p in sm.RESULT_INTEGRITY_GENRE_PATTERNS[
                "positive_self_organized_initiative"
            ]
        ), text


def test_benefit_legislation_update_excluded_real_text():
    # Exact real text from report-33 (2026-09-01), Хойніцкія навіны.
    d = _decision(
        "Новое в законодательстве о пособиях по уходу",
        "Согласно пункту 22 Положения пособие по уходу данной категории "
        "лиц может выплачиваться до достижения ими возраста 70 лет. "
        "Указанная новация не распространяется на родителей, опекунов, "
        "которые сами являются инвалидами I группы. Вместе с тем "
        "зачастую получатель пособия по уходу в связи с дефицитом "
        "младшего медицинского персонала продолжает осуществлять "
        "постоянный уход за ним и в больнице.",
    )
    assert not d.relevant
    assert d.reason == "Editorial Intent: порядок получения услуги или пособия без жалобы"


def test_benefit_explainer_still_fires_on_original_three_framings():
    # Spot-check the three pre-existing branches in this group still work
    # after adding a fourth.
    assert any(
        p.search(sm.normalized_search_text(sm.repair_mojibake(
            "Когда и куда обращаться за пособием на ребенка"
        )))
        for p in sm.RESULT_INTEGRITY_GENRE_PATTERNS["benefit_or_application_explainer"]
    )
    assert any(
        p.search(sm.normalized_search_text(sm.repair_mojibake(
            "Хто можа палепшыць жыллёвыя ўмовы: тлумачаць юрысты"
        )))
        for p in sm.RESULT_INTEGRITY_GENRE_PATTERNS["benefit_or_application_explainer"]
    )
