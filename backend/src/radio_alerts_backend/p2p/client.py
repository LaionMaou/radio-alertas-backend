from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import websockets

from ..models import P2PStatus
from ..normalize import iso_z, sha1_12, utcnow

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
StateHandler = Callable[[bool], Awaitable[None]]
AttemptsHandler = Callable[[int], Awaitable[None]]


class P2PClient:
    """Una sola conexión WebSocket a P2PQuake, con reconexión exponencial."""

    def __init__(
        self,
        url: str,
        codes: set[int] | frozenset[int],
        handler: MessageHandler,
        *,
        backoff_initial: float = 1.0,
        backoff_factor: float = 2.0,
        backoff_max: float = 30.0,
        backoff_jitter: float = 0.2,
        alert_after_attempts: int = 10,
        healthy_seconds: float = 60.0,
        user_agent: str = "KumaRadioAlerts/0.1",
        on_connection_change: StateHandler | None = None,
        on_attempts_exceeded: AttemptsHandler | None = None,
        connect: Callable[..., Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = utcnow,
        random_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.url = url
        self.codes = frozenset(int(code) for code in codes)
        self._handler = handler
        self._backoff_initial = backoff_initial
        self._backoff_factor = backoff_factor
        self._backoff_max = backoff_max
        self._backoff_jitter = backoff_jitter
        self._alert_after_attempts = alert_after_attempts
        self._healthy_seconds = healthy_seconds
        self._user_agent = user_agent
        self._on_connection_change = on_connection_change
        self._on_attempts_exceeded = on_attempts_exceeded
        self._connect = connect or websockets.connect
        self._sleep = sleep
        self._clock = clock
        self._random = random_fn

        self.status = P2PStatus()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_limit = 2000
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ws: Any = None
        self._alerted = False

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stopped.clear()
        self._task = asyncio.create_task(self.run(), name="p2p-client")
        return self._task

    def request_stop(self) -> None:
        self._stopped.set()

    async def stop(self) -> None:
        self.request_stop()
        websocket = self._ws
        if websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.close()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._ws = None
        self.status.connected = False
        self.status.connected_since = None

    async def run(self) -> None:
        while not self._stopped.is_set():
            await self.run_session()
            if self._stopped.is_set():
                break
            await self._backoff()

    async def run_session(self) -> None:
        connected_at: datetime | None = None
        try:
            async with self._connect(
                self.url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                open_timeout=10,
                max_size=1024 * 1024,
                user_agent_header=self._user_agent,
            ) as websocket:
                self._ws = websocket
                connected_at = self._clock()
                self.status.connected = True
                self.status.connected_since = iso_z(connected_at)
                self.status.next_retry_at = None
                logger.info("p2p.connected", extra={"url": self.url})
                await self._notify_connection(True)

                async for raw in websocket:
                    if self._stopped.is_set():
                        break
                    await self.handle_raw(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = _safe_error(exc)
            self.status.last_error = message
            logger.warning("p2p.disconnected", extra={"error": message})
        finally:
            was_connected = self.status.connected
            self._ws = None
            self.status.connected = False
            self.status.connected_since = None
            if (
                connected_at is not None
                and (self._clock() - connected_at).total_seconds() >= self._healthy_seconds
            ):
                self.status.attempts = 0
                self._alerted = False
            if was_connected:
                await self._notify_connection(False)

    async def handle_raw(self, raw: str | bytes) -> bool:
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("p2p.invalid_frame")
                return False
        else:
            text = raw

        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            logger.warning("p2p.invalid_json")
            return False
        if not isinstance(payload, dict):
            logger.warning("p2p.invalid_payload")
            return False

        self.status.last_message_at = iso_z(self._clock())

        code = payload.get("code")
        try:
            code = int(code)
        except (TypeError, ValueError):
            return False
        if code not in self.codes:
            logger.debug("p2p.unsupported_code", extra={"code": code})
            return False

        identity = str(payload.get("id") or "").strip() or sha1_12(text)
        if identity in self._seen:
            logger.info("p2p.duplicate", extra={"code": code, "id": identity})
            return False
        self._seen[identity] = None
        self._seen.move_to_end(identity)
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)

        try:
            await self._handler(payload)
        except Exception as exc:
            logger.warning(
                "p2p.handler_error",
                extra={"code": code, "error": _safe_error(exc)},
            )
            return False
        return True

    async def _backoff(self) -> None:
        self.status.attempts += 1
        delay = min(
            self._backoff_initial * (self._backoff_factor ** (self.status.attempts - 1)),
            self._backoff_max,
        )
        if self._backoff_jitter:
            delay *= 1 + self._random(-self._backoff_jitter, self._backoff_jitter)
            delay = max(0.1, delay)

        retry_at = self._clock() + timedelta(seconds=delay)
        self.status.next_retry_at = iso_z(retry_at)
        logger.info(
            "p2p.reconnect",
            extra={"attempt": self.status.attempts, "delay": round(delay, 2)},
        )

        if (
            self.status.attempts >= self._alert_after_attempts
            and not self._alerted
            and self._on_attempts_exceeded is not None
        ):
            self._alerted = True
            with contextlib.suppress(Exception):
                await self._on_attempts_exceeded(self.status.attempts)

        await self._sleep(delay)
        self.status.next_retry_at = None

    async def _notify_connection(self, connected: bool) -> None:
        if self._on_connection_change is None:
            return
        with contextlib.suppress(Exception):
            await self._on_connection_change(connected)


def _safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    if not text:
        text = exc.__class__.__name__
    return text[:200]
