"""Result Event Integrity 1.12 — 2026-08-29 report-21/debug-21 follow-up.

The vkurier.by protected+chromium profile added in Result Event Integrity
1.10 was a no-op in production: both chromium-trigger sites inside
extract_article() were hard-restricted to
`candidate.source.adapter == "belsat_article"`, ignoring transport_order
entirely. report-21 confirmed this directly — a real (non-deferred)
extraction attempt happened (processed=26, recovery_retried=26) and still
extraction_failed=26/26, with chromium_attempts=0. This round removes the
adapter restriction; the gate is now transport_order alone, which is the
config vkurier.by (and any future source with the same needs) actually
controls.
"""
from pathlib import Path

import social_monitor


SETTINGS = social_monitor.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def _extract_article_source() -> str:
    path = Path(social_monitor.__file__)
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("def extract_article("))
    end = next(
        i for i, line in enumerate(lines[start + 5:], start + 5)
        if line.startswith("def ")
    )
    return "\n".join(lines[start:end])


def test_chromium_trigger_no_longer_hard_restricted_to_belsat_adapter():
    # The exact bug: both trigger conditions used to AND in
    # `candidate.source.adapter == "belsat_article"`, so no non-Belsat
    # source's transport_order could ever actually invoke chromium.
    body = _extract_article_source()
    assert 'candidate.source.adapter == "belsat_article"' not in body
    # The real gate must still be transport_order-driven.
    assert body.count('"chromium" in transport_order') >= 2


def test_vkurier_profile_now_has_a_working_chromium_gate():
    src = social_monitor.Source(
        enabled=True, country="Беларусь", country_code="BY-VI",
        locality="Витебск", rank=1, priority="A", name="Витебский курьер",
        media_type="website", domain="vkurier.by",
        start_url="https://vkurier.by/", language="ru",
        adapter="numeric_articles",
    )
    profile = social_monitor.effective_source_profile(src, SETTINGS)
    assert "chromium" in profile["transport_order"]
    # This is exactly the source/adapter combination report-21 showed
    # chromium never firing for (0 attempts across 26/26 failures).
    assert src.adapter != "belsat_article"


def test_belsat_chromium_behaviour_is_unaffected():
    # Belsat must keep working exactly as before — the fix only widens the
    # gate, it does not change what belsat.eu itself is configured with.
    src = social_monitor.Source(
        enabled=True, country="Беларусь", country_code="BY-HM",
        locality="Беларусь", rank=1, priority="A", name="Белсат",
        media_type="website", domain="belsat.eu",
        start_url="https://belsat.eu/", language="ru",
        adapter="belsat_article",
    )
    profile = social_monitor.effective_source_profile(src, SETTINGS)
    assert "chromium" in profile["transport_order"]
    assert profile["protected"] is True
