#!/usr/bin/env python3
"""Apply predeclared shadow gates and create one comparison summary."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--results", type=Path, required=True); args = parser.parse_args()
    reports = []
    for path in sorted(args.results.glob("*_metrics.json")):
        if path.name == "comparison_metrics.json": continue
        try: reports.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception: continue
    candidates = []
    for report in reports:
        g = report["gold"]; base = g["baseline_weighted"]; cascade = g["cascade_weighted"]; actions = g["actions"]
        gates = {
            "net_error_reduction_at_least_10": g["net_error_reduction"] >= 10,
            "weighted_precision_gain_at_least_0_08": cascade["keep_precision"] >= base["keep_precision"] + 0.08,
            "weighted_recall_loss_at_most_0_02": cascade["keep_recall"] >= base["keep_recall"] - 0.02,
            "introduced_errors_at_most_5": g["introduced_errors"] <= 5,
            "veto_precision_at_least_0_80": actions["veto"]["precision"] is not None and actions["veto"]["precision"] >= 0.80,
            "rescue_precision_at_least_0_70": actions["rescue"]["precision"] is not None and actions["rescue"]["precision"] >= 0.70,
        }
        gates["passed_all"] = all(gates.values())
        candidates.append({"candidate": report["candidate"], "model_id": report["model_id"], "gates": gates, "gold": g, "threshold_selection": report["threshold_selection"]})
    eligible = [x for x in candidates if x["gates"]["passed_all"]]
    rank = sorted(eligible, key=lambda x: (x["gold"]["net_error_reduction"], x["gold"]["cascade_weighted"]["keep_precision"], x["gold"]["cascade_weighted"]["keep_recall"]), reverse=True)
    decision = "SHADOW_ONLY_CANDIDATE" if rank else "NO_CANDIDATE_PASSED"
    result = {"experiment": "semantic cascade blind evaluation 1.0", "production_changed": False, "decision": decision, "selected_for_shadow": rank[0]["candidate"] if rank else None, "selection_policy": "predeclared gates, then net error reduction, weighted precision, weighted recall", "candidates": candidates, "failures_possible": len(reports) < 3}
    (args.results / "comparison_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = ["candidate", "passed_all", "fixed_errors", "introduced_errors", "net_error_reduction", "veto_count", "veto_precision", "rescue_count", "rescue_precision", "weighted_precision", "weighted_recall", "weighted_balanced_accuracy"]
    with (args.results / "comparison_summary.csv").open("w", encoding="utf-8", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=fields); w.writeheader()
        for x in candidates:
            g=x["gold"]; w.writerow({"candidate":x["candidate"],"passed_all":x["gates"]["passed_all"],"fixed_errors":g["fixed_errors"],"introduced_errors":g["introduced_errors"],"net_error_reduction":g["net_error_reduction"],"veto_count":g["actions"]["veto"]["count"],"veto_precision":g["actions"]["veto"]["precision"],"rescue_count":g["actions"]["rescue"]["count"],"rescue_precision":g["actions"]["rescue"]["precision"],"weighted_precision":g["cascade_weighted"]["keep_precision"],"weighted_recall":g["cascade_weighted"]["keep_recall"],"weighted_balanced_accuracy":g["cascade_weighted"]["balanced_accuracy"]})
    (args.results / "RUN_SUMMARY.txt").write_text(f"Semantic cascade blind evaluation 1.0\nCandidates completed: {len(reports)}/3\nDecision: {decision}\nSelected for shadow: {result['selected_for_shadow'] or 'none'}\nProduction monitoring was not changed.\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if len(reports) == 3 else 2


if __name__ == "__main__":
    raise SystemExit(main())
