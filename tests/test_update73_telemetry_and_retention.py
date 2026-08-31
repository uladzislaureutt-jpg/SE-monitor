from pathlib import Path

import social_monitor


SETTINGS = social_monitor.load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
)


def source() -> social_monitor.Source:
    return social_monitor.Source(
        enabled=True,
        country="Беларусь",
        country_code="BY",
        locality="Беларусь",
        rank=1,
        priority="A",
        name="Тестовый источник",
        media_type="website",
        domain="example.by",
        start_url="https://example.by",
        language="ru",
        adapter="standard",
    )


def decision(title: str, text: str):
    return social_monitor.evaluate_relevance(title, "", text, source(), SETTINGS)


def test_update73_rejects_smart_meter_training_simulation_but_keeps_school_resonance():
    smart_meters = decision(
        "Квартира будущего: как в «Гродноэнерго» учат работать с умными счётчиками",
        "Учебный центр моделирует нештатные ситуации. Энергетики объяснили, "
        "куда обращаться, если не работает уличное освещение в деревне.",
    )
    school_rules = decision(
        "В барановичской средней школе №1 имени Сергея Грицевца опубликовали требования к внешнему виду учеников.",
        "Требования вызвали активное обсуждение в Threads, где родители и "
        "жители города высказывали противоположные позиции.",
    )
    assert not smart_meters.relevant, smart_meters.reason
    assert school_rules.relevant, school_rules.reason


def test_update73_wagon_shortage_is_relevant_before_seen_url_suppression():
    cases = (
        (
            "В Беларуси начался дефицит грузовых вагонов-платформ. «Железнодорожники Беларуси»: дело в воинских перевозках",
            "Железнодорожники сообщили о нехватке вагонов-платформ БЖД из-за воинских перевозок.",
        ),
        (
            "На БЖД возник дефицит вагонов-платформ из-за воинских перевозок",
            "На Белорусской железной дороге не хватает вагонов-платформ; причиной называют воинские перевозки.",
        ),
    )
    for title, text in cases:
        assessed = decision(title, text)
        assert assessed.relevant, (title, assessed.reason)
    fingerprint = social_monitor.infer_event_fingerprint(*cases[0], "")
    assert fingerprint.signature == "беларусь|rail_platform_wagons|absence_shortage"


def test_update73_marks_material_partial_transport_loss_and_alerts_operator():
    item = source()
    collection = social_monitor.SourceCollectionMetrics(selected_candidates=35)
    processing = social_monitor.SourceProcessingMetrics(
        processed=35,
        fetch_ok=24,
        fetch_failed=11,
        extraction_full=24,
        extraction_failed=11,
    )
    row = social_monitor.build_source_coverage(
        [item], [], [], [], [],
        {(item.country, item.name): collection},
        {(item.country, item.name): processing},
        SETTINGS,
    )[0]
    assert row["access_status"] == "partial_transport_loss"
    assert row["blind_zone_status"] == "partial_transport_loss"
    alerts = social_monitor.source_access_alerts([row])
    assert len(alerts) == 1
    assert "partial_transport_loss" in alerts[0]


def test_update73_preserves_terminal_network_errno_in_telemetry_detail():
    error = ConnectionError(
        "HTTPSConnectionPool(host='www.orshanka.by', port=443): Max retries "
        "exceeded with url: /?p=109282 (Caused by NewConnectionError(" 
        "[Errno -3] Temporary failure in name resolution))"
    )
    detail = social_monitor.request_exception_detail(error)
    assert detail.startswith("ConnectionError:")
    assert "[Errno -3] Temporary failure in name resolution" in detail
