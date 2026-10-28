from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from radio_alerts_backend.config import Settings
from radio_alerts_backend.models import Event, EventKind, TtsStatus
from radio_alerts_backend.store import EventStore
from radio_alerts_backend.tts_client import TtsClient

ISSUED = datetime(2026, 10, 25, 4, 5, tzinfo=UTC)


def settings(**kwargs) -> Settings:
    values = {
        "internal_token": "test-token",
        "tts_api_token": "tts-secret",
        "tts_api_url": "http://tts.test",
        "dry_run": False,
        "tts_result_timeout_seconds": 10,
    }
    values.update(kwargs)
    return Settings(**values)


def event(event_key: str = "quake:1", **kwargs) -> Event:
    values = {
        "epicenter": {"name": "宮城県沖", "magnitude": 7.2},
        "max_intensity": {"value": 50, "label": "5強"},
        "title": "地震情報",
    }
    values.update(kwargs)
    kind = values.pop("kind", EventKind.EARTHQUAKE)
    return Event(
        event_key=event_key,
        kind=kind,
        issued_at=ISSUED,
        **values,
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay


class Notifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, reason: str, fields: dict[str, Any]) -> None:
        self.calls.append((reason, fields))


class Recorder:
    def __init__(self, responses) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        value = self.responses[key]
        if isinstance(value, list):
            return value.pop(0)
        return value

    def payloads(self) -> list[dict[str, Any]]:
        import json

        return [
            json.loads(request.content)
            for request in self.requests
            if request.method == "POST"
        ]


async def make_store(tmp_path, *events: Event) -> EventStore:
    store = EventStore(tmp_path)
    for item in events:
        await store.upsert(item)
    return store


async def test_dry_run_makes_no_http_calls(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    notifier = Notifier()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no debe haber HTTP en dry-run")

    client = TtsClient(
        settings(dry_run=True),
        store,
        transport=httpx.MockTransport(handler),
        notifier=notifier,
    )
    outcome = await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.aclose()

    assert outcome.status == TtsStatus.DRY_RUN
    assert store.get(item.event_key).tts["status"] == "dry-run"
    assert notifier.calls == []


async def test_accepted_alert_is_polled_until_result(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    recorder = Recorder(
        {
            ("POST", "/alert"): httpx.Response(202, json={"alert_id": "a1", "status": "queued"}),
            ("GET", "/alert/a1"): httpx.Response(
                200, json={"status": "ok", "sent_audio": True, "error": None}
            ),
        }
    )
    client = TtsClient(settings(), store, transport=httpx.MockTransport(recorder))

    outcome = await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.wait_pending()
    await client.aclose()

    assert outcome.status == TtsStatus.QUEUED
    assert outcome.alert_id == "a1"
    assert store.get(item.event_key).tts["status"] == "ok"
    assert recorder.payloads()[0] == {
        "text": "地震情報。",
        "kind": "other",
        "magnitude": 7.2,
        "title": "地震情報",
        "dedupe_key": "k",
        "tone": "soft",
    }


async def test_major_tsunami_payload_asks_for_urgent_loop_and_preempt(tmp_path) -> None:
    item = event(event_key="tsunami:1", kind=EventKind.TSUNAMI, title="大津波警報")
    store = await make_store(tmp_path, item)
    recorder = Recorder(
        {
            ("POST", "/alert"): httpx.Response(202, json={"alert_id": "a1", "status": "queued"}),
            ("GET", "/alert/a1"): httpx.Response(200, json={"status": "stopped", "error": None}),
        }
    )
    client = TtsClient(settings(), store, transport=httpx.MockTransport(recorder))

    await client.submit(
        item,
        text="大津波警報が発表されました。",
        dedupe_key="k",
        tone="urgent",
        loop=True,
        preempt=True,
    )
    await client.wait_pending()
    await client.aclose()

    payload = recorder.payloads()[0]
    assert payload["tone"] == "urgent"
    assert payload["loop"] is True
    assert payload["preempt"] is True


async def test_duplicate_without_stored_result_keeps_duplicate_status(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    recorder = Recorder(
        {
            ("POST", "/alert"): httpx.Response(
                200, json={"alert_id": "new", "status": "duplicate", "duplicate_of": "orig"}
            ),
            ("GET", "/alert/orig"): httpx.Response(404, json={"detail": "missing"}),
        }
    )
    client = TtsClient(settings(), store, transport=httpx.MockTransport(recorder))

    outcome = await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.aclose()

    assert outcome.status == TtsStatus.DUPLICATE
    assert outcome.duplicate_of == "orig"
    assert store.get(item.event_key).tts["status"] == "duplicate"


async def test_duplicate_picks_up_result_still_in_memory(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    recorder = Recorder(
        {
            ("POST", "/alert"): httpx.Response(
                200, json={"alert_id": "new", "status": "duplicate", "duplicate_of": "orig"}
            ),
            ("GET", "/alert/orig"): httpx.Response(200, json={"status": "ok", "error": None}),
        }
    )
    client = TtsClient(settings(), store, transport=httpx.MockTransport(recorder))

    await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.aclose()

    assert store.get(item.event_key).tts["status"] == "ok"


async def test_busy_is_retried_with_backoff(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    clock = Clock()
    recorder = Recorder(
        {
            ("POST", "/alert"): [
                httpx.Response(429, json={"detail": "busy"}),
                httpx.Response(429, json={"detail": "busy"}),
                httpx.Response(429, json={"detail": "busy"}),
                httpx.Response(202, json={"alert_id": "a1", "status": "queued"}),
            ],
            ("GET", "/alert/a1"): httpx.Response(404, json={"detail": "missing"}),
        }
    )
    client = TtsClient(
        settings(),
        store,
        transport=httpx.MockTransport(recorder),
        sleep=clock.sleep,
        clock=clock,
    )

    outcome = await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.aclose()

    assert outcome.status == TtsStatus.QUEUED
    assert clock.delays[:3] == [1.0, 2.0, 4.0]


async def test_busy_after_all_retries_fails_and_notifies(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    clock = Clock()
    notifier = Notifier()
    recorder = Recorder(
        {("POST", "/alert"): httpx.Response(429, json={"detail": "busy"})}
    )
    client = TtsClient(
        settings(),
        store,
        transport=httpx.MockTransport(recorder),
        notifier=notifier,
        sleep=clock.sleep,
        clock=clock,
    )

    outcome = await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.aclose()

    assert outcome.status == TtsStatus.FAILED
    assert clock.delays == [1.0, 2.0, 4.0]
    assert len(recorder.requests) == 4
    assert store.get(item.event_key).tts["status"] == "failed"
    assert [call[0] for call in notifier.calls] == ["tts_failed"]


async def test_auth_failure_is_not_retried(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    notifier = Notifier()
    recorder = Recorder(
        {("POST", "/alert"): httpx.Response(401, json={"detail": "no"})}
    )
    client = TtsClient(
        settings(),
        store,
        transport=httpx.MockTransport(recorder),
        notifier=notifier,
    )

    outcome = await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.aclose()

    assert outcome.status == TtsStatus.FAILED
    assert len(recorder.requests) == 1
    assert len(notifier.calls) == 1
    assert "tts-secret" not in str(notifier.calls)


async def test_server_error_is_retried_once(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    clock = Clock()
    notifier = Notifier()
    recorder = Recorder(
        {
            ("POST", "/alert"): [
                httpx.Response(503, json={"detail": "down"}),
                httpx.Response(503, json={"detail": "down"}),
            ]
        }
    )
    client = TtsClient(
        settings(),
        store,
        transport=httpx.MockTransport(recorder),
        notifier=notifier,
        sleep=clock.sleep,
        clock=clock,
    )

    outcome = await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.aclose()

    assert outcome.status == TtsStatus.FAILED
    assert clock.delays == [2.0]
    assert len(recorder.requests) == 2


async def test_missing_result_becomes_unknown(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    clock = Clock()
    recorder = Recorder(
        {
            ("POST", "/alert"): httpx.Response(202, json={"alert_id": "a1", "status": "queued"}),
            ("GET", "/alert/a1"): httpx.Response(404, json={"detail": "missing"}),
        }
    )
    client = TtsClient(
        settings(),
        store,
        transport=httpx.MockTransport(recorder),
        sleep=clock.sleep,
        clock=clock,
    )

    await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.wait_pending()
    await client.aclose()

    assert store.get(item.event_key).tts["status"] == "unknown"


async def test_failed_result_notifies_once(tmp_path) -> None:
    item = event()
    store = await make_store(tmp_path, item)
    notifier = Notifier()
    recorder = Recorder(
        {
            ("POST", "/alert"): httpx.Response(202, json={"alert_id": "a1", "status": "queued"}),
            ("GET", "/alert/a1"): httpx.Response(
                200, json={"status": "failed", "error": "icecast down"}
            ),
        }
    )
    client = TtsClient(
        settings(),
        store,
        transport=httpx.MockTransport(recorder),
        notifier=notifier,
    )

    await client.submit(item, text="地震情報。", dedupe_key="k")
    await client.wait_pending()
    await client.aclose()

    assert store.get(item.event_key).tts["status"] == "failed"
    assert len(notifier.calls) == 1
    assert notifier.calls[0][1]["error"] == "icecast down"


class GatedTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.posts = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.posts += 1
            if self.posts == 1:
                self.started.set()
                await self.release.wait()
            return httpx.Response(
                202, json={"alert_id": f"a{self.posts}", "status": "queued"}
            )
        return httpx.Response(404, json={"detail": "missing"})


async def test_submits_are_serialized(tmp_path) -> None:
    first = event("quake:1")
    second = event("quake:2")
    store = await make_store(tmp_path, first, second)
    clock = Clock()
    transport = GatedTransport()
    client = TtsClient(
        settings(),
        store,
        transport=transport,
        sleep=clock.sleep,
        clock=clock,
    )

    task_one = asyncio.create_task(client.submit(first, text="一つ目。", dedupe_key="k1"))
    await transport.started.wait()
    task_two = asyncio.create_task(client.submit(second, text="二つ目。", dedupe_key="k2"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert transport.posts == 1

    transport.release.set()
    outcomes = await asyncio.gather(task_one, task_two)
    await client.aclose()

    assert transport.posts == 2
    assert [outcome.status for outcome in outcomes] == [
        TtsStatus.QUEUED,
        TtsStatus.QUEUED,
    ]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (EventKind.EEW, "eew"),
        (EventKind.TSUNAMI, "tsunami"),
        (EventKind.TYPHOON, "typhoon"),
        (EventKind.EARTHQUAKE, "other"),
    ],
)
async def test_kind_mapping(tmp_path, kind: EventKind, expected: str) -> None:
    item = event(event_key=f"{kind.value}:1", kind=kind)
    store = await make_store(tmp_path, item)
    recorder = Recorder(
        {
            ("POST", "/alert"): httpx.Response(202, json={"alert_id": "a1", "status": "queued"}),
            ("GET", "/alert/a1"): httpx.Response(200, json={"status": "ok", "error": None}),
        }
    )
    client = TtsClient(settings(), store, transport=httpx.MockTransport(recorder))

    await client.submit(item, text="本文。", dedupe_key="k")
    await client.wait_pending()
    await client.aclose()

    assert recorder.payloads()[0]["kind"] == expected
