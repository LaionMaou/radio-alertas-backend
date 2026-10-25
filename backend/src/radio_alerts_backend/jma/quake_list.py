from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..normalize import haversine_km, jma_maxi_to_scale
from .common import parse_cod, parse_time, to_float

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuakeListEntry:
    eid: str
    origin_at: datetime | None
    report_at: datetime | None
    name: str
    maxi: int | None
    magnitude: float | None
    lat: float | None
    lon: float | None
    title: str
    detail: str


def parse_quake_list(data: Any) -> list[QuakeListEntry]:
    if not isinstance(data, list):
        return []
    entries: list[QuakeListEntry] = []
    for raw in data:
        if not isinstance(raw, Mapping):
            continue
        eid = str(raw.get("eid") or "").strip()
        if not eid:
            continue
        lat, lon = parse_cod(raw.get("cod"))
        entries.append(
            QuakeListEntry(
                eid=eid,
                origin_at=parse_time(raw.get("at")),
                report_at=parse_time(raw.get("rdt")),
                name=str(raw.get("anm") or "").strip(),
                maxi=jma_maxi_to_scale(raw.get("maxi")),
                magnitude=to_float(raw.get("mag")),
                lat=lat,
                lon=lon,
                title=str(raw.get("ttl") or "").strip(),
                detail=str(raw.get("json") or "").strip(),
            )
        )
    return entries


def match_quake(
    entries: Sequence[QuakeListEntry],
    *,
    origin_at: datetime | None,
    name: str | None,
    lat: float | None,
    lon: float | None,
    tolerance_seconds: float = 120.0,
) -> QuakeListEntry | None:
    """Correlaciona un 551 de P2P con la lista de JMA sin heurísticas frágiles."""
    if origin_at is None:
        return None
    wanted_name = (name or "").strip()
    window = timedelta(seconds=tolerance_seconds)

    candidates: list[tuple[float, float, QuakeListEntry]] = []
    for entry in entries:
        if entry.origin_at is None:
            continue
        delta = abs((entry.origin_at - origin_at).total_seconds())
        if delta > window.total_seconds():
            continue
        name_match = bool(wanted_name) and entry.name == wanted_name
        distance = haversine_km(lat, lon, entry.lat, entry.lon)
        coord_match = distance is not None and distance <= 50.0
        if name_match or coord_match:
            candidates.append((0.0 if name_match else 1.0, delta, entry))
        elif not wanted_name and lat is None and lon is None:
            candidates.append((2.0, delta, entry))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    best = candidates[0]
    if best[0] == 2.0 and len(candidates) > 1:
        logger.debug("jma.quake_ambiguous", extra={"candidates": len(candidates)})
        return None
    return best[2]
