"""Result Event Integrity 1.8 — regression tests for the 2026-08-27 report-16
follow-up: polarity-aware positive-comparison rejection, a national-scale
event_signature fallback (object+problem without a mandatory locality), an
internal rejected-signals debug artifact, and cross-run title stability.
"""
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


# --- Item 1: polarity check on the positive-income-comparison rejection ---

def test_positive_income_comparison_with_reversal_still_rejected():
    # media-polesye.com, 2026-08-27: salaries grew faster than inflation.
    # Before this fix, the "institutional finding" override let it back in
    # despite matching the positive_income_comparison genre pattern.
    title = (
        "Стали ли полешуки жить лучше? Сравнили рост зарплат и инфляцию "
        "за два года"
    )
    text = (
        "За последние два года зарплаты жителей Полесья заметно выросли. "
        "Самый большой прирост средней начисленной зарплаты за два года "
        "показал Ивановский район. При этом за два года общий рост "
        "потребительских цен оказался значительно ниже роста зарплат. "
        "Это означает, что в среднем покупательная способность зарплаты "
        "увеличилась. Даже с учетом накопленного роста цен покупательная "
        "способность этой зарплаты стала выше."
    )
    result = decision(title, text)
    assert result.relevant is False
    assert "положительная динамика доходов" in result.reason


def test_genuine_real_income_decline_still_passes():
    # A structurally similar lead (comparison of wages and prices) but with
    # an unreversed negative outcome must not be caught by the same guard.
    title = "Реальные доходы жителей района снизились из-за роста цен"
    text = (
        "Зарплаты жителей района практически не изменились, при этом цены "
        "выросли значительно сильнее. Жители жалуются, что денег стало не "
        "хватать на привычные покупки. Покупательная способность зарплаты "
        "снизилась за последний год."
    )
    result = decision(title, text)
    assert result.reason != (
        "Result Integrity: положительная динамика доходов без "
        "подтверждённой проблемы"
    )


def test_has_unreversed_negative_outcome_helper():
    positive = (
        "рост потребительских цен оказался значительно ниже роста "
        "зарплат, покупательная способность зарплаты увеличилась"
    )
    negative = "реальные доходы снизились, людям не хватает денег"
    neutral = "открылся новый торговый центр в центре города"
    assert social_monitor.has_unreversed_negative_outcome(positive) is False
    assert social_monitor.has_unreversed_negative_outcome(negative) is True
    assert social_monitor.has_unreversed_negative_outcome(neutral) is False


# --- Item 3: event_signature national fallback ---

def test_national_scope_fallback_builds_signature_when_object_and_problem_match():
    fingerprint = social_monitor.infer_event_fingerprint(
        "В Беларуси участились перебои с водоснабжением",
        "",
        "По стране растёт число жалоб на перебои с подачей воды.",
    )
    assert fingerprint.signature == "беларусь|water_supply|outage"


def test_national_fallback_does_not_fire_without_object_or_problem():
    # A national-scale story whose object/problem categories don't exist in
    # EVENT_OBJECT_PATTERNS / EVENT_PROBLEM_PATTERNS must not get a fake
    # signature just because the title says "Беларусь".
    fingerprint = social_monitor.infer_event_fingerprint(
        "Число занятых в экономике Беларуси незначительно сократилось "
        "по итогам июля 2026 года",
        "",
        "Согласно данным Белстата, число занятых в экономике Беларуси "
        "сократилось.",
    )
    assert fingerprint.signature == ""


def test_national_fallback_does_not_paper_over_missing_locality_extraction():
    # smartpress.by, 2026-08-27: same Pripyat low-water event as two other
    # sources, object+problem both resolve, but no locality/region was
    # extracted and the title does not say "Беларусь" — this must stay
    # unsigned rather than be silently promoted to a national scope, which
    # would risk merging it with unrelated Belarus-wide stories instead of
    # the actual Mozyr-anchored duplicates.
    fingerprint = social_monitor.infer_event_fingerprint(
        "Можно добраться пешком до другого берега? Припять обмелела до "
        "антирекордного уровня",
        "",
        "Река обмелела настолько, что местами её можно перейти вброд.",
    )
    assert fingerprint.object_key == "natural_water"
    assert fingerprint.problem_key == "low_water"
    assert fingerprint.signature == ""


def test_explicit_locality_still_takes_priority_over_national_fallback():
    fingerprint = social_monitor.infer_event_fingerprint(
        "Мозырь: Припять обмелела до антирекордного уровня",
        "",
        "Уровень воды в реке Припять возле Мозыря критически обмелел.",
    )
    assert fingerprint.locality == "Мозырь"
    assert fingerprint.signature == "мозырь|natural_water|low_water"


# --- Item 2: rejected-signals debug artifact ---

def test_write_rejected_signals_csv_only_includes_strong_prefilter_rejections(tmp_path):
    candidate_strong = social_monitor.Candidate(
        source=source(),
        url="https://example.by/bus-1",
        title="В Вилейке автобус на маршруте заменили на маленький",
        summary="",
        discovered_via="homepage",
    )
    trace_strong = social_monitor.CandidateProcessingTelemetry(
        prefilter_status="strong",
        final_stage="relevance_rejected",
        rejection_reason="нет связки социальной темы и проблемы",
    )
    candidate_weak = social_monitor.Candidate(
        source=source(),
        url="https://example.by/unrelated",
        title="Открылась новая кофейня",
        summary="",
        discovered_via="homepage",
    )
    trace_weak = social_monitor.CandidateProcessingTelemetry(
        prefilter_status="needs_text",
        final_stage="relevance_rejected",
        rejection_reason="развлекательный или досуговый сюжет",
    )
    candidate_included = social_monitor.Candidate(
        source=source(),
        url="https://example.by/included",
        title="Включённый материал",
        summary="",
        discovered_via="homepage",
    )
    trace_included = social_monitor.CandidateProcessingTelemetry(
        prefilter_status="strong",
        final_stage="included",
    )
    outcomes = {
        "https://example.by/bus-1": (candidate_strong, trace_strong),
        "https://example.by/unrelated": (candidate_weak, trace_weak),
        "https://example.by/included": (candidate_included, trace_included),
    }
    out_path = tmp_path / "debug" / "rejected_signals_2026-08-27.csv"
    social_monitor.write_rejected_signals_csv(out_path, outcomes)
    content = out_path.read_text(encoding="utf-8-sig")
    assert "bus-1" in content
    assert "нет связки социальной темы и проблемы" in content
    assert "unrelated" not in content
    assert "included" not in content


# --- Item 4: title stability across runs ---

def test_prune_state_keeps_title_cache_in_sync_with_seen():
    state = {
        "seen": {
            "https://example.by/kept": {"first_seen": social_monitor.utc_now().isoformat()},
        },
        "title_cache": {
            "https://example.by/kept": "Заголовок остаётся",
            "https://example.by/gone": "Заголовок должен исчезнуть",
        },
    }
    social_monitor.prune_state(state, retain_days=120)
    assert "https://example.by/kept" in state["title_cache"]
    assert "https://example.by/gone" not in state["title_cache"]
