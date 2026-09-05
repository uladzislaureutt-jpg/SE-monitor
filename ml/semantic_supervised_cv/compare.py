#!/usr/bin/env python3
"""Make one conservative, human-readable summary of completed candidates."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    entries = []
    for path in sorted(args.results.glob("*_metrics.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("experiment") != "semantic supervised grouped cross-validation 1.0":
            continue
        gates = item["gates"]
        entries.append({
            "candidate": item["candidate"],
            "passed_all": gates["passed_all"],
            "net_error_reduction": item["net_error_reduction"],
            "introduced_errors": item["introduced_errors"],
            "weighted_keep_precision": item["cascade_weighted"]["keep_precision"],
            "weighted_keep_recall": item["cascade_weighted"]["keep_recall"],
            "veto_precision": item["actions"]["veto"]["precision"],
            "rescue_precision": item["actions"]["rescue"]["precision"],
        })
    decision = "NO_CANDIDATE_READY_FOR_PROSPECTIVE_SHADOW"
    if any(entry["passed_all"] for entry in entries):
        decision = "CANDIDATE_MAY_ENTER_PROSPECTIVE_SHADOW_ONLY"
    report = {
        "experiment": "semantic supervised grouped cross-validation 1.0",
        "completed_candidates": len(entries),
        "decision": decision,
        "production_changed": False,
        "next_step": "A fresh, previously unused prospective sample is required; no report decision is changed.",
        "candidates": entries,
    }
    args.results.mkdir(parents=True, exist_ok=True)
    (args.results / "comparison_metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.results / "comparison_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(entries[0]) if entries else ["candidate"])
        writer.writeheader(); writer.writerows(entries)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
