#!/usr/bin/env python3
"""Leakage-safe SetFit pilot for S-monitor.

The script is intentionally independent from social_monitor.py. It reads only
the frozen event-safe CSV splits and writes a model plus diagnostic artifacts.
No production state, cache, reports, secrets, delivery or workflow are used.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
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
    parser.add_argument("--model-id", default="cointegrated/rubert-tiny2")
    parser.add_argument("--seed", type=int, default=20260822)
    parser.add_argument("--batch-size", type=int, default=16)
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
    work = frame[["event_group", "final_label"]].copy()
    work["keep_probability"] = probabilities
    grouped = work.groupby("event_group", sort=True, as_index=False).agg(
        final_label=("final_label", "first"),
        label_count=("final_label", "nunique"),
        keep_probability=("keep_probability", "mean"),
        rows=("final_label", "size"),
    )
    if (grouped["label_count"] != 1).any():
        raise ValueError("evaluation split contains a mixed-label event")
    return grouped


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
    args.output.mkdir(parents=True, exist_ok=True)
    model_dir = args.output / "model"

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
    model = SetFitModel.from_pretrained(args.model_id)
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

    # GOLD is first used here, after the model and threshold have been frozen.
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
        args.output / "gold_test_predictions.csv", index=False, quoting=csv.QUOTE_MINIMAL
    )

    test_events["prediction"] = np.where(test_event_prediction == 1, "KEEP", "REJECT")
    test_events["correct"] = test_events["prediction"] == test_events["final_label"]
    test_events.to_csv(args.output / "gold_test_event_predictions.csv", index=False)

    model.save_pretrained(model_dir)
    acceptance = {
        "keep_recall_at_least_0_90": event_metrics.keep_recall >= 0.90,
        "keep_precision_at_least_0_65": event_metrics.keep_precision >= 0.65,
        "keep_f1_at_least_0_75": event_metrics.keep_f1 >= 0.75,
        "balanced_accuracy_at_least_0_75": event_metrics.balanced_accuracy >= 0.75,
    }
    acceptance["passed_all"] = all(acceptance.values())
    report = {
        "stage": "compact SetFit pilot 1.0",
        "model_id": args.model_id,
        "seed": args.seed,
        "production_integration": False,
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
        "gold_test": {
            "policy": "evaluated once after model and threshold selection",
            "row_metrics": asdict(row_metrics),
            "event_metrics": asdict(event_metrics),
        },
        "acceptance": acceptance,
        "decision": "SHADOW_CANDIDATE" if acceptance["passed_all"] else "REJECT_PILOT",
    }
    (args.output / "setfit_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
