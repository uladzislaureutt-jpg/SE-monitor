"""Regression cases from the first advisory MiniLM shadow and run 47."""

import importlib.util
from pathlib import Path

import pytest

import social_monitor as sm


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = sm.load_settings(ROOT / "config" / "settings.yaml")
SOURCE = sm.Source(True, "Беларусь", "BY", "Беларусь", 1, "A", "Тест", "website", "", "", "ru")


def decision(title: str, text: str) -> sm.RelevanceDecision:
    return sm.evaluate_relevance(title, "", text, SOURCE, SETTINGS)


def test_positive_community_cleanup_is_not_a_problem_card() -> None:
    result = decision(
        "Яркой осени — да: в Гомеле проводят большую уборку",
        "Во время областного субботника жители косят траву, приводят в порядок "
        "детские площадки, скверы и дворы.",
    )
    assert result.relevant is False


def test_foreign_security_diplomacy_is_not_social_monitoring() -> None:
    result = decision(
        "От Беларуси зависит безопасность региона, заявил украинский дипломат",
        "Дипломат говорил о диалоге, демократизации, войне и ультиматуме, "
        "не описывая нарушение услуги или ущерб жителям.",
    )
    assert result.relevant is False


def test_foreign_trade_sanctions_statement_is_not_rescue() -> None:
    result = decision(
        "Депутат пожаловался на закрытый для белорусского калия порт Клайпеды",
        "Он заявил, что санкции нарушают право страны на доступ к морю и "
        "сократили долю на мировом рынке.",
    )
    assert result.relevant is False


def test_indian_environment_story_is_excluded_without_belarusian_impact() -> None:
    result = decision(
        "Студент из Индии за шесть месяцев самостоятельно очистил загрязненную реку",
        "Местные жители годами страдали от грязной воды и мусора на берегах.",
    )
    assert result.relevant is False
    assert "зарубежный сюжет" in result.reason


def test_shadow_score_is_a_normalized_softmax_sum() -> None:
    path = ROOT / "ml/semantic_shadow_v2/run_shadow.py"
    spec = importlib.util.spec_from_file_location("semantic_shadow", path)
    assert spec and spec.loader
    shadow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shadow)
    result = {"labels": shadow.HYPOTHESES, "scores": [0.21, 0.17, 0.18, 0.16, 0.15, 0.13]}
    assert shadow.normalized_keep_score(result) == pytest.approx(0.38)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        shadow.normalized_keep_score({"labels": shadow.HYPOTHESES, "scores": [0.8, 0.7, 0, 0, 0, 0]})


def test_shadow_uses_single_distribution_not_independent_labels() -> None:
    runner = (ROOT / "ml/semantic_shadow_v2/run_shadow.py").read_text(encoding="utf-8")
    assert "multi_label=False" in runner
    assert "multi_label=True" not in runner


def test_update83_build_marker() -> None:
    assert sm.MONITOR_BUILD == "2026-09-05.social.83-run47-integrity-shadow-score-1.0"
