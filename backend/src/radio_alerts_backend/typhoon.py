from __future__ import annotations

from datetime import datetime
from typing import Any

from .jma.service import TyphoonSnapshot
from .models import Event, EventKind, EventStatus
from .normalize import iso_z, sha1_12, typhoon_event_key, utcnow


def event_from_snapshot(snapshot: TyphoonSnapshot, *, source: str = "jma") -> Event | None:
    info = snapshot.info
    if info is None or not info.event_id:
        return None
    areas = [
        {
            "code": area.code,
            "name": area.name,
            "prefecture": area.prefecture,
            "probability": area.probability,
            "at": iso_z(area.at),
            "window": area.window,
        }
        for area in snapshot.probabilities
    ]
    data = {
        "event_id": info.event_id,
        "number": info.number,
        "number_label": info.number_label,
        "name": info.name,
        "class": info.class_text,
        "location": info.location,
        "direction": info.direction,
        "pressure_hpa": info.pressure_hpa,
        "wind_mps": info.wind_mps,
        "gust_mps": info.gust_mps,
        "lat": info.lat,
        "lon": info.lon,
        "areas": areas,
    }
    return _event(data, issued_at=info.report_at or snapshot.fetched_at, source=source)


def event_from_mapping(
    data: dict[str, Any],
    *,
    issued_at: datetime | None = None,
    source: str = "simulate",
) -> Event:
    return _event(dict(data), issued_at=issued_at or utcnow(), source=source)


def _event(data: dict[str, Any], *, issued_at: datetime, source: str) -> Event:
    ident = str(data.get("event_id") or "").strip()
    key = typhoon_event_key(ident or f"sim-{sha1_12(str(sorted(data.items())))}")
    return Event(
        event_key=key,
        kind=EventKind.TYPHOON,
        issued_at=issued_at,
        status=EventStatus.ACTIVE,
        title="台風情報",
        summary=_summary(data),
        typhoon=data,
        source=source,
        raw_id=ident,
    )


def _summary(data: dict[str, Any]) -> str:
    parts: list[str] = []
    label = _number_label(data)
    name = str(data.get("name") or "")
    if label or name:
        parts.append(f"{label} {name}".strip())
    location = str(data.get("location") or "")
    if location:
        parts.append(location)
    pressure = data.get("pressure_hpa")
    if pressure is not None:
        parts.append(f"{pressure}hPa")
    wind = data.get("wind_mps")
    if wind is not None:
        parts.append(f"最大風速{wind}m/s")
    return " ".join(parts) or "台風情報"


def _number_label(data: dict[str, Any]) -> str:
    label = str(data.get("number_label") or "").strip()
    if label:
        return label
    number = str(data.get("number") or "").strip()
    if len(number) >= 2 and number[-2:].isdigit():
        return f"台風{int(number[-2:])}号"
    return ""
