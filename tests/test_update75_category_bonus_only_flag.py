"""`category_bonus_only` diagnostic flag, added as P0 hardening after the
report-34/35 "работник" bug. That bug took roughly an hour of manual
debug-printing through a 2000-line function to trace: the only visible
symptom was an empty `subcategory` alongside a non-empty `category`, which
meant the winning category's weight came entirely from one of the ~24
disambiguation "bonus channel" additions (category_weights[x] += N) rather
than from any of the keyword lists that populate `subcategory`. This flag
makes that diagnosis a column lookup instead of a debugging session.

`category_bonus_only` is True exactly when the winning category has no
genuine keyword hits of its own (its `subcategory` would be empty) and its
weight is still greater than zero. It threads through
RelevanceDecision -> ArticleResult -> the CSV `category_bonus_only` column.
"""

from pathlib import Path

import social_monitor as sm


SETTINGS = sm.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def _source(language: str = "ru") -> sm.Source:
    return sm.Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Гродно",
        rank=1,
        priority="A",
        name="Тестовый источник",
        media_type="website",
        domain="example.by",
        start_url="https://example.by",
        language=language,
        adapter="standard",
    )


def test_bonus_only_false_for_genuine_keyword_category_win():
    d = sm.evaluate_relevance(
        "Рабочие завода жалуются на условия труда",
        "",
        "Сотрудники говорят, что работодатель не обеспечивает средства "
        "защиты, условия труда не соответствуют нормам, зарплата "
        "задерживается третий месяц.",
        _source(),
        SETTINGS,
    )
    assert d.category == "Работа, зарплаты и доходы"
    assert d.subcategory
    assert d.category_bonus_only is False


def test_bonus_only_true_for_bonus_channel_category_win():
    # Triggers the "inactive_population_signal" bonus channel (Белстат +
    # "не заняты в экономике" + "сколько/данные") with no genuine
    # employment-category keyword present anywhere else in the text.
    d = sm.evaluate_relevance(
        "Белстат обновил данные",
        "",
        "Свежие данные показывают, сколько человек не заняты в экономике "
        "региона в этом году.",
        _source(),
        SETTINGS,
    )
    assert d.category == "Работа, зарплаты и доходы"
    assert d.subcategory == ""
    assert d.category_bonus_only is True


def test_bonus_only_flag_reaches_csv_output():
    import csv
    import tempfile

    d = sm.evaluate_relevance(
        "Белстат обновил данные",
        "",
        "Свежие данные показывают, сколько человек не заняты в экономике "
        "региона в этом году.",
        _source(),
        SETTINGS,
    )
    assert d.category_bonus_only is True

    result = sm.ArticleResult(
        source_name="Т", source_type="website", country="Беларусь",
        locality="Гродно", priority="A", source_language="ru", title="t",
        title_generated=False, url="https://t.by/1", published_at="2026-09-03",
        category=d.category, subcategory=d.subcategory, excerpt="e",
        signal_type=d.signal_type, official_response=False, score=d.score,
        matched_terms="", discovered_via="feed", text_length=10,
        category_bonus_only=d.category_bonus_only,
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.csv"
        sm.write_csv_report(path, [result])
        with path.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    assert rows[0]["category_bonus_only"] == "True"
