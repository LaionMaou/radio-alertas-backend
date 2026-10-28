from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .models import Event, EventKind, TtsStatus
from .store import EventStore

logger = logging.getLogger(__name__)

_KIND_MAP = {
    EventKind.EEW: "eew",
    EventKind.TSUNAMI: "tsunami",
    EventKind.TYPHOON: "typhoon",
    EventKind.EARTHQUAKE: "other",
}

_RESULT_STATUS = {
    "ok": TtsStatus.OK,
    "stopped": TtsStatus.OK,
    "dry-run": TtsStatus.DRY_RUN,
    "partial": TtsStatus.PARTIAL,
    "failed": TtsStatus.FAILED,
}

_RATE_RETRY_DELAYS = (1.0, 2.0, 4.0)

Notifier = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class SubmitOutcome:
    status: TtsStatus
    alert_id: str | None = None
    duplicate_of: str | None = None
    error: str | None = None


class TtsClient:
    """Cliente HTTP de la cadena TTS existente; nunca espera la reproducción."""

    def __init__(
        self,
        settings: Settings,
        store: EventStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        notifier: Notifier | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = 1.0,
    ) -> None:
        self.settings = settings
        self.store = store
        self._notifier = notifier
        self._sleep = sleep
        self._clock = clock
        self._poll_interval = poll_interval
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        headers = {}
        if settings.tts_api_token:
            headers["Authorization"] = f"Bearer {settings.tts_api_token}"
        self._client = httpx.AsyncClient(
            base_url=settings.tts_api_url,
            headers=headers,
            timeout=httpx.Timeout(15.0, connect=5.0),
            transport=transport,
        )

    async def submit(
        self,
        event: Event,
        *,
        text: str,
        dedupe_key: str,
        tone: str = "soft",
        loop: bool = False,
        preempt: bool = False,
    ) -> SubmitOutcome:
        if self.settings.dry_run:
            await self.store.update_tts(
                event.event_key,
                {"status": TtsStatus.DRY_RUN.value, "alert_id": None, "error": None},
            )
            logger.info(
                "tts.dry_run",
                extra={
                    "event_key": event.event_key,
                    "kind": event.kind.value,
                    "chars": len(text),
                    "text": text,
                },
            )
            return SubmitOutcome(status=TtsStatus.DRY_RUN)

        payload: dict[str, Any] = {
            "text": text,
            "kind": _KIND_MAP.get(event.kind, "other"),
            "magnitude": (event.epicenter or {}).get("magnitude"),
            "title": _title(event),
            "dedupe_key": dedupe_key,
            "tone": tone,
        }
        if loop:
            payload["loop"] = True
        if preempt:
            payload["preempt"] = True
        async with self._lock:
            return await self._submit_locked(event.event_key, payload)

    def status(self) -> dict[str, Any]:
        return {
            "dry_run": bool(self.settings.dry_run),
            "configured": bool(self.settings.tts_api_token),
            "pending_results": len(self._tasks),
        }

    async def wait_pending(self) -> None:
        if not self._tasks:
            return
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
        await self._client.aclose()

    async def _submit_locked(self, event_key: str, payload: dict[str, Any]) -> SubmitOutcome:
        rate_retries = 0
        network_retries = 0

        while True:
            try:
                response = await self._client.post("/alert", json=payload)
            except httpx.HTTPError as exc:
                if network_retries < 1:
                    network_retries += 1
                    await self._sleep(2.0)
                    continue
                return await self._fail(event_key, f"{type(exc).__name__}: {exc}")

            if response.status_code == 202:
                data = _json(response)
                alert_id = str(data.get("alert_id") or "").strip()
                if not alert_id:
                    return await self._fail(event_key, "respuesta sin alert_id")
                await self.store.update_tts(
                    event_key,
                    {"status": TtsStatus.QUEUED.value, "alert_id": alert_id, "error": None},
                )
                logger.info(
                    "tts.submit",
                    extra={"event_key": event_key, "alert_id": alert_id, "status": "queued"},
                )
                self._spawn_wait(alert_id, event_key)
                return SubmitOutcome(status=TtsStatus.QUEUED, alert_id=alert_id)

            if response.status_code == 200:
                data = _json(response)
                duplicate = str(
                    data.get("duplicate_of") or data.get("alert_id") or ""
                ).strip()
                await self.store.update_tts(
                    event_key,
                    {
                        "status": TtsStatus.DUPLICATE.value,
                        "alert_id": duplicate or None,
                        "error": None,
                    },
                )
                logger.info(
                    "tts.submit",
                    extra={
                        "event_key": event_key,
                        "alert_id": duplicate or None,
                        "status": "duplicate",
                    },
                )
                if duplicate:
                    await self._check_existing(duplicate, event_key)
                return SubmitOutcome(
                    status=TtsStatus.DUPLICATE,
                    alert_id=duplicate or None,
                    duplicate_of=duplicate or None,
                )

            if response.status_code == 429:
                if rate_retries < len(_RATE_RETRY_DELAYS):
                    await self._sleep(_RATE_RETRY_DELAYS[rate_retries])
                    rate_retries += 1
                    continue
                return await self._fail(event_key, "TTS ocupado (429)")

            if response.status_code in (401, 403):
                return await self._fail(
                    event_key,
                    f"autenticación rechazada ({response.status_code})",
                )

            if response.status_code >= 500:
                if network_retries < 1:
                    network_retries += 1
                    await self._sleep(2.0)
                    continue
                return await self._fail(
                    event_key, f"TTS no disponible ({response.status_code})"
                )

            return await self._fail(event_key, f"respuesta inesperada ({response.status_code})")

    def _spawn_wait(self, alert_id: str, event_key: str) -> None:
        task = asyncio.create_task(self._wait_for(alert_id, event_key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _wait_for(self, alert_id: str, event_key: str) -> None:
        deadline = self._clock() + float(self.settings.tts_result_timeout_seconds)
        while self._clock() < deadline:
            try:
                response = await self._client.get(_alert_path(alert_id))
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code == 200:
                await self._apply_result(alert_id, event_key, _json(response))
                return
            await self._sleep(self._poll_interval)

        await self.store.update_tts(
            event_key,
            {
                "status": TtsStatus.UNKNOWN.value,
                "alert_id": alert_id,
                "error": "resultado de TTS no disponible",
            },
        )
        logger.info(
            "tts.result",
            extra={"event_key": event_key, "alert_id": alert_id, "status": "unknown"},
        )

    async def _check_existing(self, alert_id: str, event_key: str) -> None:
        try:
            response = await self._client.get(_alert_path(alert_id))
        except httpx.HTTPError:
            return
        if response.status_code == 200:
            await self._apply_result(alert_id, event_key, _json(response))

    async def _apply_result(
        self,
        alert_id: str,
        event_key: str,
        data: dict[str, Any],
    ) -> None:
        status = _RESULT_STATUS.get(str(data.get("status") or ""), TtsStatus.UNKNOWN)
        error = data.get("error")
        await self.store.update_tts(
            event_key,
            {
                "status": status.value,
                "alert_id": alert_id,
                "error": str(error) if error else None,
            },
        )
        logger.info(
            "tts.result",
            extra={
                "event_key": event_key,
                "alert_id": alert_id,
                "status": status.value,
                "sent_audio": bool(data.get("sent_audio")),
            },
        )
        if status in (TtsStatus.FAILED, TtsStatus.PARTIAL):
            await self._notify(
                "tts_failed",
                {
                    "event_key": event_key,
                    "alert_id": alert_id,
                    "status": status.value,
                    "error": str(error) if error else None,
                },
            )

    async def _fail(self, event_key: str, error: str) -> SubmitOutcome:
        await self.store.update_tts(
            event_key,
            {"status": TtsStatus.FAILED.value, "alert_id": None, "error": error},
        )
        logger.error("tts.failed", extra={"event_key": event_key, "error": error})
        await self._notify("tts_failed", {"event_key": event_key, "error": error})
        return SubmitOutcome(status=TtsStatus.FAILED, error=error)

    async def _notify(self, reason: str, fields: dict[str, Any]) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier(reason, fields)
        except Exception as exc:
            logger.warning(
                "tts.notify_error", extra={"reason": reason, "error": type(exc).__name__}
            )


def _title(event: Event) -> str:
    if event.kind == EventKind.EARTHQUAKE:
        return "地震情報"
    return event.title or ""


def _alert_path(alert_id: str) -> str:
    return f"/alert/{quote(alert_id, safe='')}"


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
