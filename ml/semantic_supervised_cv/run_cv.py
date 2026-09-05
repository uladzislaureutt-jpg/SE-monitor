#!/usr/bin/env python3
"""Train compact supervised candidates with event-grouped OOF evaluation.

This is an isolated model-selection experiment.  It never imports the monitor
and never writes a model consumed by production.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import FeatureUnion

from core import action_stats, crossfit_cascade, metrics


def args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", choices=("tfidf", "setfit"), required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-id", default="")
    parser.add_argument("--model-revision", default="")
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--action-precision", type=float, default=0.90)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--num-iterations", type=int, default=3)
    parser.add_argument("--body-epochs", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=256)
    return parser.parse_args()


def load_gold(path):
    frame = pd.read_csv(path, dtype=str).fillna("")
    required = {"item_id", "event_id", "title", "regex_decision", "final_label", "sampling_weight"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")
    if len(frame) != 240:
        raise ValueError(f"expected exactly 240 finalized GOLD cards, got {len(frame)}")
    if not set(frame.final_label).issubset({"KEEP", "REJECT"}):
        raise ValueError("GOLD contains non-binary final labels")
    if not set(frame.regex_decision).issubset({"KEEP", "REJECT"}):
        raise ValueError("GOLD contains non-binary regex decisions")
    if frame.item_id.duplicated().any():
        raise ValueError("duplicate item_id")
    if (frame.title.str.strip() == "").any():
        raise ValueError("empty title")
    frame["text"] = "TITLE: " + frame.title.str.strip()
    return frame


def tfidf_fit_predict(train, test, seed):
    features = FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=60_000, sublinear_tf=True)),
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2, max_features=120_000, sublinear_tf=True)),
    ])
    x_train = features.fit_transform(train.text)
    model = LogisticRegression(C=1.0, class_weight="balanced", max_iter=2_000, random_state=seed)
    model.fit(x_train, (train.final_label == "KEEP").astype(int))
    return model.predict_proba(features.transform(test.text))[:, 1]


def setfit_fit_predict(train, test, settings):
    import torch
    from datasets import Dataset
    from setfit import SetFitModel, Trainer, TrainingArguments
    random.seed(settings.seed); np.random.seed(settings.seed); torch.manual_seed(settings.seed)
    kwargs = {"trust_remote_code": False}
    if settings.model_revision:
        kwargs["revision"] = settings.model_revision
    model = SetFitModel.from_pretrained(settings.model_id, **kwargs)
    model.model_body.max_seq_length = settings.max_length
    dataset = Dataset.from_dict({"text": train.text.tolist(), "label": (train.final_label == "KEEP").astype(int).tolist()})
    trainer = Trainer(
        model=model,
        args=TrainingArguments(batch_size=settings.batch_size, num_epochs=settings.body_epochs,
                               num_iterations=settings.num_iterations, seed=settings.seed),
        train_dataset=dataset,
    )
    trainer.train()
    try:
        raw = model.predict_proba(test.text.tolist(), as_numpy=True)
    except TypeError:
        raw = model.predict_proba(test.text.tolist())
    if hasattr(raw, "detach"):
        raw = raw.detach().cpu().numpy()
    raw = np.asarray(raw)
    classes = np.asarray(getattr(model.model_head, "classes_", np.array([0, 1])))
    keep = np.where(classes == 1)[0]
    if len(keep) != 1:
        raise ValueError(f"cannot locate KEEP class: {classes!r}")
    return raw[:, int(keep[0])].astype(float)


def gates(result):
    actions = result["actions"]
    weighted = result["cascade_weighted"]
    baseline = result["baseline_weighted"]
    return {
        "net_error_reduction_at_least_10": result["net_error_reduction"] >= 10,
        "introduced_errors_at_most_5": result["introduced_errors"] <= 5,
        "weighted_recall_loss_at_most_0_02": weighted["keep_recall"] >= baseline["keep_recall"] - 0.02,
        "veto_precision_at_least_0_85": actions["veto"]["count"] > 0 and actions["veto"]["precision"] >= 0.85,
        "rescue_precision_at_least_0_80": actions["rescue"]["count"] > 0 and actions["rescue"]["precision"] >= 0.80,
    }


def main():
    settings = args(); started = time.monotonic(); settings.output.mkdir(parents=True, exist_ok=True)
    frame = load_gold(settings.gold)
    splitter = StratifiedGroupKFold(n_splits=settings.folds, shuffle=True, random_state=settings.seed)
    y = (frame.final_label == "KEEP").astype(int)
    probabilities = np.full(len(frame), np.nan); folds = np.full(len(frame), -1, dtype=int)
    for fold, (train_idx, test_idx) in enumerate(splitter.split(frame.text, y, groups=frame.event_id)):
        train, test = frame.iloc[train_idx], frame.iloc[test_idx]
        if settings.candidate == "tfidf":
            scores = tfidf_fit_predict(train, test, settings.seed + fold)
        else:
            if not settings.model_id:
                raise ValueError("--model-id is required for SetFit")
            per_fold = argparse.Namespace(**vars(settings)); per_fold.seed = settings.seed + fold
            scores = setfit_fit_predict(train, test, per_fold)
        probabilities[test_idx] = scores; folds[test_idx] = fold
        print(f"completed fold {fold + 1}/{settings.folds}", flush=True)
    if np.isnan(probabilities).any() or (folds < 0).any():
        raise RuntimeError("incomplete out-of-fold predictions")
    labels = frame.final_label.tolist(); regex = frame.regex_decision.tolist(); weights = frame.sampling_weight.astype(float).tolist()
    decisions, actions, thresholds = crossfit_cascade(labels, regex, probabilities.tolist(), folds.tolist(), settings.action_precision)
    fixed = sum(base != truth and prediction == truth for base, prediction, truth in zip(regex, decisions, labels))
    harmed = sum(base == truth and prediction != truth for base, prediction, truth in zip(regex, decisions, labels))
    result = {
        "experiment": "semantic supervised grouped cross-validation 1.0",
        "candidate": settings.slug, "candidate_type": settings.candidate,
        "model_id": "TF-IDF word+char / logistic regression" if settings.candidate == "tfidf" else settings.model_id,
        "model_revision": settings.model_revision or "local/default",
        "production_integration": False, "model_artifact_uploaded": False,
        "feature_mode": "title_only", "gold_rows": len(frame), "gold_events": int(frame.event_id.nunique()),
        "folds": settings.folds, "seed": settings.seed,
        "data_sha256": hashlib.sha256(settings.gold.read_bytes()).hexdigest(),
        "threshold_protocol": "For each test fold thresholds use only OOF predictions and labels from the other folds.",
        "crossfit_thresholds": thresholds,
        "baseline_raw": metrics(labels, regex), "cascade_raw": metrics(labels, decisions),
        "baseline_weighted": metrics(labels, regex, weights), "cascade_weighted": metrics(labels, decisions, weights),
        "actions": action_stats(labels, actions, decisions), "fixed_errors": fixed,
        "introduced_errors": harmed, "net_error_reduction": fixed - harmed,
        "elapsed_seconds": time.monotonic() - started,
        "decision": "EXPLORATORY_ONLY__REQUIRES_FRESH_PROSPECTIVE_GOLD",
    }
    result["gates"] = gates(result); result["gates"]["passed_all"] = all(result["gates"].values())
    output = frame[["item_id", "event_id", "source", "title", "url", "regex_decision", "final_label", "sampling_weight"]].copy()
    output["fold"] = folds; output["keep_probability_oof"] = probabilities
    output["cascade_decision"] = decisions; output["cascade_action"] = actions
    output["baseline_correct"] = output.regex_decision == output.final_label
    output["cascade_correct"] = output.cascade_decision == output.final_label
    output.to_csv(settings.output / f"{settings.slug}_oof_predictions.csv", index=False, quoting=csv.QUOTE_MINIMAL)
    (settings.output / f"{settings.slug}_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
