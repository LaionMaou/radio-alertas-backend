from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from radio_alerts_backend.jma.client import JMAClient

START = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_client(handler, clock: Clock, **kwargs) -> JMAClient:
    return JMAClient(
        user_agent="KumaRadioAlerts/0.1 (+https://example.test)",
        transport=httpx.MockTransport(handler),
        clock=clock,
        **kwargs,
    )


async def test_response_is_cached_until_ttl() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="ok")

    clock = Clock()
    client = make_client(handler, clock)

    assert await client.get_text("https://example.test/a", ttl=60) == "ok"
    clock.advance(30)
    assert await client.get_text("https://example.test/a", ttl=60) == "ok"
    assert calls == ["https://example.test/a"]


async def test_rate_limit_returns_stale_cache_and_blocks_requests() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=f"call-{len(calls)}")

    clock = Clock()
    client = make_client(handler, clock, min_interval_seconds=300)

    assert await client.get_text("https://example.test/a", ttl=60) == "call-1"
    clock.advance(120)
    assert await client.get_text("https://example.test/a", ttl=60) == "call-1"
    clock.advance(200)
    assert await client.get_text("https://example.test/a", ttl=60) == "call-2"
    assert len(calls) == 2


async def test_network_error_falls_back_to_stale_cache() -> None:
    clock = Clock()
    state = {"fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, text="cached")

    client = make_client(handler, clock, min_interval_seconds=1)

    assert await client.get_text("https://example.test/a", ttl=10) == "cached"
    state["fail"] = True
    clock.advance(20)
    assert await client.get_text("https://example.test/a", ttl=10) == "cached"


async def test_network_error_without_cache_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = make_client(handler, Clock())

    assert await client.get_text("https://example.test/a", ttl=10) is None


async def test_http_error_without_cache_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = make_client(handler, Clock())

    assert await client.get_text("https://example.test/a", ttl=10) is None


async def test_json_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"eid": "1"})

    client = make_client(handler, Clock())

    assert await client.get_json("https://example.test/a.json", ttl=10) == {"eid": "1"}


async def test_invalid_json_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = make_client(handler, Clock())

    assert await client.get_json("https://example.test/a.json", ttl=10) is None


async def test_xml_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<?xml version="1.0"?><Report><Head/></Report>')

    client = make_client(handler, Clock())
    root = await client.get_xml("https://example.test/a.xml", ttl=10)

    assert root is not None
    assert root.tag == "Report"


async def test_invalid_xml_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<Report>")

    client = make_client(handler, Clock())

    assert await client.get_xml("https://example.test/a.xml", ttl=10) is None


async def test_user_agent_header_is_sent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, text="ok")

    client = make_client(handler, Clock())
    await client.get_text("https://example.test/a", ttl=10)

    assert seen == ["KumaRadioAlerts/0.1 (+https://example.test)"]


async def test_oversized_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 2048)

    client = make_client(handler, Clock(), max_bytes=1024)

    assert await client.get_text("https://example.test/a", ttl=10) is None
