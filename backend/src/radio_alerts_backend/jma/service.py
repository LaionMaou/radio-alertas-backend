from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ..config import Settings
from ..models import JMAStatus
from ..normalize import iso_z, utcnow
from .client import JMAClient
from .feed import (
    FeedEntry,
    TyphoonAreaProbability,
    TyphoonInfo,
    parse_feed,
    parse_vpta51,
    parse_vptw61,
    select_latest,
)
from .quake_list import QuakeListEntry, match_quake, parse_quake_list
from .tsunami_list import TsunamiListEntry, match_tsunami, parse_tsunami_list

logger = logging.getLogger(__name__)

QUAKE_LIST_URL = "https://www.jma.go.jp/bosai/quake/data/list.json"
TSUNAMI_LIST_URL = "https://www.jma.go.jp/bosai/tsunami/data/list.json"
EXTRA_FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"

LIST_TTL_SECONDS = 300
FEED_TTL_SECONDS = 600
DETAIL_TTL_SECONDS = 600


@dataclass(frozen=True)
class Enrichment:
    eid: str
    maxi: int | None
    magnitude: float | None
    detail: str


@dataclass(frozen=True)
class TyphoonSnapshot:
    info: TyphoonInfo | None
    probabilities: list[TyphoonAreaProbability]
    fetched_at: datetime


class JMAService:
    """Enriquecimiento opcional: si JMA falla, el evento P2P sigue su curso."""

    def __init__(self, settings: Settings, client: JMAClient | None = None) -> None:
        self.settings = settings
        self.enabled = bool(settings.jma_enabled)
        self._client = client or JMAClient(
            user_agent=settings.jma_user_agent,
            min_interval_seconds=settings.jma_min_interval_seconds,
        )
        self._owns_client = client is None
        self.status = JMAStatus(enabled=self.enabled)
        self._typhoon: TyphoonSnapshot | None = None

    async def enrich_quake(
        self,
        *,
        origin_at: datetime | None,
        name: str | None,
        lat: float | None,
        lon: float | None,
    ) -> Enrichment | None:
        if not self.enabled or origin_at is None:
            return None
        data = await self._client.get_json(QUAKE_LIST_URL, ttl=LIST_TTL_SECONDS)
        if data is None:
            self._fail("lista de sismos no disponible")
            return None
        entry: QuakeListEntry | None = match_quake(
            parse_quake_list(data),
            origin_at=origin_at,
            name=name,
            lat=lat,
            lon=lon,
        )
        if entry is None:
            logger.info("jma.enrich", extra={"kind": "quake", "matched": False})
            return None
        self._ok()
        logger.info("jma.enrich", extra={"kind": "quake", "matched": True, "eid": entry.eid})
        return Enrichment(
            eid=entry.eid,
            maxi=entry.maxi,
            magnitude=entry.magnitude,
            detail=entry.detail,
        )

    async def enrich_tsunami(self, *, issued_at: datetime | None) -> Enrichment | None:
        if not self.enabled or issued_at is None:
            return None
        data = await self._client.get_json(TSUNAMI_LIST_URL, ttl=LIST_TTL_SECONDS)
        if data is None:
            self._fail("lista de tsunamis no disponible")
            return None
        entry: TsunamiListEntry | None = match_tsunami(
            parse_tsunami_list(data),
            issued_at=issued_at,
        )
        if entry is None:
            logger.info("jma.enrich", extra={"kind": "tsunami", "matched": False})
            return None
        self._ok()
        logger.info("jma.enrich", extra={"kind": "tsunami", "matched": True, "eid": entry.eid})
        return Enrichment(eid=entry.eid, maxi=None, magnitude=None, detail=entry.detail)

    async def refresh_typhoons(self) -> TyphoonSnapshot | None:
        if not self.enabled or not self.settings.typhoon_enabled:
            return self._typhoon

        feed = await self._client.get_xml(EXTRA_FEED_URL, ttl=FEED_TTL_SECONDS)
        if feed is None:
            self._fail("feed de tifones no disponible")
            return self._typhoon

        latest: dict[str, FeedEntry] = select_latest(parse_feed(feed))
        info: TyphoonInfo | None = None
        probabilities: list[TyphoonAreaProbability] = []

        if "VPTW61" in latest:
            xml = await self._client.get_xml(latest["VPTW61"].url, ttl=DETAIL_TTL_SECONDS)
            if xml is not None:
                info = parse_vptw61(xml, source_url=latest["VPTW61"].url)

        area_codes = self.settings.typhoon_area_set
        if "VPTA51" in latest and area_codes:
            xml = await self._client.get_xml(latest["VPTA51"].url, ttl=DETAIL_TTL_SECONDS)
            if xml is not None:
                probabilities = parse_vpta51(xml, area_codes).areas

        if info is None and not probabilities:
            self._fail("sin datos de tifón utilizables")
            return self._typhoon

        self._ok()
        snapshot = TyphoonSnapshot(
            info=info,
            probabilities=probabilities,
            fetched_at=utcnow(),
        )
        self._typhoon = snapshot
        self.status.typhoon_updated_at = iso_z(snapshot.fetched_at)
        self.status.typhoon_name = _typhoon_label(info)
        logger.info(
            "jma.typhoon",
            extra={
                "event_id": info.event_id if info else None,
                "areas": len(probabilities),
                "threshold": self.settings.typhoon_probability_threshold,
            },
        )
        return snapshot

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _ok(self) -> None:
        self.status.last_success_at = iso_z(utcnow())
        self.status.last_error = None

    def _fail(self, message: str) -> None:
        self.status.last_error = message


def _typhoon_label(info: TyphoonInfo | None) -> str | None:
    if info is None:
        return None
    number = info.number_label
    if number and info.name:
        return f"{number} {info.name}"
    return info.name or number
