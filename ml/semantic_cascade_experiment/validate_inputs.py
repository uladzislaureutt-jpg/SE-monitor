#!/usr/bin/env python3
"""Fail closed on changed splits, malformed GOLD, or event leakage."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from pathlib import Path


EXPECTED = {
    "train.csv": ("bf3528d5e797d95eea937edbbe5436acfa8ad6bfa310b01b07fd707f496e036f", 375),
    "validation.csv": ("04595b2fbc2cc31b01890adb8d8580c39af3802ad9e3e4500c1032d204afeebb", 62),
    "test.csv": ("27ae6e155a5d6ccce78aaeb8bec3bd986a916f612965578f0c33aa4649d5c644", 72),
    "model_test_event_safe_239.csv": ("6aa9bb0641cc1b49f8caa13b53458c3f6d38e55f82a765f54d87030742686e7c", 239),
    "blind_gold_final_240.csv": ("f9e35b5b8986b095434b35c17d6059ead21710c509d01a8c7a666c95638b9d33", 240),
    "legacy_overlap_quarantine.csv": ("1d611abfc8a5490d94b19357f7567e750378e759d5b6e22917da2f7793b9c2d5", 1),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def norm(value):
    value = unicodedata.normalize("NFKC", value or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^a-zа-я0-9]+", " ", value).split())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-data", type=Path, required=True)
    parser.add_argument("--gold-data", type=Path, required=True)
    args = parser.parse_args(); errors = []; loaded = {}
    for name, (expected_hash, expected_rows) in EXPECTED.items():
        path = (args.legacy_data if name in {"train.csv", "validation.csv", "test.csv"} else args.gold_data) / name
        if not path.is_file(): errors.append(f"missing {path}"); continue
        if digest(path) != expected_hash: errors.append(f"{name}: SHA-256 mismatch")
        loaded[name] = rows(path)
        if len(loaded[name]) != expected_rows: errors.append(f"{name}: expected {expected_rows}, got {len(loaded[name])}")
    gold = loaded.get("model_test_event_safe_239.csv", [])
    if gold:
        required = {"id", "title", "final_label", "event_group", "regex_decision", "sampling_weight", "review_status"}
        if required - set(gold[0]): errors.append(f"GOLD missing columns: {sorted(required - set(gold[0]))}")
        if {r.get("final_label") for r in gold} - {"KEEP", "REJECT"}: errors.append("GOLD has non-binary labels")
        if {r.get("regex_decision") for r in gold} - {"KEEP", "REJECT"}: errors.append("GOLD has non-binary regex decisions")
        if any(r.get("review_status") != "GOLD_LOCKED" for r in gold): errors.append("GOLD is not locked")
        if len({r.get("id") for r in gold}) != len(gold): errors.append("GOLD duplicate IDs")
        if len({r.get("event_group") for r in gold}) != len(gold): errors.append("GOLD duplicate events")
        legacy = loaded.get("train.csv", []) + loaded.get("validation.csv", []) + loaded.get("test.csv", [])
        legacy_urls = {r.get("url", "").rstrip("/") for r in legacy if r.get("url")}
        legacy_titles = {norm(r.get("title", "")) for r in legacy if norm(r.get("title", ""))}
        overlap = [r.get("id") for r in gold if r.get("url", "").rstrip("/") in legacy_urls or norm(r.get("title", "")) in legacy_titles]
        if overlap: errors.append(f"legacy/GOLD leakage: {overlap}")
    result = {"valid": not errors, "errors": errors, "rows": {k: len(v) for k, v in loaded.items()}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
