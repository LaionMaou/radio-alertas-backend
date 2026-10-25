from __future__ import annotations

from datetime import timedelta

import pytest

from radio_alerts_backend.models import (
    Event,
    EventKind,
    EventStatus,
    TtsStatus,
    default_tts,
    make_announcement,
)
from radio_alerts_backend.normalize import parse_iso, utcnow


def make_event(**overrides: object) -> Event:
    values: dict[str, object] = {
        "event_key": "quake:1",
        "kind": EventKind.EARTHQUAKE,
        "issued_at": utcnow(),
    }
    values.update(overrides)
    return Event(**values)  # type: ignore[arg-type]


def test_public_view_hides_internal_fields() -> None:
    event = make_event(rank=514, raw_id="p2p-1", seq=7)

    public = event.to_public()

    assert "rank" not in public
    assert "raw_id" not in public
    assert public["seq"] == 7
    assert public["kind"] == "earthquake"
    assert public["source"] == "p2p"


def test_record_includes_rank_and_round_trips() -> None:
    issued = utcnow().replace(microsecond=0)
    event = make_event(
        issued_at=issued,
        origin_at=issued - timedelta(minutes=2),
        rank=514,
        raw_id="p2p-1",
        seq=7,
        status=EventStatus.UPDATED,
        version=3,
        title="地震情報",
        max_intensity={"value": 50, "label": "5強"},
        announcements=[
            make_announcement(
                version=1, rank=514, alert_id="a-1", status="ok", at=issued
            )
        ],
        tts={"status": "ok", "alert_id": "a-1", "error": None, "updated_at": None},
    )

    restored = Event.from_record(event.to_record())

    assert restored.rank == 514
    assert restored.raw_id == "p2p-1"
    assert restored.version == 3
    assert restored.status == EventStatus.UPDATED
    assert restored.origin_at == event.origin_at
    assert restored.announced_rank == 514
    assert restored.tts["alert_id"] == "a-1"
    assert parse_iso(restored.to_public()["issued_at"]) == issued.replace(microsecond=0)


def test_test_status_is_not_restored_as_test() -> None:
    restored = Event.from_record(make_event(status=EventStatus.TEST).to_record())
    assert restored.status == EventStatus.ACTIVE


def test_unknown_kind_is_rejected() -> None:
    record = make_event().to_record()
    record["kind"] = "meteor"
    with pytest.raises(ValueError):
        Event.from_record(record)


def test_announced_rank_uses_the_highest_announcement() -> None:
    event = make_event(
        announcements=[
            {"version": 1, "rank": 45},
            {"version": 2, "rank": 60},
            {"version": "bad", "rank": "bad"},
        ]
    )

    assert event.announced_rank == 60


def test_expiry_depends_on_kind() -> None:
    issued = utcnow()
    quake = make_event(issued_at=issued)
    typhoon = make_event(issued_at=issued, kind=EventKind.TYPHOON)

    assert quake.expires_at() == issued + timedelta(hours=24)
    assert typhoon.expires_at() == issued + timedelta(hours=12)
    assert quake.is_expired(issued + timedelta(hours=23)) is False
    assert quake.is_expired(issued + timedelta(hours=25)) is True


def test_default_tts_returns_a_fresh_mapping() -> None:
    first = default_tts()
    second = default_tts()
    first["status"] = TtsStatus.OK.value
    assert second["status"] == TtsStatus.NONE.value


def test_announcement_serializes_timestamp() -> None:
    at = utcnow()
    item = make_announcement(version=2, rank=60, alert_id="a-2", status="ok", at=at)
    assert item["at"].endswith("Z")
    assert item["version"] == 2
    assert item["rank"] == 60
    assert item["alert_id"] == "a-2"
