from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree as DefusedET

from ..normalize import utcnow

logger = logging.getLogger(__name__)

_MAX_BYTES = 8 * 1024 * 1024


@dataclass
class _CacheEntry:
    text: str
    expires_at: datetime


class JMAClient:
    """HTTP de JMA con caché y un presupuesto fijo por URL.

    La caché sirve respuestas viejas si la red falla, y el intervalo mínimo
    evita ráfagas: JMA no debe recibir más de una petición por endpoint cada
    `min_interval_seconds`, aunque un evento P2P llegue en medio.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        min_interval_seconds: float = 300.0,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=utcnow,
        max_bytes: int = _MAX_BYTES,
    ) -> None:
        self._user_agent = user_agent
        self._min_interval = float(min_interval_seconds)
        self._clock = clock
        self._max_bytes = max_bytes
        self._cache: dict[str, _CacheEntry] = {}
        self._last_attempt: dict[str, datetime] = {}
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, read=timeout * 2),
            headers={"User-Agent": user_agent},
            transport=transport,
            follow_redirects=True,
        )

    async def get_text(self, url: str, *, ttl: float) -> str | None:
        cached = self._cache.get(url)
        now = self._clock()
        if cached is not None and now < cached.expires_at:
            return cached.text

        last = self._last_attempt.get(url)
        if last is not None and (now - last).total_seconds() < self._min_interval:
            logger.debug("jma.rate_limited", extra={"url": url})
            return cached.text if cached is not None else None

        self._last_attempt[url] = now
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            logger.warning(
                "jma.fetch_error",
                extra={"url": url, "error": _safe_error(exc)},
            )
            return cached.text if cached is not None else None

        if response.status_code >= 400:
            logger.warning(
                "jma.fetch_status",
                extra={"url": url, "status": response.status_code},
            )
            return cached.text if cached is not None else None

        text = response.text
        if len(text) > self._max_bytes:
            logger.warning(
                "jma.fetch_too_large",
                extra={"url": url, "bytes": len(text)},
            )
            return cached.text if cached is not None else None

        self._cache[url] = _CacheEntry(
            text=text,
            expires_at=now + timedelta(seconds=ttl),
        )
        return text

    async def get_json(self, url: str, *, ttl: float) -> Any | None:
        text = await self.get_text(url, ttl=ttl)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            logger.warning("jma.invalid_json", extra={"url": url})
            return None

    async def get_xml(self, url: str, *, ttl: float) -> Element | None:
        text = await self.get_text(url, ttl=ttl)
        if text is None:
            return None
        try:
            return DefusedET.fromstring(text)
        except Exception as exc:
            logger.warning(
                "jma.invalid_xml",
                extra={"url": url, "error": _safe_error(exc)},
            )
            return None

    async def aclose(self) -> None:
        await self._http.aclose()


def _safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text[:200]
