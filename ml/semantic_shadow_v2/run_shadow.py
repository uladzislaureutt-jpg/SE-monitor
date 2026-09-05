#!/usr/bin/env python3
"""Run the advisory semantic layer on an archived regex decision file.

This program is deliberately outside ``social_monitor.py``.  Its output is
an internal CSV only: it cannot affect a report, a notification or state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


MODEL_ID = "MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli"
SCHEMA_VERSION = "semantic-shadow-v2.0"

# These are the two positive hypotheses used to form the diagnostic keep
# score. The remaining hypotheses intentionally represent non-inclusion
# genres, so the score is the sum of the two positive probabilities.
HYPOTHESES = [
    "В тексте описана конкретная жалоба жителей Беларуси на социальную услугу, инфраструктуру или условия жизни, включая ЖКХ, транспорт, медицину, мобильную связь или телевидение.",
    "В тексте описана подтверждённая социально-экономическая проблема в Беларуси, затрагивающая жителей, работников или потребителей.",
    "Это нейтральный анонс, совет, прогноз, справочный или позитивный материал без конкретной жалобы и без нарушения.",
    "Это единичное происшествие или правонарушение без признаков устойчивой социальной проблемы.",
    "Материал касается другой страны или не относится к внутренней социально-экономической проблеме Беларуси.",
    "Это рекламный, развлекательный или исторический материал без актуальной проблемы услуги.",
]

# These bands are deliberately used only to order manual review. Before any
# action can be proposed, their calibration must be confirmed prospectively.
VETO_CANDIDATE_BELOW = 0.23
RESCUE_CANDIDATE_ABOVE = 0.67


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--max-chars", type=int, default=6000)
    parser.add_argument("--limit", type=int, default=0, help="0 means all rows")
    return parser.parse_args()


def make_priority(regex_decision: str, keep_score: float) -> str:
    if regex_decision == "KEEP" and keep_score < VETO_CANDIDATE_BELOW:
        return "VETO_CANDIDATE_REVIEW"
    if regex_decision == "REJECT" and keep_score > RESCUE_CANDIDATE_ABOVE:
        return "RESCUE_CANDIDATE_REVIEW"
    return "OBSERVE_ONLY"


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        raise SystemExit(f"Input not found: {args.input}")

    # Import only after argument validation: unit tests and the regex monitor
    # must not need the heavyweight runtime.
    from transformers import pipeline

    classifier = pipeline("zero-shot-classification", model=args.model, device=-1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.input.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    if args.limit:
        rows = rows[: args.limit]

    fields = [
        "shadow_schema_version", "model_id", "url", "source", "title",
        "regex_relevance_decision", "semantic_text_sha256", "keep_score",
        "shadow_priority", "output_is_advisory", "input_chars",
    ]
    counts: dict[str, int] = {}
    with args.output.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            text = (row.get("semantic_model_text") or "")[: args.max_chars]
            result = classifier(text, HYPOTHESES, multi_label=True)
            score_by_label = dict(zip(result["labels"], result["scores"]))
            keep_score = sum(float(score_by_label.get(label, 0.0)) for label in HYPOTHESES[:2])
            regex_decision = row.get("regex_relevance_decision", "")
            priority = make_priority(regex_decision, keep_score)
            counts[priority] = counts.get(priority, 0) + 1
            writer.writerow({
                "shadow_schema_version": SCHEMA_VERSION,
                "model_id": args.model,
                "url": row.get("url", ""),
                "source": row.get("source", ""),
                "title": row.get("title", ""),
                "regex_relevance_decision": regex_decision,
                "semantic_text_sha256": row.get("semantic_text_sha256") or hashlib.sha256(text.encode()).hexdigest(),
                "keep_score": f"{keep_score:.6f}",
                "shadow_priority": priority,
                "output_is_advisory": "true",
                "input_chars": len(text),
            })
            if index % 25 == 0:
                print(f"Scored {index}/{len(rows)}")

    print(json.dumps({"input_rows": len(rows), "priorities": counts, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
