from pathlib import Path

import social_monitor


SETTINGS = social_monitor.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source() -> social_monitor.Source:
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
        language="ru",
        adapter="standard",
    )


def decision(title: str, text: str):
    return social_monitor.evaluate_relevance(title, "", text, source(), SETTINGS)


def article(name: str, title: str, excerpt: str, url: str):
    fingerprint = social_monitor.infer_event_fingerprint(title, "", excerpt)
    return social_monitor.ArticleResult(
        source_name=name,
        source_type="website",
        country="Беларусь",
        locality="Барановичи",
        priority="A",
        source_language="ru",
        title=title,
        title_generated=False,
        url=url,
        published_at="2026-08-31T12:00:00+00:00",
        category="Законы, права и общественное регулирование",
        subcategory="",
        excerpt=excerpt,
        signal_type="общественная реакция на закон или проект правил",
        official_response=False,
        score=10,
        matched_terms="",
        discovered_via="feed:test",
        text_length=len(excerpt),
        event_region=fingerprint.region,
        event_locality=fingerprint.locality,
        event_object=fingerprint.object_label,
        event_problem=fingerprint.problem_label,
        event_signature=fingerprint.signature,
    )


def test_update74_rejects_confirmed_positive_opening_and_credit_statistic():
    underpass = decision(
        "В Могилеве открывается новая подземка на проспекте Мира — посмотрели, как она выглядит",
        "Объект прошёл приёмку и начнёт функционировать завтра. У подземного "
        "перехода три входа и лифты.",
    )
    credit_debt = decision(
        "Просроченная задолженность госсектора по кредитам в годовом измерении выросла в 4,4 раза",
        "На 1 августа задолженность госсектора по кредитам составила 33 млрд рублей. "
        "Просроченная задолженность выросла в 4,4 раза.",
    )
    assert not underpass.relevant, underpass.reason
    assert not credit_debt.relevant, credit_debt.reason


def test_update74_consolidates_baranavichy_school_appearance_rule_resonance():
    rows = [
        article(
            "Onlíner",
            "Барановичская школа показала запрещенные для учеников прически — в соцсетях возмущаются",
            "Школа №1 в Барановичах опубликовала требования к внешнему виду учеников. "
            "Ограничения на прически вызвали активное обсуждение в соцсетях.",
            "https://a.example/school-rules",
        ),
        article(
            "Zerkalo.io",
            "В барановичской средней школе №1 имени Сергея Грицевца опубликовали требования к внешнему виду учеников.",
            "Требования к внешнему виду учеников вызвали активное обсуждение в Threads.",
            "https://b.example/school-rules",
        ),
    ]
    assert {row.event_signature for row in rows} == {
        "барановичи|school_appearance_rules|public_resonance"
    }
    consolidated = social_monitor.deduplicate_results(rows)
    assert len(consolidated) == 1
    assert social_monitor.represented_publication_count(consolidated) == 2
