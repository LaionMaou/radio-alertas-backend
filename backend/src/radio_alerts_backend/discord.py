from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx

from .normalize import utcnow

logger = logging.getLogger(__name__)

_REASONS = {
    "p2p_down": "No se puede conectar a P2PQuake",
    "p2p_reconnected": "Conexión con P2PQuake restablecida",
    "jma_failing": "JMA no responde tras varios intentos",
    "tts_failed": "La cadena TTS no pudo emitir la alerta",
    "config_error": "Configuración inválida en el backend de alertas",
}


class DiscordNotifier:
    """Avisos de operación; nunca se usa para confirmar emisiones normales."""

    def __init__(
        self,
        webhook_url: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cooldown_seconds: float = 900.0,
        timeout: float = 8.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._url = webhook_url.strip()
        self._cooldown = cooldown_seconds
        self._sleep = sleep
        self._clock = clock
        self._last_sent: dict[str, datetime] = {}
        self._client = (
            httpx.AsyncClient(timeout=httpx.Timeout(timeout), transport=transport)
            if self._url
            else None
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def __call__(self, reason: str, fields: dict[str, Any]) -> None:
        if self._client is None:
            return
        now = self._clock()
        last = self._last_sent.get(reason)
        if last is not None and (now - last).total_seconds() < self._cooldown:
            return
        self._last_sent[reason] = now

        content = _format(reason, fields)
        for attempt in range(3):
            try:
                response = await self._client.post(self._url, json={"content": content})
                if response.status_code < 400:
                    return
            except httpx.HTTPError:
                pass
            if attempt < 2:
                await self._sleep(1.5 * (2**attempt))
        logger.warning("discord.failed", extra={"reason": reason})

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def _format(reason: str, fields: dict[str, Any]) -> str:
    headline = _REASONS.get(reason, reason)
    details = " ".join(
        f"{key}={_clip(value)}" for key, value in fields.items() if value is not None
    )
    text = f"⚠️ {headline}"
    if details:
        text += f" — {details}"
    return text[:1900]


def _clip(value: Any, limit: int = 200) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
