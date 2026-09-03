import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ml" / "semantic_cascade_experiment"))
import cascade_core as core


def test_protected_keep_cannot_be_vetoed():
    decision, action = core.cascade_decision("KEEP", 0.01, 0.2, 0.8, protected=True)
    assert (decision, action) == ("KEEP", "protected_keep")


def test_uncertain_probability_preserves_regex_decision():
    assert core.cascade_decision("KEEP", 0.5, 0.2, 0.8) == ("KEEP", "unchanged")
    assert core.cascade_decision("REJECT", 0.5, 0.2, 0.8) == ("REJECT", "unchanged")


def test_extremes_trigger_only_expected_action():
    assert core.cascade_decision("KEEP", 0.1, 0.2, 0.8) == ("REJECT", "veto")
    assert core.cascade_decision("REJECT", 0.9, 0.2, 0.8) == ("KEEP", "rescue")


def test_thresholds_meet_empirical_precision_target():
    labels = ["REJECT"] * 9 + ["KEEP"] + ["KEEP"] * 9 + ["REJECT"]
    probabilities = [i / 100 for i in range(10)] + [0.90 + i / 100 for i in range(10)]
    veto = core.choose_veto_threshold(labels, probabilities, 0.90)
    rescue = core.choose_rescue_threshold(labels, probabilities, 0.90)
    assert veto.target_met and veto.precision >= 0.90
    assert rescue.target_met and rescue.precision >= 0.90


def test_binary_metrics_known_matrix():
    result = core.binary_metrics(["KEEP", "KEEP", "REJECT", "REJECT"], ["KEEP", "REJECT", "KEEP", "REJECT"])
    assert (result["tp"], result["fn"], result["fp"], result["tn"]) == (1.0, 1.0, 1.0, 1.0)


if __name__ == "__main__":
    tests = sorted((name, value) for name, value in globals().items() if name.startswith("test_") and callable(value))
    for _, test in tests:
        test()
    print(f"semantic cascade self-test: {len(tests)} passed")
