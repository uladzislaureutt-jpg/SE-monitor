"""Check event identity on archived report cards, enriching from saved inputs.

Not a full production replay: relevance, date and excerpt selection are unchanged.
"""
import argparse
import csv
import dataclasses
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import social_monitor as sm


def run(args):
    inputs = {r["url"]: r for r in csv.DictReader(args.inputs.open(encoding="utf-8-sig"))}
    articles = list(csv.DictReader(args.articles.open(encoding="utf-8-sig")))
    results = []
    for row in articles:
        values = {field.name: row[field.name] for field in dataclasses.fields(sm.ArticleResult)
                  if field.name in row}
        values.update(source_name=row["source"], country=row["region"], excerpt=row["exact_excerpt"])
        for key in ("title_generated", "official_response", "event_echo", "category_bonus_only"):
            values[key] = str(values.get(key, "")).lower() == "true"
        for key in ("score", "text_length"):
            values[key] = int(values[key])
        saved = inputs.get(row["url"], {}).get("semantic_model_text", "")
        parts = dict(line.split(": ", 1) for line in saved.splitlines() if ": " in line)
        if values["title_generated"]:
            values["title"] = sm.repair_generated_title_from_text(values["title"], parts.get("TEXT", ""))
        fp = sm.infer_event_fingerprint(values["title"], parts.get("SUMMARY", ""), parts.get("TEXT", values["excerpt"]))
        values.update(event_region=fp.region, event_locality=fp.locality, event_object=fp.object_label,
                      event_problem=fp.problem_label, event_signature=fp.signature)
        values["event_numeric_anchors"] = sm.event_numeric_anchors_from_text(parts.get("TEXT") or parts.get("SUMMARY", ""))
        results.append(sm.ArticleResult(**values))
    deduped = sm.deduplicate_results(results)
    report = {"build": sm.MONITOR_BUILD,
              "module_sha256": hashlib.sha256(Path(sm.__file__).read_bytes()).hexdigest(),
              "scope": __doc__, "input_cards": len(results), "output_cards": len(deduped),
              "merged": [{"title": r.title, "url": r.url, "signature": r.event_signature,
                          "related": r.related_coverage} for r in deduped if r.related_coverage]}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
