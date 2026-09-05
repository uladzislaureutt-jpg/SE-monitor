"""Metrics and leakage-safe cross-fitted cascade helpers."""

from __future__ import annotations

from dataclasses import asdict


def choose_veto_threshold(labels, scores, target):
    """Maximise the covered low-score region subject to REJECT precision."""
    options = []
    for threshold in sorted({0.0, *map(float, scores), 1.0000001}):
        selected = [i for i, score in enumerate(scores) if float(score) < threshold]
        if not selected:
            continue
        correct = sum(labels[i] == "REJECT" for i in selected)
        precision = correct / len(selected)
        if precision >= target:
            options.append((len(selected), precision, threshold, correct))
    if not options:
        return {"threshold": 0.0, "selected": 0, "correct": 0, "precision": None, "target": target, "met": False}
    selected, precision, threshold, correct = max(options, key=lambda item: (item[0], item[1], -item[2]))
    return {"threshold": threshold, "selected": selected, "correct": correct, "precision": precision, "target": target, "met": True}


def choose_rescue_threshold(labels, scores, target):
    """Maximise the covered high-score region subject to KEEP precision."""
    options = []
    for threshold in sorted({0.0, *map(float, scores), 1.0000001}):
        selected = [i for i, score in enumerate(scores) if float(score) >= threshold]
        if not selected:
            continue
        correct = sum(labels[i] == "KEEP" for i in selected)
        precision = correct / len(selected)
        if precision >= target:
            options.append((len(selected), precision, threshold, correct))
    if not options:
        return {"threshold": 1.0000001, "selected": 0, "correct": 0, "precision": None, "target": target, "met": False}
    selected, precision, threshold, correct = max(options, key=lambda item: (item[0], item[1], item[2]))
    return {"threshold": threshold, "selected": selected, "correct": correct, "precision": precision, "target": target, "met": True}


def cascade(regex, probability, veto, rescue):
    if regex == "KEEP" and probability < veto:
        return "REJECT", "veto"
    if regex == "REJECT" and probability >= rescue:
        return "KEEP", "rescue"
    return regex, "unchanged"


def crossfit_cascade(labels, regex, scores, folds, action_precision=0.90):
    """Apply thresholds chosen without the evaluated fold's labels.

    Every score must already be out-of-fold.  For fold F, the thresholds use
    labels and OOF scores from all folds except F; the resulting action on F is
    therefore free from both fit and threshold-selection leakage.
    """
    decisions, actions, fold_choices = [None] * len(labels), [None] * len(labels), {}
    for fold in sorted(set(folds)):
        train = [i for i, value in enumerate(folds) if value != fold]
        test = [i for i, value in enumerate(folds) if value == fold]
        veto = choose_veto_threshold([labels[i] for i in train], [scores[i] for i in train], action_precision)
        rescue = choose_rescue_threshold([labels[i] for i in train], [scores[i] for i in train], action_precision)
        fold_choices[str(fold)] = {"veto": veto, "rescue": rescue, "evaluation_rows": len(test)}
        for i in test:
            decisions[i], actions[i] = cascade(regex[i], float(scores[i]), veto["threshold"], rescue["threshold"])
    return decisions, actions, fold_choices


def metrics(labels, predictions, weights=None):
    values = [1.0] * len(labels) if weights is None else [float(item) for item in weights]
    cells = {"tn": 0.0, "fp": 0.0, "fn": 0.0, "tp": 0.0}
    for truth, prediction, weight in zip(labels, predictions, values):
        key = ("t" if truth == prediction else "f") + ("p" if prediction == "KEEP" else "n")
        cells[key] += weight
    tn, fp, fn, tp = (cells[key] for key in ("tn", "fp", "fn", "tp"))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    specificity = tn / (tn + fp) if tn + fp else 0.0
    return {
        **cells, "rows_or_weight": sum(values),
        "accuracy": (tp + tn) / sum(values) if values else 0.0,
        "balanced_accuracy": (recall + specificity) / 2,
        "keep_precision": precision, "keep_recall": recall,
        "keep_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
    }


def action_stats(labels, actions, decisions):
    result = {}
    for action, expected in (("veto", "REJECT"), ("rescue", "KEEP")):
        indices = [i for i, item in enumerate(actions) if item == action]
        correct = sum(decisions[i] == labels[i] == expected for i in indices)
        result[action] = {
            "count": len(indices), "correct": correct,
            "harmful": len(indices) - correct,
            "precision": correct / len(indices) if indices else None,
        }
    return result
