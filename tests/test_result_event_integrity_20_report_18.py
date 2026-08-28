"""Result Event Integrity 1.10 — 2026-08-28 report-18/debug-18 follow-up:

Discovery for vkurier.by now works (Result Event Integrity 1.9 fixed
is_probable_article_url), but the very next dry-run showed all 26 newly
discovered candidates failing extraction (access_status "extraction_blind").
This round:

1. Adds html_length to ArticleExtraction / CandidateProcessingTelemetry so a
   future run can tell "page had no usable body" apart from "our extraction
   logic missed a normal page" — the debug artifact had no way to
   distinguish these before.
2. Extends write_rejected_signals_csv() to also capture extraction failures
   (final_stage "degraded_queued", degraded_reason "extraction_failed"),
   not just relevance-stage rejections — vkurier.by produced zero debug
   rows on 2026-08-28 despite 26/26 extraction failures, because that
   final_stage wasn't covered.
3. Adds a vkurier.by STRATEGIC_SOURCE_PROFILES entry (protected=True,
   chromium transport fallback, prefer_largest_container) as a low-risk
   hedge, matching the belsat.eu pattern already used for the same class of
   Belarusian independent outlet, without touching the discovery config
   (feeds/sitemaps/homepage) that Result Event Integrity 1.9 just fixed.
"""
from pathlib import Path
import tempfile

import social_monitor


SETTINGS = social_monitor.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def vkurier_source() -> social_monitor.Source:
    return social_monitor.Source(
        enabled=True, country="Беларусь", country_code="BY-VI",
        locality="Витебск", rank=1, priority="A", name="Витебский курьер",
        media_type="website", domain="vkurier.by",
        start_url="https://vkurier.by/", language="ru",
        adapter="numeric_articles",
        feed_url="https://vkurier.by/news/feed",
        sitemap_url="https://vkurier.by/sitemap.xml",
    )


def test_vkurier_profile_hedges_transport_without_touching_discovery():
    profile = social_monitor.effective_source_profile(vkurier_source(), SETTINGS)
    assert profile["protected"] is True
    assert profile["transport_order"] == ["requests", "chromium"]
    assert profile["prefer_largest_container"] is True
    # Result Event Integrity 1.9 fixed discovery via is_probable_article_url;
    # this round must not touch feeds/sitemaps/homepage behaviour.
    assert profile["feeds"] == ["https://vkurier.by/news/feed"]
    assert profile["sitemaps"] == ["https://vkurier.by/sitemap.xml"]
    assert profile["skip_homepage"] is False
    assert profile["exact_discovery"] is False


def test_write_rejected_signals_csv_now_captures_extraction_failures():
    candidate = social_monitor.Candidate(
        source=vkurier_source(),
        url="https://vkurier.by/238683",
        title="Александр Субботин заявил о недоверии к сертификатам в ЕАЭС",
        discovered_via="homepage",
    )
    # 2026-08-28 report-18 shape: strong prefilter signal, fetch succeeded
    # (large html_length), but no strategy extracted article text.
    trace = social_monitor.CandidateProcessingTelemetry(
        prefilter_status="strong",
        final_stage="degraded_queued",
        degraded_reason="extraction_failed",
        html_length=48213,
        text_length=0,
    )
    # A weak-signal extraction failure must still be excluded, same as
    # before for relevance-stage rejections.
    weak_candidate = social_monitor.Candidate(
        source=vkurier_source(),
        url="https://vkurier.by/238999",
        title="Прогноз погоды на выходные",
        discovered_via="homepage",
    )
    weak_trace = social_monitor.CandidateProcessingTelemetry(
        prefilter_status="needs_text",
        final_stage="degraded_queued",
        degraded_reason="extraction_failed",
        html_length=40000,
        text_length=0,
    )
    # A transport failure (not an extraction failure) must still be
    # excluded — different cause, different fix.
    transport_candidate = social_monitor.Candidate(
        source=vkurier_source(),
        url="https://vkurier.by/238700",
        title="Сильный сигнал, но сайт не ответил",
        discovered_via="homepage",
    )
    transport_trace = social_monitor.CandidateProcessingTelemetry(
        prefilter_status="strong",
        final_stage="degraded_queued",
        degraded_reason="transport_failed",
        html_length=0,
        text_length=0,
    )
    outcomes = {
        "https://vkurier.by/238683": (candidate, trace),
        "https://vkurier.by/238999": (weak_candidate, weak_trace),
        "https://vkurier.by/238700": (transport_candidate, transport_trace),
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "debug" / "rejected_signals_test.csv"
        social_monitor.write_rejected_signals_csv(path, outcomes)
        content = path.read_text(encoding="utf-8-sig")

    assert "238683" in content
    assert "html fetched, no article text extracted" in content
    assert "48213" in content
    assert "238999" not in content
    assert "238700" not in content


def test_write_rejected_signals_csv_still_covers_relevance_rejections():
    # Result Event Integrity 1.9 behaviour (relevance-stage rejections)
    # must be unaffected by the new extraction-failure branch.
    candidate = social_monitor.Candidate(
        source=vkurier_source(),
        url="https://vkurier.by/238410",
        title="Материал без связки темы и проблемы",
        discovered_via="homepage",
    )
    trace = social_monitor.CandidateProcessingTelemetry(
        prefilter_status="strong",
        final_stage="relevance_rejected",
        rejection_reason="нет связки социальной темы и проблемы",
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "debug" / "rejected_signals_test.csv"
        social_monitor.write_rejected_signals_csv(path, {"u": (candidate, trace)})
        content = path.read_text(encoding="utf-8-sig")
    assert "нет связки социальной темы и проблемы" in content
