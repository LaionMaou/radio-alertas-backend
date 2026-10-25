from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from radio_alerts_backend.bus import EventBus
from radio_alerts_backend.models import (
    Event,
    EventKind,
    EventStatus,
    make_announcement,
)
from radio_alerts_backend.normalize import utcnow
from radio_alerts_backend.store import EventStore


def make_event(
    key: str = "quake:test",
    *,
    kind: EventKind = EventKind.EARTHQUAKE,
    issued_at: datetime | None = None,
    rank: int = 0,
    version: int = 1,
    **kwargs: Any,
) -> Event:
    return Event(
        event_key=key,
        kind=kind,
        issued_at=issued_at or utcnow(),
        rank=rank,
        version=version,
        **kwargs,
    )


async def test_upsert_assigns_sequence_and_publishes(tmp_path) -> None:
    bus = EventBus()
    store = EventStore(tmp_path, bus=bus)

    async with bus.subscribe() as subscription:
        event = await store.upsert(make_event())
        message = await asyncio.wait_for(subscription.queue.get(), 1)

    assert event.seq == 1
    assert message.seq == 1
    assert message.type == "alert"
    assert message.data["event_key"] == "quake:test"
    assert message.data["seq"] == 1


async def test_update_increments_version_and_preserves_state(tmp_path) -> None:
    store = EventStore(tmp_path)
    event = await store.upsert(make_event(rank=50))
    await store.record_announcement(
        event.event_key,
        make_announcement(version=1, rank=50, alert_id="alert-1", status="ok"),
    )
    await store.update_tts(event.event_key, {"status": "ok", "alert_id": "alert-1"})

    updated = await store.upsert(make_event(rank=50, version=1))

    assert updated.version == 2
    assert updated.announced_rank == 50
    assert updated.tts["status"] == "ok"
    assert len(updated.announcements) == 1


async def test_rank_never_goes_backwards(tmp_path) -> None:
    store = EventStore(tmp_path)
    await store.upsert(make_event(rank=50))

    updated = await store.upsert(make_event(rank=40))

    assert updated.rank == 50


async def test_latest_and_history_are_sorted_by_issue_time(tmp_path) -> None:
    store = EventStore(tmp_path)
    now = utcnow()
    await store.upsert(make_event("quake:a", issued_at=now - timedelta(hours=2)))
    await store.upsert(make_event("quake:b", issued_at=now - timedelta(hours=1)))
    await store.upsert(make_event("quake:c", issued_at=now))

    assert store.latest().event_key == "quake:c"
    assert [item["event_key"] for item in store.history(2)] == ["quake:c", "quake:b"]
    assert store.latest(EventKind.EEW) is None


async def test_latest_marks_expired_events_without_mutating_status(tmp_path) -> None:
    store = EventStore(tmp_path)
    event = await store.upsert(
        make_event(issued_at=utcnow() - timedelta(hours=25))
    )

    public = store.latest_public()

    assert public is not None
    assert public["status"] == EventStatus.EXPIRED.value
    assert event.status == EventStatus.ACTIVE


async def test_history_limit_evicts_the_oldest_event(tmp_path) -> None:
    store = EventStore(tmp_path, history_limit=2)
    now = utcnow()
    await store.upsert(make_event("quake:a", issued_at=now - timedelta(hours=2)))
    await store.upsert(make_event("quake:b", issued_at=now - timedelta(hours=1)))
    await store.upsert(make_event("quake:c", issued_at=now))

    assert store.get("quake:a") is None
    assert store.status()["events"] == 2


async def test_replay_restores_announcements_and_tts(tmp_path) -> None:
    store = EventStore(tmp_path)
    event = await store.upsert(make_event(rank=60))
    await store.record_announcement(
        event.event_key,
        make_announcement(version=1, rank=60, alert_id="alert-1", status="ok"),
    )
    await store.update_tts(
        event.event_key,
        {"status": "ok", "alert_id": "alert-1", "updated_at": "2026-10-25T04:00:00Z"},
    )
    last_seq = store.seq

    replayed = EventStore(tmp_path)
    count = await replayed.load()
    restored = replayed.get(event.event_key)

    assert count == 1
    assert restored is not None
    assert restored.rank == 60
    assert restored.announced_rank == 60
    assert restored.tts["alert_id"] == "alert-1"
    assert restored.seq == last_seq
    assert replayed.seq == last_seq


async def test_find_related_eew_uses_time_and_distance(tmp_path) -> None:
    store = EventStore(tmp_path)
    now = utcnow()
    eew = await store.upsert(
        make_event(
            "eew:1",
            kind=EventKind.EEW,
            issued_at=now,
            origin_at=now - timedelta(minutes=5),
            epicenter={"lat": 38.1, "lon": 142.0, "name": "宮城県沖"},
        )
    )

    assert (
        store.find_related_eew(now - timedelta(minutes=4), 38.2, 142.1) is eew
    )
    assert store.find_related_eew(now - timedelta(minutes=4), 33.0, 130.0) is None
    assert (
        store.find_related_eew(now - timedelta(minutes=40), 38.1, 142.0) is None
    )


async def test_find_related_eew_ignores_cancelled_and_expired(tmp_path) -> None:
    store = EventStore(tmp_path)
    now = utcnow()
    await store.upsert(
        make_event(
            "eew:cancelled",
            kind=EventKind.EEW,
            issued_at=now,
            origin_at=now,
            status=EventStatus.CANCELLED,
            epicenter={"lat": 38.1, "lon": 142.0},
        )
    )
    await store.upsert(
        make_event(
            "eew:expired",
            kind=EventKind.EEW,
            issued_at=now - timedelta(hours=25),
            origin_at=now,
            epicenter={"lat": 38.1, "lon": 142.0},
        )
    )

    assert store.find_related_eew(now, 38.1, 142.0) is None


async def test_cleanup_removes_files_older_than_retention(tmp_path) -> None:
    (tmp_path / "events-2020-01-01.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "events-2026-10-25.jsonl").write_text("", encoding="utf-8")
    store = EventStore(tmp_path, history_days=30)

    removed = await store.cleanup(now=datetime(2026, 10, 25, tzinfo=UTC))

    assert removed == 1
    assert not (tmp_path / "events-2020-01-01.jsonl").exists()
    assert (tmp_path / "events-2026-10-25.jsonl").exists()


async def test_snapshot_contains_latest_and_history(tmp_path) -> None:
    store = EventStore(tmp_path)
    await store.upsert(make_event("quake:a"))

    snapshot = store.snapshot()

    assert snapshot["seq"] == 1
    assert snapshot["alert"]["event_key"] == "quake:a"
    assert len(snapshot["alerts"]) == 1
