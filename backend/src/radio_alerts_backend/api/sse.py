from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..services import Services
from .common import services

logger = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

router = APIRouter()


@router.get("/api/alert/stream")
async def alert_stream(request: Request) -> StreamingResponse:
    return StreamingResponse(
        event_stream(
            services(request),
            request.is_disconnected,
            _last_event_id(request),
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def event_stream(
    service: Services,
    is_disconnected: Callable[[], Awaitable[bool]],
    last_event_id: int | None = None,
    *,
    heartbeat_seconds: float = _HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    logger.info("sse.client", extra={"action": "connected", "last_event_id": last_event_id})
    try:
        yield "retry: 5000\n\n"
        snapshot = service.store.snapshot(service.settings.history_limit)
        snapshot["status"] = service.status()
        yield _format("snapshot", snapshot, service.bus.seq)

        async with service.bus.subscribe(last_event_id) as subscription:
            for message in subscription.backlog:
                yield _format(message.type, message.data, message.seq)
            while True:
                if await is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(
                        subscription.queue.get(), timeout=heartbeat_seconds
                    )
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _format(message.type, message.data, message.seq)
    finally:
        logger.info("sse.client", extra={"action": "disconnected"})


def _format(event_type: str, data: dict[str, Any], seq: int) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {seq}\nevent: {event_type}\ndata: {payload}\n\n"


def _last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
