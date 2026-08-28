"""Result Event Integrity 1.9 — 2026-08-27 report-17 follow-up:

1. debug/ wired into the daily workflow's artifact upload (see
   .github/workflows/daily-social-monitor.yml) so rejected_signals_*.csv
   actually reaches a maintainer instead of being lost on the runner.
2. is_probable_article_url() recognises bare short-numeric CMS article ids
   (vkurier.by: /238683, /238672, /238698), which were silently unreachable
   for 6+ consecutive days despite the source actively publishing, while
   preserving the deliberate nashaniva.com exception documented in
   test_architecture_core32a22_nasha_niva_comments_guard_covers_all_locales.
"""
from pathlib import Path

import social_monitor


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_vkurier_numeric_article_ids_now_admitted():
    domain = "vkurier.by"
    real_articles = (
        "https://vkurier.by/238683",
        "https://vkurier.by/238672",
        "https://vkurier.by/238698",
    )
    for url in real_articles:
        assert social_monitor.is_probable_article_url(url, domain) is True
        assert social_monitor.classify_source_url(url, domain) == "article"


def test_numeric_article_id_rule_stays_bounded():
    domain = "vkurier.by"
    non_articles = {
        "https://vkurier.by/1": "single-digit, pagination-like",
        "https://vkurier.by/12": "two-digit, pagination-like",
        "https://vkurier.by/2024": "four-digit, looks like a year archive",
        "https://vkurier.by/123456789012": "implausibly long, hash/timestamp-like",
    }
    for url, _reason in non_articles.items():
        assert social_monitor.is_probable_article_url(url, domain) is False

    # A numeric id inside a recognised service/rubric path must still be
    # classified structurally, not swept up by the new fast path.
    assert social_monitor.classify_source_url(
        "https://vkurier.by/search?q=238683", domain
    ) == "service"
    assert social_monitor.classify_source_url(
        "https://vkurier.by/tag/238683", domain
    ) == "rubric"


def test_nashaniva_bare_numeric_route_exception_is_preserved():
    # The numeric-id fast path must not override the considered decision to
    # leave nashaniva.com's bare /<id> route "unknown" (duplicate/redirect
    # risk against the canonical /ru/<id> and /be_latn/<id>/ forms).
    domain = "nashaniva.com"
    assert social_monitor.is_probable_article_url(
        "https://nashaniva.com/401729", domain
    ) is False
    assert social_monitor.classify_source_url(
        "https://nashaniva.com/401729", domain
    ) == "unknown"
    # The already-admitted localized forms are unaffected.
    assert social_monitor.is_source_article_url(
        "https://nashaniva.com/ru/401729",
        social_monitor.replace(
            social_monitor.Source(
                enabled=True, country="Беларусь", country_code="BY-HM",
                locality="Беларусь", rank=1, priority="A", name="Наша Ніва",
                media_type="website", domain="nashaniva.com",
                start_url="https://nashaniva.com/", language="ru",
                adapter="standard",
            ),
        ),
    )


def test_daily_workflow_uploads_debug_directory_as_a_separate_artifact():
    # Item 1: the daily workflow only ever uploaded reports/, so the
    # rejected-signals debug CSV (written to debug/, see
    # write_rejected_signals_csv / run_monitor) never reached a maintainer
    # on GitHub Actions even though it was being written on every run.
    workflow_path = (
        REPO_ROOT / ".github" / "workflows" / "daily-social-monitor.yml"
    )
    text = workflow_path.read_text(encoding="utf-8")
    assert "path: reports/" in text
    assert "path: debug/" in text
    # The debug upload must not silently fail the workflow when a run
    # produced no debug output (e.g. every candidate had a weak prefilter).
    debug_section = text[text.index("path: debug/"):]
    assert "if-no-files-found: warn" in debug_section[:400]
