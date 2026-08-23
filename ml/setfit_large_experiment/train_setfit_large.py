#!/usr/bin/env python3
"""Isolated larger-encoder SetFit experiment for S-monitor.

The script is intentionally independent from social_monitor.py. It reads only
the frozen event-safe CSV splits and writes diagnostic artifacts. The trained
weights stay on the ephemeral runner and are not uploaded. No production state,
cache, reports, secrets, delivery or workflow are used.

Important: the historical GOLD split was already used by compact pilot 1.0.
It is therefore a legacy comparison set, not a new blind acceptance test.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from datasets import Dataset
from setfit import SetFitModel, Trainer, TrainingArguments
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)


POSITIVE = "KEEP"


@dataclass(frozen=True)
class Metrics:
    rows: int
    accuracy: float
    balanced_accuracy: float
    keep_precision: float
    keep_recall: float
    keep_f1: float
    tn: int
    fp: int
    fn: int
    tp: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-id", default="intfloat/multilingual-e5-small")
    parser.add_argument(
        "--model-revision",
        default="0e60b8d9d2166d80387f86e3b48ec9ced55f4d15",
    )
    parser.add_argument("--compact-reference", type=Path)
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--num-iterations", type=int, default=5)
    parser.add_argument("--body-epochs", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=384)
    return parser.parse_args()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_split(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str).fillna("")
    required = {
        "id",
        "title",
        "exact_excerpt",
        "final_label",
        "event_group",
        "is_label_representative",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{path}: missing columns: {', '.join(missing)}")
    if not set(frame["final_label"]).issubset({"KEEP", "REJECT"}):
        raise ValueError(f"{path}: hard split contains non-binary labels")
    frame["text"] = (
        frame["title"].str.strip()
        + "\n\n"
        + frame["exact_excerpt"].str.strip()
    ).str.strip()
    if (frame["text"].str.len() == 0).any():
        raise ValueError(f"{path}: empty model text")
    return frame


def label_array(frame: pd.DataFrame) -> np.ndarray:
    return (frame["final_label"].to_numpy() == POSITIVE).astype(int)


def metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Metrics:
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    return Metrics(
        rows=int(len(y_true)),
        accuracy=float(accuracy_score(y_true, y_pred)),
        balanced_accuracy=float(balanced_accuracy_score(y_true, y_pred)),
        keep_precision=float(precision_score(y_true, y_pred, zero_division=0)),
        keep_recall=float(recall_score(y_true, y_pred, zero_division=0)),
        keep_f1=float(f1_score(y_true, y_pred, zero_division=0)),
        tn=int(tn),
        fp=int(fp),
        fn=int(fn),
        tp=int(tp),
    )


def event_view(frame: pd.DataFrame, probabilities: np.ndarray) -> pd.DataFrame:
    work = frame[
        ["event_group", "final_label", "source", "title", "url"]
    ].copy()
    work["keep_probability"] = probabilities
    grouped = work.groupby("event_group", sort=True, as_index=False).agg(
        final_label=("final_label", "first"),
        label_count=("final_label", "nunique"),
        keep_probability=("keep_probability", "mean"),
        rows=("final_label", "size"),
        source=("source", "first"),
        title=("title", "first"),
        url=("url", "first"),
    )
    if (grouped["label_count"] != 1).any():
        raise ValueError("evaluation split contains a mixed-label event")
    return grouped


def exact_mcnemar_p(new_correct: np.ndarray, old_correct: np.ndarray) -> dict[str, float | int]:
    new_only = int(np.sum(new_correct & ~old_correct))
    old_only = int(np.sum(~new_correct & old_correct))
    discordant = new_only + old_only
    if discordant == 0:
        p_value = 1.0
    else:
        tail = min(new_only, old_only)
        probability = sum(math.comb(discordant, k) for k in range(tail + 1)) / (2**discordant)
        p_value = min(1.0, 2.0 * probability)
    return {
        "larger_correct_compact_wrong": new_only,
        "compact_correct_larger_wrong": old_only,
        "discordant_events": discordant,
        "two_sided_exact_p": p_value,
    }


def compare_compact(current: pd.DataFrame, reference_path: Path | None) -> dict[str, object]:
    if reference_path is None or not reference_path.is_file():
        return {"available": False, "reason": "compact event predictions not supplied"}
    reference = pd.read_csv(reference_path, dtype=str).fillna("")
    required = {"event_group", "final_label", "prediction"}
    if not required.issubset(reference.columns):
        return {"available": False, "reason": "compact reference schema mismatch"}
    old = reference[list(required)].rename(columns={"prediction": "compact_prediction"})
    joined = current.merge(old, on=["event_group", "final_label"], how="inner")
    if len(joined) != len(current):
        return {
            "available": False,
            "reason": f"event alignment mismatch: {len(joined)} of {len(current)}",
        }
    new_correct = (joined["prediction"] == joined["final_label"]).to_numpy()
    old_correct = (joined["compact_prediction"] == joined["final_label"]).to_numpy()
    return {
        "available": True,
        "events": int(len(joined)),
        "larger_correct": int(new_correct.sum()),
        "compact_correct": int(old_correct.sum()),
        "net_correct_events": int(new_correct.sum() - old_correct.sum()),
        "mcnemar": exact_mcnemar_p(new_correct, old_correct),
    }


def choose_threshold(frame: pd.DataFrame) -> tuple[float, Metrics, bool]:
    y_true = label_array(frame)
    probability = frame["keep_probability"].to_numpy(dtype=float)
    candidates = sorted(set([0.0, 0.5, 1.0] + probability.tolist()))
    scored = [(t, metrics(y_true, (probability >= t).astype(int))) for t in candidates]
    eligible = [row for row in scored if row[1].keep_recall >= 0.90]
    pool = eligible or scored
    chosen = max(
        pool,
        key=lambda row: (
            row[1].keep_precision,
            row[1].keep_f1,
            row[1].balanced_accuracy,
            row[0],
        ),
    )
    return chosen[0], chosen[1], bool(eligible)


def probabilities(model: SetFitModel, texts: list[str]) -> np.ndarray:
    try:
        raw = model.predict_proba(texts, as_numpy=True)
    except TypeError:
        raw = model.predict_proba(texts)
    if hasattr(raw, "detach"):
        raw = raw.detach().cpu().numpy()
    values = np.asarray(raw)
    if values.ndim == 1:
        return values.astype(float)
    if values.shape[1] != 2:
        raise ValueError(f"expected two probability columns, got {values.shape}")
    classes = getattr(model.model_head, "classes_", np.array([0, 1]))
    classes = np.asarray(classes)
    positive = np.where(classes == 1)[0]
    if len(positive) != 1:
        raise ValueError(f"cannot identify KEEP probability column: classes={classes!r}")
    return values[:, int(positive[0])].astype(float)


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    args.output.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    paths = {name: args.data / f"{name}.csv" for name in ("train", "validation", "test")}
    frames = {name: load_split(path) for name, path in paths.items()}
    train = frames["train"]
    train = train[train["is_label_representative"] == "YES"].copy()
    validation = frames["validation"]
    test = frames["test"]

    train_dataset = Dataset.from_dict(
        {"text": train["text"].tolist(), "label": label_array(train).tolist()}
    )
    model = SetFitModel.from_pretrained(
        args.model_id,
        revision=args.model_revision,
        trust_remote_code=False,
    )
    model.model_body.max_seq_length = args.max_length
    training_args = TrainingArguments(
        batch_size=args.batch_size,
        num_epochs=args.body_epochs,
        num_iterations=args.num_iterations,
        seed=args.seed,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
    )
    trainer.train()

    validation_probability = probabilities(model, validation["text"].tolist())
    validation_events = event_view(validation, validation_probability)
    threshold, validation_metrics, recall_floor_met = choose_threshold(validation_events)

    # This historical GOLD was already exposed by compact pilot 1.0. It is kept
    # only for like-for-like comparison and cannot authorize production use.
    test_probability = probabilities(model, test["text"].tolist())
    test_prediction = (test_probability >= threshold).astype(int)
    row_metrics = metrics(label_array(test), test_prediction)
    test_events = event_view(test, test_probability)
    test_event_prediction = (
        test_events["keep_probability"].to_numpy(dtype=float) >= threshold
    ).astype(int)
    event_metrics = metrics(label_array(test_events), test_event_prediction)

    predictions = test[
        ["id", "event_group", "source", "title", "url", "final_label"]
    ].copy()
    predictions["keep_probability"] = test_probability
    predictions["prediction"] = np.where(test_prediction == 1, "KEEP", "REJECT")
    predictions["correct"] = predictions["prediction"] == predictions["final_label"]
    predictions.to_csv(
        args.output / "legacy_test_predictions.csv", index=False, quoting=csv.QUOTE_MINIMAL
    )

    test_events["prediction"] = np.where(test_event_prediction == 1, "KEEP", "REJECT")
    test_events["correct"] = test_events["prediction"] == test_events["final_label"]
    test_events.to_csv(args.output / "legacy_test_event_predictions.csv", index=False)

    exploratory_gates = {
        "keep_recall_at_least_0_90": event_metrics.keep_recall >= 0.90,
        "keep_precision_at_least_0_65": event_metrics.keep_precision >= 0.65,
        "keep_f1_at_least_0_75": event_metrics.keep_f1 >= 0.75,
        "balanced_accuracy_at_least_0_75": event_metrics.balanced_accuracy >= 0.75,
    }
    exploratory_gates["passed_all"] = all(exploratory_gates.values())
    compact_comparison = compare_compact(test_events, args.compact_reference)
    report = {
        "stage": "larger SetFit encoder experiment 2.0",
        "model_id": args.model_id,
        "model_revision": args.model_revision,
        "approximate_weight_file_bytes": 471000000,
        "seed": args.seed,
        "production_integration": False,
        "model_artifact_uploaded": False,
        "training": {
            "batch_size": args.batch_size,
            "num_iterations": args.num_iterations,
            "body_epochs": args.body_epochs,
            "max_length": args.max_length,
            "train_rows": int(len(train)),
            "validation_rows": int(len(validation)),
            "validation_events": int(len(validation_events)),
        },
        "data_sha256": {name: sha256(path) for name, path in paths.items()},
        "selection": {
            "source": "SILVER validation events only",
            "threshold": float(threshold),
            "recall_floor_met": recall_floor_met,
            "metrics": asdict(validation_metrics),
        },
        "legacy_benchmark": {
            "policy": (
                "historical GOLD already exposed by compact pilot; comparison only; "
                "not a blind production gate"
            ),
            "row_metrics": asdict(row_metrics),
            "event_metrics": asdict(event_metrics),
        },
        "compact_comparison": compact_comparison,
        "exploratory_gates": exploratory_gates,
        "production_gate": {
            "passed": False,
            "reason": "a newly collected blind GOLD event set is required",
        },
        "elapsed_seconds": time.monotonic() - started,
        "decision": "AWAIT_NEW_BLIND_GOLD",
    }
    (args.output / "large_experiment_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output / "RUN_SUMMARY.txt").write_text(
        "S-monitor larger SetFit experiment 2.0\n"
        f"Model: {args.model_id}\n"
        f"Legacy event precision: {event_metrics.keep_precision:.4f}\n"
        f"Legacy event recall: {event_metrics.keep_recall:.4f}\n"
        f"Legacy event F1: {event_metrics.keep_f1:.4f}\n"
        f"Legacy balanced accuracy: {event_metrics.balanced_accuracy:.4f}\n"
        "Decision: AWAIT_NEW_BLIND_GOLD\n"
        "Production monitoring was not changed.\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
