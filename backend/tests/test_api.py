from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from radio_alerts_backend.api.sse import event_stream
from radio_alerts_backend.app import create_app
from radio_alerts_backend.config import Settings
from radio_alerts_backend.models import Event, EventKind
from radio_alerts_backend.services import Services

TOKEN = "internal-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ISSUED = datetime(2026, 10, 25, 4, 5, tzinfo=UTC)


def settings(tmp_path: Any, **kwargs: Any) -> Settings:
    values = {
        "internal_token": TOKEN,
        "data_dir": tmp_path / "data",
        "log_dir": tmp_path / "logs",
        "jma_enabled": False,
        "dry_run": True,
    }
    values.update(kwargs)
    return Settings(**values)


def make_app(tmp_path: Any, **kwargs: Any) -> FastAPI:
    return create_app(settings(tmp_path, **kwargs), start_background=False)


def quake(event_key: str = "quake:1", *, issued_at: datetime = ISSUED) -> Event:
    return Event(
        event_key=event_key,
        kind=EventKind.EARTHQUAKE,
        issued_at=issued_at,
        title="地震情報",
        summary="宮城県沖 M7.2 最大震度5強",
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 50, "label": "5強"},
        tsunami={"domestic": "None"},
    )


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncIterator[tuple[AsyncClient, Services]]:
    services = app.state.services
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, services
    await services.tts.aclose()
    await services.jma.aclose()
    if services.notifier is not None:
        await services.notifier.aclose()


async def test_health_and_empty_endpoints(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, _):
        health = await client.get("/health")
        latest = await client.get("/api/alert/latest")
        history = await client.get("/api/alert/history")

    assert health.status_code == 200
    assert health.json()["dry_run"] is True
    assert health.json()["p2p"]["enabled"] is False
    assert latest.json() == {"alert": None}
    assert history.json() == {"alerts": [], "count": 0}


async def test_latest_and_history(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, services):
        older = datetime(2026, 10, 25, tzinfo=UTC)
        newer = datetime(2026, 10, 26, tzinfo=UTC)
        await services.store.upsert(quake("quake:old", issued_at=older))
        await services.store.upsert(quake("quake:new", issued_at=newer))
        await services.store.upsert(
            Event(
                event_key="tsunami:1",
                kind=EventKind.TSUNAMI,
                issued_at=datetime(2026, 10, 27, tzinfo=UTC),
                title="津波注意報",
            )
        )

        latest = await client.get("/api/alert/latest", params={"kind": "earthquake"})
        history = await client.get("/api/alert/history", params={"limit": 2})
        filtered = await client.get("/api/alert/history", params={"kind": "tsunami"})

    assert latest.json()["alert"]["event_key"] == "quake:new"
    assert history.json()["count"] == 2
    assert history.json()["alerts"][0]["event_key"] == "tsunami:1"
    assert filtered.json()["count"] == 1
    assert filtered.json()["alerts"][0]["kind"] == "tsunami"


async def test_internal_auth_and_missing_event(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, _):
        unnamed = await client.post("/internal/trigger-tts", json={"text": "test"})
        wrong = await client.post(
            "/internal/trigger-tts",
            json={"text": "test"},
            headers={"Authorization": "Bearer nope"},
        )
        missing = await client.post(
            "/internal/trigger-tts",
            json={"event_key": "quake:none"},
            headers=AUTH,
        )
        empty = await client.post("/internal/trigger-tts", json={}, headers=AUTH)

    assert unnamed.status_code == 401
    assert wrong.status_code == 401
    assert missing.status_code == 404
    assert empty.status_code == 422


async def test_manual_trigger_respects_dry_run(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, services):
        response = await client.post(
            "/internal/trigger-tts",
            json={"text": "手動テストです。", "kind": "eew", "title": "手動"},
            headers=AUTH,
        )
        body = response.json()
        stored = services.store.get(body["event_key"])

    assert response.status_code == 200
    assert body["status"] == "dry-run"
    assert body["text"] == "手動テストです。"
    assert stored is not None
    assert stored.kind == EventKind.EEW
    assert stored.tts["status"] == "dry-run"


async def test_trigger_existing_event_uses_rendered_text(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, services):
        item = quake()
        await services.store.upsert(item)
        response = await client.post(
            "/internal/trigger-tts",
            json={"event_key": item.event_key},
            headers=AUTH,
        )
        stored = services.store.get(item.event_key)

    body = response.json()
    assert response.status_code == 200
    assert body["text"].startswith("地震情報。")
    assert stored is not None
    assert stored.tts["status"] == "dry-run"


async def test_stop_tts_requires_the_internal_token(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, _):
        unnamed = await client.post("/internal/stop-tts")
        wrong = await client.post(
            "/internal/stop-tts", headers={"Authorization": "Bearer nope"}
        )

    assert unnamed.status_code == 401
    assert wrong.status_code == 401


async def test_stop_tts_clears_the_loop_and_publishes(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, services):
        item = await services.store.upsert(
            Event(
                event_key="tsunami:1",
                kind=EventKind.TSUNAMI,
                issued_at=ISSUED,
                title="大津波警報",
            )
        )
        await services.store.update_tts(item.event_key, {"loop_active": True})

        async def fake_stop() -> dict[str, Any]:
            return {"stopped": True, "status": "stopping", "error": None}

        services.tts.stop = fake_stop  # type: ignore[method-assign]
        async with services.bus.subscribe() as subscription:
            response = await client.post("/internal/stop-tts", headers=AUTH)
            message = await asyncio.wait_for(subscription.queue.get(), 2)
        stored = services.store.get(item.event_key)

    assert response.status_code == 200
    assert response.json() == {"stopped": True, "status": "stopping", "cleared": 1}
    assert message.data["tts"]["loop_active"] is False
    assert stored is not None
    assert stored.tts["loop_active"] is False


async def test_stop_tts_keeps_the_loop_when_the_chain_fails(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, services):
        item = await services.store.upsert(
            Event(
                event_key="tsunami:1",
                kind=EventKind.TSUNAMI,
                issued_at=ISSUED,
                title="大津波警報",
            )
        )
        await services.store.update_tts(item.event_key, {"loop_active": True})

        async def failing_stop() -> dict[str, Any]:
            return {"stopped": False, "status": None, "error": "ConnectError: down"}

        services.tts.stop = failing_stop  # type: ignore[method-assign]
        response = await client.post("/internal/stop-tts", headers=AUTH)
        stored = services.store.get(item.event_key)

    assert response.status_code == 502
    assert stored is not None
    assert stored.tts["loop_active"] is True


async def test_stop_tts_in_dry_run_is_idle_and_clears_nothing(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, _):
        response = await client.post("/internal/stop-tts", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"stopped": False, "status": "idle", "cleared": 0}


async def test_simulate_is_hidden_by_default(tmp_path) -> None:
    app = make_app(tmp_path)
    async with client_for(app) as (client, _):
        response = await client.post(
            "/internal/simulate",
            json={"fixture": "eew"},
            headers=AUTH,
        )

    assert response.status_code == 404


async def test_simulate_eew_announces_once(tmp_path) -> None:
    app = make_app(tmp_path, enable_simulate=True)
    async with client_for(app) as (client, services):
        first = await client.post(
            "/internal/simulate", json={"fixture": "eew"}, headers=AUTH
        )
        second = await client.post(
            "/internal/simulate", json={"fixture": "eew"}, headers=AUTH
        )

    event = first.json()["event"]
    assert first.status_code == 200
    assert event["kind"] == "eew"
    assert event["source"] == "simulate"
    assert len(event["announcements"]) == 1
    assert event["announcements"][0]["status"] == "dry-run"
    assert event["tts"]["status"] == "dry-run"
    assert len(second.json()["event"]["announcements"]) == 1


async def test_simulate_typhoon_with_tts_area(tmp_path) -> None:
    app = make_app(
        tmp_path,
        enable_simulate=True,
        typhoon_ann_tts=True,
        typhoon_areas="130000",
    )
    async with client_for(app) as (client, _):
        response = await client.post(
            "/internal/simulate", json={"fixture": "typhoon"}, headers=AUTH
        )

    event = response.json()["event"]
    assert response.status_code == 200
    assert event["kind"] == "typhoon"
    assert event["typhoon"]["number"] == "2631"
    assert len(event["announcements"]) == 1
    assert event["announcements"][0]["rank"] == 22


async def _close(services: Services) -> None:
    await services.tts.aclose()
    await services.jma.aclose()
    if services.notifier is not None:
        await services.notifier.aclose()


async def test_sse_snapshot_replay_and_live_status(tmp_path) -> None:
    app = make_app(tmp_path)
    services = app.state.services
    await services.store.upsert(quake())

    async def connected() -> bool:
        return False

    stream = event_stream(services, connected, last_event_id=0)
    prelude = await anext(stream)
    snapshot = await anext(stream)
    replay = await anext(stream)
    await services.bus.publish("status", {"connected": True})
    live = await asyncio.wait_for(anext(stream), timeout=2)
    await stream.aclose()
    await _close(services)

    assert "retry: 5000" in prelude
    assert "event: snapshot" in snapshot
    assert "quake:1" in snapshot
    assert "event: alert" in replay
    assert "event: status" in live
    assert '"connected":true' in live


async def test_sse_heartbeat(tmp_path) -> None:
    app = make_app(tmp_path)
    services = app.state.services

    async def connected() -> bool:
        return False

    stream = event_stream(services, connected, heartbeat_seconds=0.01)
    await anext(stream)
    await anext(stream)
    ping = await asyncio.wait_for(anext(stream), timeout=2)
    await stream.aclose()
    await _close(services)

    assert ping == ": ping\n\n"
