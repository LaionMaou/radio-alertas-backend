from __future__ import annotations

from datetime import timedelta
from typing import Any

from radio_alerts_backend.config import Settings
from radio_alerts_backend.jma.service import Enrichment
from radio_alerts_backend.models import Event, EventKind, EventStatus
from radio_alerts_backend.normalize import JST, utcnow
from radio_alerts_backend.pipeline import Pipeline
from radio_alerts_backend.policy import Policy
from radio_alerts_backend.store import EventStore
from radio_alerts_backend.tts_client import TtsClient


class FakeJMA:
    def __init__(
        self,
        *,
        quake: Enrichment | None = None,
        tsunami: Enrichment | None = None,
    ) -> None:
        self.quake = quake
        self.tsunami = tsunami
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def enrich_quake(self, **kwargs: Any) -> Enrichment | None:
        self.calls.append(("quake", kwargs))
        return self.quake

    async def enrich_tsunami(self, **kwargs: Any) -> Enrichment | None:
        self.calls.append(("tsunami", kwargs))
        return self.tsunami


def settings(tmp_path, **kwargs: Any) -> Settings:
    values = {
        "internal_token": "test-token",
        "data_dir": tmp_path / "data",
        "jma_enabled": False,
        "dry_run": True,
    }
    values.update(kwargs)
    return Settings(**values)


def p2p_time(value) -> str:
    return value.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S")


def quake_payload(
    *,
    p2p_id: str = "q1",
    max_scale: int = 50,
    magnitude: float = 7.2,
    origin=None,
    issue=None,
    lat: float = 38.1,
    lon: float = 142.0,
    name: str = "宮城県沖",
) -> dict[str, Any]:
    origin = origin or utcnow()
    issue = issue or origin
    return {
        "id": p2p_id,
        "code": 551,
        "time": p2p_time(issue),
        "issue": {"source": "気象庁", "time": p2p_time(issue), "type": "DetailScale"},
        "earthquake": {
            "time": p2p_time(origin),
            "hypocenter": {
                "name": name,
                "latitude": lat,
                "longitude": lon,
                "depth": 50,
                "magnitude": magnitude,
            },
            "maxScale": max_scale,
            "domesticTsunami": "None",
            "foreignTsunami": "Unknown",
        },
        "points": [],
    }


def tsunami_payload(*, issue=None, cancelled: bool = False) -> dict[str, Any]:
    issue = issue or utcnow()
    areas = (
        []
        if cancelled
        else [{"name": "宮城県", "grade": "Watch", "immediate": False}]
    )
    return {
        "id": "t1",
        "code": 552,
        "cancelled": cancelled,
        "issue": {"time": p2p_time(issue)},
        "areas": areas,
    }


async def make_pipeline(
    tmp_path,
    *,
    jma: FakeJMA | None = None,
    **kwargs: Any,
) -> tuple[Pipeline, EventStore, TtsClient]:
    config = settings(tmp_path, **kwargs)
    store = EventStore(tmp_path / "data")
    tts = TtsClient(config, store)
    pipeline = Pipeline(config, store, Policy(config), jma or FakeJMA(), tts)  # type: ignore[arg-type]
    return pipeline, store, tts


async def test_test_eew_is_ignored(tmp_path) -> None:
    pipeline, store, tts = await make_pipeline(tmp_path)
    payload = {"id": "test-1", "code": 556, "test": True, "earthquake": {}, "issue": {}}

    event = await pipeline.handle_p2p(payload)

    assert event is None
    assert store.status()["events"] == 0
    await tts.aclose()


async def test_quake_is_announced_and_recorded(tmp_path) -> None:
    pipeline, store, tts = await make_pipeline(tmp_path)

    event = await pipeline.handle_p2p(quake_payload())

    assert event is not None
    assert len(event.announcements) == 1
    assert event.announcements[0]["status"] == "dry-run"
    assert event.tts["status"] == "dry-run"
    assert event.status == EventStatus.ACTIVE
    stored = store.get(event.event_key)
    assert stored is not None
    assert stored.announcements[0]["rank"] == 5014
    await tts.aclose()


async def test_moderate_eew_uses_the_soft_lead_in(tmp_path) -> None:
    pipeline, _, tts = await make_pipeline(tmp_path)

    event = await pipeline.handle_p2p(quake_payload(p2p_id="q7", max_scale=60))

    assert event is not None
    assert event.announcements[-1]["tone"] == "soft"
    assert event.announcements[-1]["loop"] is False
    await tts.aclose()


async def test_seven_eew_uses_the_urgent_lead_in(tmp_path) -> None:
    pipeline, _, tts = await make_pipeline(tmp_path)

    event = await pipeline.handle_p2p(quake_payload(p2p_id="q8", max_scale=70))

    assert event is not None
    assert event.announcements[-1]["tone"] == "urgent"
    await tts.aclose()


async def test_major_tsunami_loops_until_an_operator_stops_it(tmp_path) -> None:
    pipeline, _, tts = await make_pipeline(tmp_path)
    payload = tsunami_payload()
    payload["areas"] = [{"name": "宮城県", "grade": "MajorWarning", "immediate": True}]

    event = await pipeline.handle_p2p(payload)

    assert event is not None
    assert event.announcements[-1]["tone"] == "urgent"
    assert event.announcements[-1]["loop"] is True
    await tts.aclose()


async def test_low_quake_is_stored_without_announcement(tmp_path) -> None:
    pipeline, store, tts = await make_pipeline(tmp_path)

    event = await pipeline.handle_p2p(
        quake_payload(max_scale=40, magnitude=5.0, p2p_id="q2")
    )

    assert event is not None
    assert event.announcements == []
    assert event.tts["status"] == "none"
    assert store.get(event.event_key) is not None
    await tts.aclose()


async def test_stale_event_is_not_announced(tmp_path) -> None:
    pipeline, _, tts = await make_pipeline(tmp_path)
    old = utcnow() - timedelta(hours=1)

    event = await pipeline.handle_p2p(
        quake_payload(p2p_id="q3", origin=old, issue=old)
    )

    assert event is not None
    assert event.status == EventStatus.STALE
    assert event.announcements == []
    await tts.aclose()


async def test_cancelled_tsunami_is_stored_without_audio(tmp_path) -> None:
    pipeline, store, tts = await make_pipeline(tmp_path)

    event = await pipeline.handle_p2p(tsunami_payload(cancelled=True))

    assert event is not None
    assert event.status == EventStatus.CANCELLED
    assert event.announcements == []
    assert store.get(event.event_key) is not None
    await tts.aclose()


async def test_jma_enrichment_changes_key_and_fills_limits(tmp_path) -> None:
    jma = FakeJMA(
        quake=Enrichment(
            eid="20261025130518",
            maxi=50,
            magnitude=7.3,
            detail="https://www.jma.go.jp/detail.json",
        )
    )
    pipeline, store, tts = await make_pipeline(tmp_path, jma=jma, jma_enabled=True)

    event = await pipeline.handle_p2p(
        quake_payload(p2p_id="q4", max_scale=-1, magnitude=-1)
    )

    assert event is not None
    assert event.event_key == "quake:20261025130518"
    assert event.max_intensity == {"value": 50, "label": "5強"}
    assert event.epicenter["magnitude"] == 7.3
    assert event.enrichment["jma_eid"] == "20261025130518"
    assert len(event.announcements) == 1
    assert jma.calls[0][0] == "quake"
    assert store.get("quake:20261025130518") is not None
    await tts.aclose()


async def test_quake_correlating_with_announced_eew_escalates_same_event(tmp_path) -> None:
    pipeline, store, tts = await make_pipeline(tmp_path)
    origin = utcnow()
    eew = Event(
        event_key="eew:20261025130518",
        kind=EventKind.EEW,
        issued_at=origin,
        origin_at=origin,
        epicenter={"name": "宮城県沖", "lat": 38.1, "lon": 142.0, "magnitude": 7.2},
        max_intensity={"value": 45, "label": "5弱"},
        announcements=[
            {"version": 1, "rank": 4514, "alert_id": "a1", "status": "ok", "at": "x"}
        ],
    )
    await store.upsert(eew)

    event = await pipeline.handle_p2p(
        quake_payload(p2p_id="q5", origin=origin, issue=origin, max_scale=50)
    )

    assert event is not None
    assert event.event_key == eew.event_key
    assert event.kind == EventKind.EEW
    assert event.max_intensity["value"] == 50
    assert len(event.announcements) == 2
    assert event.announcements[-1]["rank"] == 5014
    assert event.enrichment["quake"]["p2p_id"] == "q5"
    assert store.get("quake:q5") is None
    await tts.aclose()


async def test_quake_without_announced_eew_stays_independent(tmp_path) -> None:
    pipeline, store, tts = await make_pipeline(tmp_path)
    origin = utcnow()
    eew = Event(
        event_key="eew:20261025130518",
        kind=EventKind.EEW,
        issued_at=origin,
        origin_at=origin,
        epicenter={"name": "宮城県沖", "lat": 38.1, "lon": 142.0, "magnitude": 7.2},
        max_intensity={"value": 45, "label": "5弱"},
    )
    await store.upsert(eew)

    event = await pipeline.handle_p2p(
        quake_payload(p2p_id="q6", origin=origin, issue=origin, max_scale=50)
    )

    assert event is not None
    assert event.kind == EventKind.EARTHQUAKE
    assert event.event_key == "quake:q6"
    assert len(event.announcements) == 1
    assert store.get(eew.event_key).announcements == []
    await tts.aclose()
