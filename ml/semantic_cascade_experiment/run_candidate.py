#!/usr/bin/env python3
"""Train one frozen title-only candidate and evaluate the regex+semantic cascade."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.linear_model import LogisticRegression

from cascade_core import binary_metrics, cascade_decision, choose_rescue_threshold, choose_veto_threshold


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", choices=("tfidf", "setfit"), required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--legacy-data", type=Path, required=True)
    p.add_argument("--gold", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model-id", default="")
    p.add_argument("--model-revision", default="")
    p.add_argument("--seed", type=int, default=20260903)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--num-iterations", type=int, default=3)
    p.add_argument("--body-epochs", type=int, default=1)
    p.add_argument("--max-length", type=int, default=256)
    p.add_argument("--action-precision", type=float, default=0.90)
    return p.parse_args()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path, gold=False):
    frame = pd.read_csv(path, dtype=str).fillna("")
    required = {"title", "final_label", "event_group"}
    if gold:
        required |= {"regex_decision", "sampling_weight"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path}: missing {sorted(missing)}")
    if not set(frame.final_label).issubset({"KEEP", "REJECT"}):
        raise ValueError(f"{path}: non-binary labels")
    frame["text"] = frame.title.str.strip()  # Common evidence available for every GOLD row.
    if (frame.text.str.len() == 0).any():
        raise ValueError(f"{path}: empty title")
    return frame


def event_view(frame, probabilities, include_gold=False):
    columns = ["event_group", "final_label", "source", "title", "url"]
    if include_gold:
        columns += ["regex_decision", "sampling_weight", "evidence_available"]
    work = frame[columns].copy()
    work["keep_probability"] = probabilities
    if (work.groupby("event_group").final_label.nunique() > 1).any():
        raise ValueError("evaluation data contains a mixed-label event")
    agg = {"final_label": "first", "source": "first", "title": "first", "url": "first", "keep_probability": "mean"}
    if include_gold:
        agg.update(regex_decision="first", sampling_weight="first", evidence_available="first")
    grouped = work.groupby("event_group", sort=True, as_index=False).agg(agg)
    if len(grouped) != frame.event_group.nunique():
        raise ValueError("event grouping failed")
    return grouped


def setfit_probabilities(model, texts):
    try:
        raw = model.predict_proba(texts, as_numpy=True)
    except TypeError:
        raw = model.predict_proba(texts)
    if hasattr(raw, "detach"):
        raw = raw.detach().cpu().numpy()
    values = np.asarray(raw)
    if values.ndim == 1:
        return values.astype(float)
    classes = np.asarray(getattr(model.model_head, "classes_", np.array([0, 1])))
    positive = np.where(classes == 1)[0]
    if len(positive) != 1:
        raise ValueError(f"cannot locate KEEP class: {classes!r}")
    return values[:, int(positive[0])].astype(float)


def train_predict(args, train, validation, gold):
    if args.candidate == "tfidf":
        features = FeatureUnion([
            ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=60000, sublinear_tf=True)),
            ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2, max_features=120000, sublinear_tf=True)),
        ])
        x_train = features.fit_transform(train.text)
        model = LogisticRegression(C=2.0, class_weight="balanced", max_iter=2000, random_state=args.seed)
        model.fit(x_train, (train.final_label == "KEEP").astype(int))
        return model.predict_proba(features.transform(validation.text))[:, 1], model.predict_proba(features.transform(gold.text))[:, 1], {"model_id": "TF-IDF word+char / logistic regression", "revision": "local-frozen-config"}
    if not args.model_id:
        raise ValueError("--model-id is required for SetFit")
    import torch
    from datasets import Dataset
    from setfit import SetFitModel, Trainer, TrainingArguments
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    kwargs = {"trust_remote_code": False}
    if args.model_revision:
        kwargs["revision"] = args.model_revision
    model = SetFitModel.from_pretrained(args.model_id, **kwargs)
    model.model_body.max_seq_length = args.max_length
    dataset = Dataset.from_dict({"text": train.text.tolist(), "label": (train.final_label == "KEEP").astype(int).tolist()})
    trainer = Trainer(model=model, args=TrainingArguments(batch_size=args.batch_size, num_epochs=args.body_epochs, num_iterations=args.num_iterations, seed=args.seed), train_dataset=dataset)
    trainer.train()
    return setfit_probabilities(model, validation.text.tolist()), setfit_probabilities(model, gold.text.tolist()), {"model_id": args.model_id, "revision": args.model_revision or "repository-default-at-run-time"}


def main():
    args = parse_args(); started = time.monotonic(); args.output.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    train = load(args.legacy_data / "train.csv")
    if "is_label_representative" in train:
        train = train[train.is_label_representative == "YES"].copy()
    validation = load(args.legacy_data / "validation.csv")
    gold = load(args.gold, gold=True)
    validation_probability, gold_probability, model_meta = train_predict(args, train, validation, gold)
    val_events = event_view(validation, validation_probability)
    veto = choose_veto_threshold(val_events.final_label.tolist(), val_events.keep_probability.tolist(), args.action_precision)
    rescue = choose_rescue_threshold(val_events.final_label.tolist(), val_events.keep_probability.tolist(), args.action_precision)
    gold_events = event_view(gold, gold_probability, include_gold=True)
    cascade, actions = [], []
    for row in gold_events.itertuples():
        decision, action = cascade_decision(row.regex_decision, row.keep_probability, veto.threshold, rescue.threshold)
        cascade.append(decision); actions.append(action)
    truth = gold_events.final_label.tolist(); baseline = gold_events.regex_decision.tolist(); weights = gold_events.sampling_weight.astype(float).tolist()
    baseline_raw = binary_metrics(truth, baseline); cascade_raw = binary_metrics(truth, cascade)
    baseline_weighted = binary_metrics(truth, baseline, weights); cascade_weighted = binary_metrics(truth, cascade, weights)
    fixes = sum(b != t and c == t for b, c, t in zip(baseline, cascade, truth))
    harms = sum(b == t and c != t for b, c, t in zip(baseline, cascade, truth))
    action_stats = {}
    for name in ("veto", "rescue"):
        idx = [i for i, action in enumerate(actions) if action == name]
        correct = sum(cascade[i] == truth[i] for i in idx)
        action_stats[name] = {"count": len(idx), "correct": correct, "harmful": len(idx) - correct, "precision": correct / len(idx) if idx else None}
    report = {
        "experiment": "semantic cascade blind evaluation 1.0", "candidate": args.slug,
        "candidate_type": args.candidate, **model_meta, "feature_mode": "title_only",
        "production_integration": False, "model_artifact_uploaded": False, "seed": args.seed,
        "data_sha256": {"train": digest(args.legacy_data / "train.csv"), "validation": digest(args.legacy_data / "validation.csv"), "gold": digest(args.gold)},
        "training": {"representatives": len(train), "validation_events": len(val_events), "gold_events": len(gold_events)},
        "threshold_selection": {"source": "legacy SILVER validation only", "action_precision_target": args.action_precision, "veto": asdict(veto), "rescue": asdict(rescue)},
        "gold": {"baseline_raw": baseline_raw, "cascade_raw": cascade_raw, "baseline_weighted": baseline_weighted, "cascade_weighted": cascade_weighted, "actions": action_stats, "fixed_errors": fixes, "introduced_errors": harms, "net_error_reduction": fixes - harms},
        "decision": "EVALUATED_ONCE_SHADOW_ONLY", "elapsed_seconds": time.monotonic() - started,
    }
    (args.output / f"{args.slug}_metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    gold_events["baseline_correct"] = gold_events.regex_decision == gold_events.final_label
    gold_events["cascade_prediction"] = cascade; gold_events["cascade_action"] = actions
    gold_events["cascade_correct"] = gold_events.cascade_prediction == gold_events.final_label
    gold_events.to_csv(args.output / f"{args.slug}_gold_predictions.csv", index=False, quoting=csv.QUOTE_MINIMAL)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
