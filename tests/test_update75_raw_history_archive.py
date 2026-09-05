"""P0 hardening (2026-09-03): every daily run's article-level classification
output used to live only in the GitHub Actions artifact (14 days for
reports/, 7 for debug/) with nothing persisted to git — so the raw material
needed to eventually build a new blind GOLD set for
ml/setfit_large_experiment/ (currently AWAIT_NEW_BLIND_GOLD) was silently
lost unless someone manually downloaded and kept the zip. This adds a
workflow step that archives social_articles_*.csv and rejected_signals_*.csv
into data/ml/raw_history/ and commits them alongside state.json.

These are text-level checks against the workflow YAML (following the
existing convention in test_result_event_integrity_19_report_17.py), not
an actual GitHub Actions run, since that isn't feasible to execute here.
"""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "daily-social-monitor.yml"


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _steps() -> list[dict]:
    data = yaml.safe_load(_workflow_text())
    return data["jobs"]["monitor"]["steps"]


def test_archive_step_exists_and_is_gated_like_state_save():
    steps = _steps()
    archive_steps = [s for s in steps if s.get("name") == "Archive raw classification history"]
    assert len(archive_steps) == 1
    archive_step = archive_steps[0]
    save_state_steps = [s for s in steps if s.get("name") == "Save monitoring state"]
    assert len(save_state_steps) == 1
    # Must not run during dry-run, matching the existing "dry-run changes
    # nothing persistent" contract already enforced for state.json/
    # discovery_cache.json.
    assert archive_step["if"] == save_state_steps[0]["if"]
    assert "inputs.dry_run != true" in archive_step["if"]


def test_archive_step_runs_before_state_is_saved():
    steps = _steps()
    names = [s.get("name") for s in steps]
    assert names.index("Archive raw classification history") < names.index(
        "Save monitoring state"
    )


def test_archive_step_copies_articles_rejections_and_semantic_inputs():
    steps = _steps()
    archive_step = next(
        s for s in steps if s.get("name") == "Archive raw classification history"
    )
    run = archive_step["run"]
    assert "reports/social_articles_*.csv" in run
    assert "debug/rejected_signals_*.csv" in run
    assert "debug/semantic_training_signals_*.csv" in run
    assert "data/ml/raw_history/articles" in run
    assert "data/ml/raw_history/rejected" in run
    assert "data/ml/raw_history/semantic_inputs" in run
    # Prefixed with the run number, not just the report date, since
    # multiple runs (manual re-runs, same-day dry-run checks) can share a
    # calendar date and would otherwise overwrite each other's archive.
    assert "GITHUB_RUN_NUMBER" in run
    # The glob-with-no-match guard: bash leaves an unexpanded glob literal
    # in $f when nothing matches, so every copy loop must skip it instead
    # of trying (and failing) to cp a nonexistent literal path.
    assert run.count('[ -e "$f" ] || continue') == 3


def test_save_state_step_now_also_commits_raw_history():
    text = _workflow_text()
    save_state_section = text[text.index("name: Save monitoring state"):]
    git_add_line = next(
        line for line in save_state_section.splitlines() if line.strip().startswith("git add")
    )
    assert "data/state.json" in git_add_line
    assert "data/discovery_cache.json" in git_add_line
    assert "data/ml/raw_history/" in git_add_line


def test_raw_history_readme_documents_silver_not_gold_status():
    readme = (REPO_ROOT / "data" / "ml" / "raw_history" / "README.md").read_text(
        encoding="utf-8"
    )
    # The most important thing this doc must not get wrong: these rows are
    # the rule-based classifier's own output, not independently verified —
    # using them as-is to train/evaluate a model would just teach it the
    # same biases it's meant to help catch.
    assert "SILVER" in readme
    assert "GOLD" in readme
