from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from radio_alerts_backend.models import EventKind, EventStatus
from radio_alerts_backend.normalize import iso_z, tsunami_event_key
from radio_alerts_backend.p2p.parse import parse_message

FIXTURES = Path(__file__).parent / "fixtures" / "p2p"
RECEIVED = datetime(2026, 10, 25, 4, 5, 30, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_quake_551_is_normalized() -> None:
    result = parse_message(load("quake_551.json"), received_at=RECEIVED)

    assert result.ok
    event = result.event
    assert event is not None
    assert event.kind == EventKind.EARTHQUAKE
    assert event.event_key == "quake:5ee1681202add671a1e1ae39"
    assert event.issued_at == datetime(2026, 10, 25, 4, 4, tzinfo=UTC)
    assert event.origin_at == datetime(2026, 10, 25, 3, 59, tzinfo=UTC)
    assert event.max_intensity == {"value": 50, "label": "5強"}
    assert event.epicenter == {
        "name": "宮城県沖",
        "reduce_name": None,
        "lat": 38.1,
        "lon": 142.0,
        "depth_km": 50,
        "magnitude": 7.2,
    }
    assert event.tsunami == {"domestic": "None", "foreign": "Unknown"}
    assert event.summary == "宮城県沖 M7.2 最大震度5強"
    assert event.enrichment == {"jma_eid": None, "source": "p2p"}


def test_quake_551_uses_jma_eid_when_enriched() -> None:
    result = parse_message(
        load("quake_551.json"),
        received_at=RECEIVED,
        jma_eid="20261025125900",
    )
    assert result.event is not None
    assert result.event.event_key == "quake:20261025125900"
    assert result.event.enrichment == {
        "jma_eid": "20261025125900",
        "source": "jma",
    }


def test_quake_551_falls_back_to_point_scales() -> None:
    payload = {
        "code": 551,
        "id": "fallback",
        "time": "2026/10/25 13:04:06.958",
        "earthquake": {
            "time": "2026/10/25 12:59:00",
            "maxScale": -1,
            "hypocenter": {"name": "宮城県沖"},
        },
        "points": [{"scale": 45}, {"scale": 46}],
    }

    result = parse_message(payload, received_at=RECEIVED)

    assert result.event is not None
    assert result.event.max_intensity == {"value": 46, "label": "5弱以上"}


def test_eew_556_prefers_exact_scale_over_open_ended() -> None:
    result = parse_message(load("eew_556.json"), received_at=RECEIVED)

    assert result.ok
    event = result.event
    assert event is not None
    assert event.kind == EventKind.EEW
    assert event.event_key == "eew:20261025130518"
    assert event.issued_at == datetime(2026, 10, 25, 4, 5, 18, tzinfo=UTC)
    assert event.origin_at == datetime(2026, 10, 25, 4, 5, tzinfo=UTC)
    assert event.max_intensity == {"value": 50, "label": "5強"}
    assert "99" not in event.summary
    assert event.summary == "宮城県沖 最大震度5強（予想）"
    assert event.eew is not None
    assert event.eew["scale_to_max"] == 50
    assert event.eew["scale_from_max"] == 45
    assert event.eew["kind_code"] == "10"
    assert len(event.eew["areas"]) == 2
    assert event.eew["areas"][0]["scale_to_label"] == "5弱以上"
    assert event.eew["areas"][1]["scale_to_label"] == "5強"
    assert event.eew["areas"][1]["arrival_time"] is None


def test_eew_556_renders_open_ended_scale_as_weak_five_or_more() -> None:
    payload = load("eew_556.json")
    payload["areas"] = [payload["areas"][0]]

    result = parse_message(payload, received_at=RECEIVED)

    assert result.event is not None
    event = result.event
    assert event.max_intensity == {"value": 99, "label": "5弱以上"}
    assert "99" not in event.summary
    assert event.summary == "宮城県沖 最大震度5弱以上（予想）"
    assert event.eew is not None
    assert event.eew["scale_to_max"] == 99


def test_eew_cancelled_is_marked_without_areas() -> None:
    result = parse_message(load("eew_cancelled_556.json"), received_at=RECEIVED)

    assert result.event is not None
    event = result.event
    assert event.status == EventStatus.CANCELLED
    assert event.summary == "緊急地震速報は取り消されました。"
    assert event.max_intensity == {"value": None, "label": "不明"}
    assert event.eew is not None
    assert event.eew["areas"] == []
    assert event.eew["scale_to_max"] is None


def test_eew_test_is_ignored() -> None:
    result = parse_message(load("eew_test_556.json"), received_at=RECEIVED)
    assert result.event is None
    assert result.reason == "test"


def test_tsunami_552_is_normalized() -> None:
    result = parse_message(load("tsunami_552.json"), received_at=RECEIVED)

    assert result.ok
    event = result.event
    assert event is not None
    issued_at = datetime(2026, 10, 25, 4, 10, tzinfo=UTC)
    assert event.issued_at == issued_at
    assert event.event_key == tsunami_event_key(None, issued_at)
    assert event.kind == EventKind.TSUNAMI
    assert event.title == "津波警報"
    assert event.summary == "津波警報（宮城県、青森県太平洋沿岸）"
    assert event.tsunami is not None
    assert event.tsunami["grade"] == "Warning"
    assert event.tsunami["immediate"] is True
    assert event.tsunami["cancelled"] is False
    areas = event.tsunami["areas"]
    assert len(areas) == 2
    assert areas[0]["arrival_time"] is None
    assert areas[0]["condition"] == "津波到達中と推測"
    assert areas[0]["max_height"] == {"description": "３ｍ", "value": 3.0}
    assert areas[1]["arrival_time"] == iso_z(
        datetime(2026, 10, 25, 4, 20, tzinfo=UTC)
    )


def test_tsunami_cancelled_is_marked() -> None:
    result = parse_message(
        load("tsunami_cancelled_552.json"), received_at=RECEIVED
    )

    assert result.event is not None
    event = result.event
    assert event.status == EventStatus.CANCELLED
    assert event.title == "津波情報"
    assert event.summary == "津波予報は解除されました。"
    assert event.tsunami is not None
    assert event.tsunami["grade"] == "None"
    assert event.tsunami["areas"] == []


def test_unsupported_code_is_reported() -> None:
    result = parse_message({"code": 554, "type": "Full"}, received_at=RECEIVED)
    assert result.event is None
    assert result.reason == "unsupported:554"


def test_missing_fields_do_not_crash() -> None:
    result = parse_message({"code": 551}, received_at=RECEIVED)

    assert result.event is not None
    event = result.event
    assert event.issued_at == RECEIVED
    assert event.max_intensity == {"value": None, "label": "不明"}
    assert event.summary == "震源不明"
    assert event.kind == EventKind.EARTHQUAKE
