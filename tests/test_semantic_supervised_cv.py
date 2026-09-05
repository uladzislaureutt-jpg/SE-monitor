from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ml" / "semantic_supervised_cv"))

from core import crossfit_cascade, metrics


def test_crossfit_thresholds_do_not_use_evaluated_fold_labels():
    labels = ["REJECT", "KEEP", "REJECT", "KEEP"]
    regex = ["KEEP", "REJECT", "KEEP", "REJECT"]
    scores = [0.05, 0.95, 0.30, 0.70]
    folds = [0, 0, 1, 1]
    decisions, actions, choices = crossfit_cascade(labels, regex, scores, folds, action_precision=0.9)
    # Fold 0 thresholds can only have learned from indexes 2 and 3.
    assert choices["0"]["evaluation_rows"] == 2
    assert choices["1"]["evaluation_rows"] == 2
    assert len(decisions) == len(labels) == len(actions)


def test_weighted_metrics_preserve_keep_direction():
    result = metrics(["KEEP", "REJECT"], ["KEEP", "KEEP"], [3, 1])
    assert result["tp"] == 3
    assert result["fp"] == 1
    assert result["keep_precision"] == 0.75
