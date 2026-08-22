#!/usr/bin/env python3
"""Fail closed when frozen event-safe splits are changed or leak events."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


EXPECTED = {
    "train": {
        "sha256": "bf3528d5e797d95eea937edbbe5436acfa8ad6bfa310b01b07fd707f496e036f",
        "rows": 375,
        "tier": "SILVER",
    },
    "validation": {
        "sha256": "04595b2fbc2cc31b01890adb8d8580c39af3802ad9e3e4500c1032d204afeebb",
        "rows": 62,
        "tier": "SILVER",
    },
    "test": {
        "sha256": "27ae6e155a5d6ccce78aaeb8bec3bd986a916f612965578f0c33aa4649d5c644",
        "rows": 72,
        "tier": "GOLD",
    },
}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    errors: list[str] = []
    rows_by_split: dict[str, list[dict[str, str]]] = {}

    for split, expected in EXPECTED.items():
        path = args.data / f"{split}.csv"
        if not path.is_file():
            errors.append(f"missing {path}")
            continue
        actual_hash = digest(path)
        if actual_hash != expected["sha256"]:
            errors.append(f"{split}: SHA-256 mismatch")
        rows = read_rows(path)
        rows_by_split[split] = rows
        if len(rows) != expected["rows"]:
            errors.append(f"{split}: expected {expected['rows']} rows, got {len(rows)}")
        labels = Counter(row.get("final_label", "") for row in rows)
        if set(labels) - {"KEEP", "REJECT"}:
            errors.append(f"{split}: non-binary labels {dict(labels)}")
        tiers = {row.get("dataset_tier", "") for row in rows}
        if tiers != {expected["tier"]}:
            errors.append(f"{split}: expected tier {expected['tier']}, got {sorted(tiers)}")
        allowed_status = {"GOLD_LOCKED"} if expected["tier"] == "GOLD" else {"REVIEWED"}
        if any(row.get("review_status") not in allowed_status for row in rows):
            errors.append(f"{split}: contains rows outside status {sorted(allowed_status)}")
        if len({row.get("id") for row in rows}) != len(rows):
            errors.append(f"{split}: duplicate IDs")

    if len(rows_by_split) == 3:
        events = {
            split: {row.get("event_group", "") for row in rows}
            for split, rows in rows_by_split.items()
        }
        for left, right in (("train", "validation"), ("train", "test"), ("validation", "test")):
            overlap = events[left] & events[right]
            if overlap:
                errors.append(f"event leakage {left}/{right}: {len(overlap)} groups")

    result = {
        "valid": not errors,
        "errors": errors,
        "splits": {
            split: {
                "rows": len(rows),
                "events": len({row.get("event_group", "") for row in rows}),
                "labels": dict(Counter(row.get("final_label", "") for row in rows)),
            }
            for split, rows in rows_by_split.items()
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
