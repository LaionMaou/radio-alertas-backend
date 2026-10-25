from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from radio_alerts_backend.discord import DiscordNotifier

NOW = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return NOW + self.offset


class Recorder:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)

    def payloads(self) -> list[dict[str, str]]:
        return [json.loads(request.content) for request in self.requests]


async def test_disabled_notifier_makes_no_requests() -> None:
    recorder = Recorder()
    notifier = DiscordNotifier("", transport=httpx.MockTransport(recorder))

    await notifier("p2p_down", {"attempts": 10})
    await notifier.aclose()

    assert notifier.enabled is False
    assert recorder.requests == []


async def test_sends_once_and_respects_cooldown() -> None:
    recorder = Recorder()
    clock = Clock()
    notifier = DiscordNotifier(
        "https://discord.test/hook",
        transport=httpx.MockTransport(recorder),
        clock=clock,
    )

    await notifier("tts_failed", {"event_key": "quake:1"})
    await notifier("tts_failed", {"event_key": "quake:2"})
    clock.offset = timedelta(minutes=16)
    await notifier("tts_failed", {"event_key": "quake:3"})
    await notifier.aclose()

    assert len(recorder.requests) == 2
    assert "quake:1" in recorder.payloads()[0]["content"]
    assert "quake:3" in recorder.payloads()[1]["content"]


async def test_retries_three_times_with_backoff() -> None:
    recorder = Recorder(status=500)
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    notifier = DiscordNotifier(
        "https://discord.test/hook",
        transport=httpx.MockTransport(recorder),
        sleep=sleep,
        clock=Clock(),
    )

    await notifier("p2p_down", {"attempts": 10})
    await notifier.aclose()

    assert len(recorder.requests) == 3
    assert delays == [1.5, 3.0]


async def test_message_is_clipped() -> None:
    recorder = Recorder()
    notifier = DiscordNotifier(
        "https://discord.test/hook",
        transport=httpx.MockTransport(recorder),
        clock=Clock(),
    )

    await notifier("tts_failed", {"error": "x" * 5000})
    await notifier.aclose()

    content = recorder.payloads()[0]["content"]
    assert len(content) <= 1900
    assert content.endswith("…")
