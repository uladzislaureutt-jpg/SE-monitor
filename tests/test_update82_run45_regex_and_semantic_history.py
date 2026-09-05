"""Run-45 regression checks and semantic-input retention for update82."""

from pathlib import Path

import social_monitor as sm


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = sm.load_settings(ROOT / "config" / "settings.yaml")
SOURCE = sm.Source(True, "Беларусь", "BY", "Беларусь", 1, "A", "Тест", "website", "", "", "ru")


def decision(title: str, text: str) -> sm.RelevanceDecision:
    return sm.evaluate_relevance(title, "", text, SOURCE, SETTINGS)


def test_run45_generic_115_instructions_are_not_cards() -> None:
    result = decision(
        "Заявку в 115 закрыли, а проблема осталась? В МинЖКХ объяснили, как действовать",
        "Если работы выполнены некачественно, новую заявку подавать не нужно. "
        "В течение трёх рабочих дней потребитель может позвонить 115, после чего "
        "заявку повторно откроют и вернут исполнителю на доработку.",
    )
    assert result.relevant is False
    assert "общий порядок" in result.reason


def test_run45_promotional_contest_stays_out_despite_old_controversy() -> None:
    result = decision(
        "ТРЦ на Московской в Бресте ищет название: победителю обещают iPhone 17",
        "Застройщик предложил брестчанам придумать название торговому центру. "
        "После аварии 2025 года жители в комментариях вспоминали обрушение.",
    )
    assert result.relevant is False


def test_run45_resolved_border_transfer_stays_out() -> None:
    result = decision(
        "На границе пассажиры смогут бесплатно пересесть в автобус ближе к шлагбауму",
        "Минсктранс разрешил бесплатную пересадку в автобус, который раньше пройдет границу.",
    )
    assert result.relevant is False


def test_run45_official_schedule_denial_stays_out() -> None:
    result = decision(
        "Достроить вторую очередь третьей линии метро в Минске планируют в 2028 году",
        "Председатель горисполкома назвал недостоверными сообщения о переносе сроков "
        "строительства на два года и сказал, что работы идут по плану.",
    )
    assert result.relevant is False


def test_run45_unpaid_wages_for_many_workers_are_restored() -> None:
    result = decision(
        "Спортивная организация в Солигорском районе полгода не платила зарплату — вмешалась прокуратура",
        "Прокуратура обнаружила несвоевременную выплату заработной платы 26 сотрудникам "
        "за период с января по июнь. Общая задолженность превысила 270 тысяч рублей.",
    )
    assert result.relevant is True
    assert result.category == "Работа, зарплаты и доходы"


def test_dns_access_restriction_is_one_sale_noncompliance_event() -> None:
    fp = sm.infer_event_fingerprint(
        "Госстандарт запретил DNS продавать 116 наименований бытовой техники",
        "В Беларуси выявили продажу без сертификатов соответствия.",
        "В 14 магазинах DNS запретили продажу 116 наименований продукции.",
    )
    assert fp.signature == "беларусь|consumer_electronics_compliance|sale_noncompliance"


def test_semantic_input_archive_is_private_and_persistent() -> None:
    workflow = (ROOT / ".github/workflows/daily-social-monitor.yml").read_text(encoding="utf-8")
    assert "data/ml/raw_history/semantic_inputs" in workflow
    assert "debug/semantic_training_signals_*.csv" in workflow
    assert "reports/semantic_training" not in workflow


def test_semantic_shadow_is_manual_and_advisory_only() -> None:
    shadow_workflow = (ROOT / ".github/workflows/social-semantic-shadow.yml").read_text(encoding="utf-8")
    runner = (ROOT / "ml/semantic_shadow_v2/run_shadow.py").read_text(encoding="utf-8")
    assert "workflow_dispatch" in shadow_workflow
    assert "daily-social-monitor" not in shadow_workflow
    assert "MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli" in runner
    assert "VETO_CANDIDATE_REVIEW" in runner
    assert "RESCUE_CANDIDATE_REVIEW" in runner
    assert "output_is_advisory" in runner


def test_shadow_priority_never_overrides_regex_decision() -> None:
    import importlib.util

    path = ROOT / "ml/semantic_shadow_v2/run_shadow.py"
    spec = importlib.util.spec_from_file_location("semantic_shadow", path)
    assert spec and spec.loader
    shadow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shadow)
    assert shadow.make_priority("KEEP", 0.22) == "VETO_CANDIDATE_REVIEW"
    assert shadow.make_priority("REJECT", 0.68) == "RESCUE_CANDIDATE_REVIEW"
    assert shadow.make_priority("KEEP", 0.8) == "OBSERVE_ONLY"


def test_update82_build_marker() -> None:
    assert sm.MONITOR_BUILD == "2026-09-05.social.83-run47-integrity-shadow-score-1.0"
