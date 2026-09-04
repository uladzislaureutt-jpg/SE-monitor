import csv
import hashlib
from pathlib import Path

import social_monitor as monitor


def _source() -> monitor.Source:
    return monitor.Source(
        enabled=True,
        country="Минск",
        country_code="BY",
        locality="Минск",
        rank=1,
        priority="A",
        name="Test source",
        media_type="online",
        domain="example.by",
        start_url="https://example.by/",
        language="ru",
    )


def _candidate(url: str, title: str) -> monitor.Candidate:
    return monitor.Candidate(
        source=_source(),
        url=url,
        title=title,
        summary="Краткое описание",
        published_at="2026-09-03T10:00:00+00:00",
        discovered_via="feed",
    )


def test_archive_text_is_bounded_and_hashes_full_normalized_input():
    full = "TITLE: Заголовок\nSUMMARY: Краткое описание\nTEXT: " + ("слово " * 100)
    text, digest, truncated = monitor.build_semantic_archive_text(
        "  Заголовок  ", "Краткое   описание", "слово " * 100, max_chars=100
    )
    assert len(text) <= 100
    assert truncated is True
    assert digest == hashlib.sha256(full.strip().encode("utf-8")).hexdigest()


def test_writer_captures_both_regex_routes_but_not_technical_failures(tmp_path: Path):
    keep = monitor.CandidateProcessingTelemetry(
        relevance_passed=True,
        final_stage="included",
        semantic_contract_version="1.0",
        semantic_model_title="Жалобы жильцов",
        semantic_model_text="TITLE: Жалобы жильцов\nTEXT: Текст",
        semantic_text_sha256="a" * 64,
        text_length=100,
    )
    reject = monitor.CandidateProcessingTelemetry(
        prefilter_status="needs_text",
        relevance_passed=False,
        final_stage="relevance_rejected",
        rejection_reason="insufficient problem signal",
        semantic_contract_version="1.0",
        semantic_model_title="Объяснение правил",
        semantic_model_text="TITLE: Объяснение правил\nTEXT: Текст",
        semantic_text_sha256="b" * 64,
        text_length=120,
    )
    degraded = monitor.CandidateProcessingTelemetry(
        final_stage="degraded_queued",
        degraded_reason="extraction_failed",
    )
    outcomes = {
        "keep": (_candidate("https://example.by/keep", "Жалобы жильцов"), keep),
        "reject": (_candidate("https://example.by/reject", "Объяснение правил"), reject),
        "degraded": (_candidate("https://example.by/fail", "Ошибка"), degraded),
    }
    target = tmp_path / "semantic.csv"
    monitor.write_semantic_training_signals_csv(target, outcomes)
    rows = list(csv.DictReader(target.open(encoding="utf-8-sig")))
    assert {row["regex_relevance_decision"] for row in rows} == {"KEEP", "REJECT"}
    assert {row["url"] for row in rows} == {
        "https://example.by/keep",
        "https://example.by/reject",
    }
    assert next(row for row in rows if row["url"].endswith("/reject"))["prefilter_status"] == "needs_text"


def test_semantic_contract_does_not_replace_legacy_rejected_debug(tmp_path: Path):
    weak_reject = monitor.CandidateProcessingTelemetry(
        prefilter_status="needs_text",
        final_stage="relevance_rejected",
        rejection_reason="weak",
    )
    outcomes = {"x": (_candidate("https://example.by/x", "X"), weak_reject)}
    target = tmp_path / "legacy.csv"
    monitor.write_rejected_signals_csv(target, outcomes)
    assert list(csv.DictReader(target.open(encoding="utf-8-sig"))) == []


def test_build_and_contract_versions_are_explicit():
    assert monitor.MONITOR_BUILD == "2026-09-03.social.77-semantic-data-contract-1.0"
    assert monitor.ARCHITECTURE_CORE_VERSION == "3.6"
    assert monitor.SEMANTIC_DATA_CONTRACT_VERSION == "1.0"
