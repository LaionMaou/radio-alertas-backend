from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .common import parse_cod, parse_time

_TSUNAMI_TITLE = "津波"


@dataclass(frozen=True)
class TsunamiListEntry:
    eid: str
    origin_at: datetime | None
    report_at: datetime | None
    name: str
    title: str
    lat: float | None
    lon: float | None
    detail: str


def parse_tsunami_list(data: Any) -> list[TsunamiListEntry]:
    if not isinstance(data, list):
        return []
    entries: list[TsunamiListEntry] = []
    for raw in data:
        if not isinstance(raw, Mapping):
            continue
        eid = str(raw.get("eid") or "").strip()
        title = str(raw.get("ttl") or "").strip()
        if not eid or _TSUNAMI_TITLE not in title:
            continue
        lat, lon = parse_cod(raw.get("cod"))
        entries.append(
            TsunamiListEntry(
                eid=eid,
                origin_at=parse_time(raw.get("at")),
                report_at=parse_time(raw.get("rdt")),
                name=str(raw.get("anm") or "").strip(),
                title=title,
                lat=lat,
                lon=lon,
                detail=str(raw.get("json") or "").strip(),
            )
        )
    return entries


def match_tsunami(
    entries: Sequence[TsunamiListEntry],
    *,
    issued_at: datetime | None,
    tolerance_seconds: float = 300.0,
) -> TsunamiListEntry | None:
    """El 552 de P2P trae la hora de emisión, que corresponde a `rdt` de JMA."""
    if issued_at is None:
        return None
    limit = tolerance_seconds
    best: tuple[float, TsunamiListEntry] | None = None
    for entry in entries:
        for candidate in (entry.report_at, entry.origin_at):
            if candidate is None:
                continue
            delta = abs((candidate - issued_at).total_seconds())
            if delta <= limit and (best is None or delta < best[0]):
                best = (delta, entry)
    return best[1] if best is not None else None
