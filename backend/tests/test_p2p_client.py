from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from radio_alerts_backend.p2p.client import P2PClient


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.closed = False

    async def __aenter__(self) -> FakeWebSocket:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        self.closed = True
        return False

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def close(self) -> None:
        self.closed = True


class BlockingWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> BlockingWebSocket:
        await asyncio.sleep(3600)
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def close(self) -> None:
        self.closed = True


def fake_connect(messages: list[str]):
    def connect(*args: Any, **kwargs: Any) -> FakeWebSocket:
        return FakeWebSocket(messages)

    return connect


def make_client(
    handler,
    *,
    connect=None,
    sleep=asyncio.sleep,
    clock=None,
    codes=(551, 552, 556),
    **kwargs: Any,
) -> P2PClient:
    options: dict[str, Any] = {
        "backoff_initial": 1.0,
        "backoff_factor": 2.0,
        "backoff_max": 30.0,
        "backoff_jitter": 0.0,
        "random_fn": lambda low, high: 0.0,
    }
    options.update(kwargs)
    return P2PClient(
        "wss://example.invalid/v2/ws",
        frozenset(codes),
        handler,
        connect=connect or fake_connect([]),
        sleep=sleep,
        clock=clock or (lambda: datetime(2026, 10, 25, 4, 0, tzinfo=UTC)),
        **options,
    )


async def test_duplicates_and_unsubscribed_codes_are_filtered() -> None:
    delivered: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> None:
        delivered.append(payload)

    client = make_client(handler, codes={551})
    quake = json.dumps({"code": 551, "id": "same-id"})

    assert await client.handle_raw(quake) is True
    assert await client.handle_raw(quake) is False
    assert await client.handle_raw(json.dumps({"code": 552, "id": "other"})) is False
    assert await client.handle_raw("not json") is False
    assert await client.handle_raw(json.dumps([1, 2, 3])) is False
    assert await client.handle_raw(json.dumps({"code": 551, "id": "new"})) is True

    assert [payload["id"] for payload in delivered] == ["same-id", "new"]
    assert client.status.last_message_at is not None


async def test_frames_without_id_are_deduplicated_by_content() -> None:
    delivered: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> None:
        delivered.append(payload)

    client = make_client(handler, connect=fake_connect([]))
    frame = json.dumps({"code": 556, "issue": {"eventId": "abc"}})

    assert await client.handle_raw(frame) is True
    assert await client.handle_raw(frame) is False
    assert len(delivered) == 1


async def test_handler_errors_do_not_close_the_connection() -> None:
    calls: list[int] = []

    async def handler(payload: dict[str, Any]) -> None:
        calls.append(1)
        raise RuntimeError("handler failed")

    client = make_client(handler, connect=fake_connect([]))

    assert await client.handle_raw(json.dumps({"code": 551, "id": "1"})) is False
    assert calls == [1]
    assert client.status.connected is False


async def test_run_session_consumes_messages_and_reports_state() -> None:
    delivered: list[dict[str, Any]] = []
    states: list[bool] = []

    async def handler(payload: dict[str, Any]) -> None:
        delivered.append(payload)

    async def on_state(connected: bool) -> None:
        states.append(connected)

    messages = [
        json.dumps({"code": 551, "id": "a"}),
        json.dumps({"code": 551, "id": "b"}),
    ]
    client = make_client(
        handler,
        connect=fake_connect(messages),
        on_connection_change=on_state,
    )

    await client.run_session()

    assert [payload["id"] for payload in delivered] == ["a", "b"]
    assert states == [True, False]
    assert client.status.connected is False
    assert client.status.connected_since is None


async def test_connection_failure_is_recorded() -> None:
    async def handler(payload: dict[str, Any]) -> None:
        return None

    def failing_connect(*args: Any, **kwargs: Any):
        raise RuntimeError("connection refused")

    client = make_client(handler, connect=failing_connect)

    await client.run_session()

    assert client.status.connected is False
    assert client.status.last_error == "connection refused"
    assert client.status.attempts == 0


async def test_backoff_grows_and_alerts_once() -> None:
    delays: list[float] = []
    alerts: list[int] = []
    client: P2PClient

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 5:
            client.request_stop()

    async def on_attempts(attempts: int) -> None:
        alerts.append(attempts)

    def failing_connect(*args: Any, **kwargs: Any):
        raise RuntimeError("down")

    async def handler(payload: dict[str, Any]) -> None:
        return None

    client = make_client(
        handler,
        connect=failing_connect,
        sleep=fake_sleep,
        alert_after_attempts=3,
        on_attempts_exceeded=on_attempts,
    )

    await asyncio.wait_for(client.run(), 2)

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert alerts == [3]
    assert client.status.attempts == 5


async def test_backoff_is_capped() -> None:
    delays: list[float] = []
    client: P2PClient

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 4:
            client.request_stop()

    def failing_connect(*args: Any, **kwargs: Any):
        raise RuntimeError("down")

    async def handler(payload: dict[str, Any]) -> None:
        return None

    client = make_client(
        handler,
        connect=failing_connect,
        sleep=fake_sleep,
        backoff_max=5.0,
    )

    await asyncio.wait_for(client.run(), 2)

    assert delays == [1.0, 2.0, 4.0, 5.0]


async def test_healthy_connection_resets_backoff() -> None:
    now = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=61)])

    async def handler(payload: dict[str, Any]) -> None:
        return None

    client = make_client(
        handler,
        connect=fake_connect([]),
        clock=lambda: next(times),
    )
    client.status.attempts = 5
    client._alerted = True

    await client.run_session()

    assert client.status.attempts == 0
    assert client._alerted is False


async def test_start_is_idempotent_and_stop_closes() -> None:
    async def handler(payload: dict[str, Any]) -> None:
        return None

    def blocking_connect(*args: Any, **kwargs: Any) -> BlockingWebSocket:
        return BlockingWebSocket()

    client = make_client(handler, connect=blocking_connect)

    first = client.start()
    second = client.start()

    assert first is second
    await client.stop()

    assert client.status.connected is False
    assert client._task is None
