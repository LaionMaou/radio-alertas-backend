from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..models import Event, EventKind, EventStatus
from ..normalize import (
    VALID_SCALES,
    eew_event_key,
    grade_rank,
    iso_z,
    parse_p2p_time,
    quake_event_key,
    scale_code,
    scale_label,
    tsunami_event_key,
    utcnow,
)

_GRADE_LABELS = {
    "MajorWarning": "大津波警報",
    "Warning": "津波警報",
    "Watch": "津波注意報",
    "Unknown": "津波予報",
}

_EEW_TITLE = "緊急地震速報"
_QUAKE_TITLE = "地震情報"


@dataclass(frozen=True)
class ParseResult:
    event: Event | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.event is not None


def parse_message(
    payload: Mapping[str, Any],
    *,
    received_at: datetime | None = None,
    jma_eid: str | None = None,
) -> ParseResult:
    code = _int(payload.get("code"))
    received = received_at or utcnow()

    if code == 551:
        return ParseResult(event=_parse_quake(payload, received, jma_eid))
    if code == 552:
        return ParseResult(event=_parse_tsunami(payload, received, jma_eid))
    if code == 556:
        if payload.get("test"):
            return ParseResult(reason="test")
        return ParseResult(event=_parse_eew(payload, received))
    return ParseResult(reason=f"unsupported:{code}")


def _parse_quake(
    payload: Mapping[str, Any],
    received: datetime,
    jma_eid: str | None,
) -> Event:
    earthquake = _mapping(payload.get("earthquake"))
    hypocenter = _mapping(earthquake.get("hypocenter"))
    points = _sequence(payload.get("points"))

    raw_scale = _scale_value(earthquake.get("maxScale"))
    if raw_scale in (None, -1):
        raw_scale = _pick_scale(points, "scale")

    origin_at = _p2p_time(earthquake.get("time"))
    issued_at = (
        _p2p_time(_mapping(payload.get("issue")).get("time"))
        or _p2p_time(payload.get("time"))
        or origin_at
        or received
    )
    intensity = _intensity(raw_scale)
    epicenter = _epicenter(hypocenter)
    domestic = _text(earthquake.get("domesticTsunami")) or None
    foreign = _text(earthquake.get("foreignTsunami")) or None

    raw_id = _text(payload.get("id")) or _text(payload.get("time"))
    return Event(
        event_key=quake_event_key(jma_eid, raw_id),
        kind=EventKind.EARTHQUAKE,
        issued_at=issued_at,
        received_at=received,
        origin_at=origin_at,
        title=_QUAKE_TITLE,
        summary=_quake_summary(epicenter, intensity),
        epicenter=epicenter,
        max_intensity=intensity,
        tsunami={"domestic": domestic, "foreign": foreign},
        enrichment={"jma_eid": jma_eid, "source": "jma" if jma_eid else "p2p"},
        source="p2p",
        raw_id=raw_id,
    )


def _parse_tsunami(
    payload: Mapping[str, Any],
    received: datetime,
    jma_eid: str | None,
) -> Event:
    issue = _mapping(payload.get("issue"))
    cancelled = bool(payload.get("cancelled"))
    areas = [_tsunami_area(item) for item in _sequence(payload.get("areas"))]
    areas = [area for area in areas if area is not None]

    grade = "None" if cancelled else _max_grade(areas)
    immediate = any(area["immediate"] for area in areas)
    issued_at = _p2p_time(issue.get("time")) or _p2p_time(payload.get("time")) or received
    label = _GRADE_LABELS.get(grade, "津波予報")

    if cancelled:
        title = "津波情報"
        summary = "津波予報は解除されました。"
    else:
        title = label
        summary = _tsunami_summary(label, areas)

    return Event(
        event_key=tsunami_event_key(jma_eid, issued_at),
        kind=EventKind.TSUNAMI,
        issued_at=issued_at,
        received_at=received,
        status=EventStatus.CANCELLED if cancelled else EventStatus.ACTIVE,
        title=title,
        summary=summary,
        tsunami={
            "domestic": None if cancelled else grade,
            "grade": grade,
            "immediate": immediate,
            "cancelled": cancelled,
            "areas": areas,
        },
        enrichment={"jma_eid": jma_eid, "source": "jma" if jma_eid else "p2p"},
        source="p2p",
        raw_id=_text(payload.get("id")) or _text(issue.get("time")),
    )


def _parse_eew(payload: Mapping[str, Any], received: datetime) -> Event:
    earthquake = _mapping(payload.get("earthquake"))
    hypocenter = _mapping(earthquake.get("hypocenter"))
    issue = _mapping(payload.get("issue"))
    cancelled = bool(payload.get("cancelled"))
    areas = [_eew_area(item) for item in _sequence(payload.get("areas"))]
    areas = [area for area in areas if area is not None]

    origin_at = _p2p_time(earthquake.get("originTime"))
    issued_at = _p2p_time(issue.get("time")) or received
    raw_scale = None if cancelled else _pick_scale(areas, "scale_to")
    intensity = _intensity(raw_scale)
    epicenter = _epicenter(hypocenter)
    name = epicenter.get("name") or ""
    event_key = eew_event_key(issue.get("eventId"), origin_at, name)

    summary = (
        "緊急地震速報は取り消されました。"
        if cancelled
        else _eew_summary(epicenter, intensity, _text(earthquake.get("condition")))
    )

    return Event(
        event_key=event_key,
        kind=EventKind.EEW,
        issued_at=issued_at,
        received_at=received,
        origin_at=origin_at,
        status=EventStatus.CANCELLED if cancelled else EventStatus.ACTIVE,
        title=_EEW_TITLE,
        summary=summary,
        epicenter=epicenter,
        max_intensity=intensity,
        eew={
            "areas": areas,
            "scale_to_max": raw_scale,
            "scale_from_max": None if cancelled else _pick_scale(areas, "scale_from"),
            "kind_code": _first_kind_code(areas),
            "condition": _text(earthquake.get("condition")),
            "serial": _text(issue.get("serial")),
            "event_id": _text(issue.get("eventId")),
        },
        source="p2p",
        raw_id=_text(issue.get("eventId")),
    )


def _tsunami_area(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    grade = _text(item.get("grade"))
    if grade not in _GRADE_LABELS:
        grade = "Unknown"
    first_height = _mapping(item.get("firstHeight"))
    max_height = _mapping(item.get("maxHeight"))
    return {
        "name": _text(item.get("name")),
        "grade": grade,
        "immediate": bool(item.get("immediate")),
        "arrival_time": iso_z(_p2p_time(first_height.get("arrivalTime"))),
        "condition": _text(first_height.get("condition")) or None,
        "max_height": {
            "description": _text(max_height.get("description")) or None,
            "value": _number(max_height.get("value")),
        },
    }


def _eew_area(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    scale_from = _scale_value(item.get("scaleFrom"))
    scale_to = _scale_value(item.get("scaleTo"))
    if scale_from is None and scale_to is None:
        return None
    return {
        "pref": _text(item.get("pref")),
        "name": _text(item.get("name")),
        "scale_from": scale_from,
        "scale_to": scale_to,
        "scale_from_label": scale_label(scale_from),
        "scale_to_label": scale_label(scale_to),
        "kind_code": _text(item.get("kindCode")),
        "arrival_time": iso_z(_p2p_time(item.get("arrivalTime"))),
    }


def _epicenter(hypocenter: Mapping[str, Any]) -> dict[str, Any]:
    depth = _number(hypocenter.get("depth"))
    magnitude = _number(hypocenter.get("magnitude"))
    return {
        "name": _text(hypocenter.get("name")),
        "reduce_name": _text(hypocenter.get("reduceName")) or None,
        "lat": _coordinate(hypocenter.get("latitude"), 90),
        "lon": _coordinate(hypocenter.get("longitude"), 180),
        "depth_km": int(round(depth)) if depth is not None and depth >= 0 else None,
        "magnitude": round(magnitude, 1)
        if magnitude is not None and magnitude >= 0
        else None,
    }


def _intensity(raw: int | None) -> dict[str, Any]:
    if raw is None:
        return {"value": None, "label": "不明"}
    return {"value": raw, "label": scale_label(raw)}


def _quake_summary(
    epicenter: Mapping[str, Any],
    intensity: Mapping[str, Any],
) -> str:
    parts = [epicenter.get("name") or "震源不明"]
    magnitude = epicenter.get("magnitude")
    if magnitude is not None:
        parts.append(f"M{magnitude}")
    if intensity.get("label") not in (None, "不明"):
        parts.append(f"最大震度{intensity['label']}")
    return " ".join(str(part) for part in parts)


def _eew_summary(
    epicenter: Mapping[str, Any],
    intensity: Mapping[str, Any],
    condition: str,
) -> str:
    parts = [epicenter.get("name") or "震源不明"]
    if intensity.get("label") not in (None, "不明"):
        parts.append(f"最大震度{intensity['label']}（予想）")
    if condition:
        parts.append(condition)
    return " ".join(str(part) for part in parts)


def _tsunami_summary(label: str, areas: Sequence[Mapping[str, Any]]) -> str:
    names = [str(area["name"]) for area in areas if area.get("name")]
    if not names:
        return f"{label}が発表されました。"
    if len(names) <= 2:
        return f"{label}（{'、'.join(names)}）"
    return f"{label} {len(names)}地域"


def _max_grade(areas: Sequence[Mapping[str, Any]]) -> str:
    best = "Unknown"
    best_rank = -1
    for area in areas:
        grade = str(area.get("grade") or "Unknown")
        rank = grade_rank(grade)
        if rank > best_rank:
            best = grade
            best_rank = rank
    return best


def _pick_scale(entries: Sequence[Any], field: str) -> int | None:
    best: int | None = None
    best_key = (-1, False)
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        raw = _scale_value(entry.get(field))
        if raw is None:
            continue
        key = (scale_code(raw), raw in (46, 99))
        if key > best_key:
            best = raw
            best_key = key
    return best


def _first_kind_code(areas: Sequence[Mapping[str, Any]]) -> str:
    for area in areas:
        code = _text(area.get("kind_code"))
        if code:
            return code
    return ""


def _scale_value(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    code = int(round(number))
    return code if code in VALID_SCALES else None


def _coordinate(value: Any, limit: float) -> float | None:
    number = _number(value)
    if number is None or number == -200 or abs(number) > limit:
        return None
    return round(number, 4)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _p2p_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return parse_p2p_time(value)
