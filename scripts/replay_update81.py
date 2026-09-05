"""Offline relevance replay; never fetches URLs or changes production state.

python scripts/replay_update81.py --input ARCHIVE.csv --output audit.json
Optional --baseline /path/to/previous/social_monitor.py compares identical inputs.
Archived texts may be truncated: this is not a full production-pipeline replay.
"""
import argparse
import collections
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import social_monitor as current


def load_baseline(path):
    spec = importlib.util.spec_from_file_location("replay_baseline", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def replay(args):
    modules = [current]
    if args.baseline:
        modules.insert(0, load_baseline(args.baseline))
    settings = current.load_settings(ROOT / "config/settings.yaml")
    sources = {s.name: s for s in current.load_sources(ROOT / "config/sources.csv")}
    rows = list(csv.DictReader(args.input.open(encoding="utf-8-sig")))
    output, unmatched = [], set()
    for i, row in enumerate(rows, 1):
        parts = {}
        for line in row["semantic_model_text"].splitlines():
            key, sep, value = line.partition(": ")
            if sep and key in {"TITLE", "SUMMARY", "TEXT"}:
                parts[key] = value
        source = sources.get(row["source"])
        if source is None:
            unmatched.add(row["source"])
            source = current.Source(True, "Беларусь", "BY", "Беларусь", 1, "A",
                                    row["source"], "website", "", "", "ru")
        decisions = []
        for module in modules:
            decision = module.evaluate_relevance(
                row["title"], parts.get("SUMMARY", ""), parts.get("TEXT", ""),
                source, settings,
            )
            decisions.append({"decision": "KEEP" if decision.relevant else "REJECT",
                              "reason": decision.reason, "category": decision.category})
        output.append({"url": row["url"], "source": row["source"], "title": row["title"],
                       "archived": row["regex_relevance_decision"],
                       "truncated": row.get("semantic_text_truncated", ""),
                       "before": decisions[0] if len(modules) == 2 else None,
                       "after": decisions[-1]})
        if i % 50 == 0:
            print(f"Replayed {i}/{len(rows)}", flush=True)
    transitions = collections.Counter()
    for row in output:
        before = row["before"]["decision"] if row["before"] else row["archived"]
        transitions[f'{before}->{row["after"]["decision"]}'] += 1
    report = {"input_file": args.input.name,
              "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
              "settings_sha256": hashlib.sha256((ROOT / "config/settings.yaml").read_bytes()).hexdigest(),
              "builds": [m.MONITOR_BUILD for m in modules], "count": len(rows),
              "module_sha256": [hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in modules],
              "unmatched_sources": sorted(unmatched), "transitions": dict(transitions),
              "scope": "Offline relevance only; archived normalized/truncated text; current source config. No collection, deduplication, temporal filtering or semantic model inference.",
              "rows": output}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    replay(parser.parse_args())
