"""Dependency-free threshold selection and cascade evaluation helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThresholdChoice:
    threshold: float
    selected: int
    correct: int
    precision: float
    target_precision: float
    target_met: bool


def choose_veto_threshold(labels, probabilities, target_precision=0.90):
    """Largest high-confidence REJECT region p(KEEP) < threshold."""
    candidates = sorted({0.0, *map(float, probabilities), 1.0000001})
    choices = []
    for threshold in candidates:
        selected = [i for i, p in enumerate(probabilities) if float(p) < threshold]
        if not selected:
            continue
        correct = sum(labels[i] == "REJECT" for i in selected)
        precision = correct / len(selected)
        if precision >= target_precision:
            choices.append((len(selected), precision, threshold, correct))
    if not choices:
        return ThresholdChoice(0.0, 0, 0, 1.0, target_precision, False)
    selected, precision, threshold, correct = max(choices, key=lambda x: (x[0], x[1], -x[2]))
    return ThresholdChoice(threshold, selected, correct, precision, target_precision, True)


def choose_rescue_threshold(labels, probabilities, target_precision=0.90):
    """Largest high-confidence KEEP region p(KEEP) >= threshold."""
    candidates = sorted({0.0, *map(float, probabilities), 1.0000001})
    choices = []
    for threshold in candidates:
        selected = [i for i, p in enumerate(probabilities) if float(p) >= threshold]
        if not selected:
            continue
        correct = sum(labels[i] == "KEEP" for i in selected)
        precision = correct / len(selected)
        if precision >= target_precision:
            choices.append((len(selected), precision, threshold, correct))
    if not choices:
        return ThresholdChoice(1.0000001, 0, 0, 1.0, target_precision, False)
    selected, precision, threshold, correct = max(choices, key=lambda x: (x[0], x[1], x[2]))
    return ThresholdChoice(threshold, selected, correct, precision, target_precision, True)


def cascade_decision(regex_decision, keep_probability, veto_threshold, rescue_threshold, protected=False):
    if protected:
        return "KEEP", "protected_keep"
    probability = float(keep_probability)
    if regex_decision == "KEEP" and probability < veto_threshold:
        return "REJECT", "veto"
    if regex_decision == "REJECT" and probability >= rescue_threshold:
        return "KEEP", "rescue"
    return regex_decision, "unchanged"


def binary_metrics(labels, predictions, weights=None):
    weights = [1.0] * len(labels) if weights is None else [float(x) for x in weights]
    cells = {"tn": 0.0, "fp": 0.0, "fn": 0.0, "tp": 0.0}
    for truth, prediction, weight in zip(labels, predictions, weights):
        key = ("t" if truth == prediction else "f") + ("p" if prediction == "KEEP" else "n")
        cells[key] += weight
    tn, fp, fn, tp = cells["tn"], cells["fp"], cells["fn"], cells["tp"]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    specificity = tn / (tn + fp) if tn + fp else 0.0
    return {
        **cells,
        "rows_or_weight": sum(weights),
        "accuracy": (tp + tn) / sum(weights) if weights else 0.0,
        "balanced_accuracy": (recall + specificity) / 2,
        "keep_precision": precision,
        "keep_recall": recall,
        "keep_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
    }
