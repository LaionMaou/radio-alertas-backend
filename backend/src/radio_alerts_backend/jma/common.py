from __future__ import annotations

import re
from typing import Any
from xml.etree.ElementTree import Element

from ..normalize import parse_iso

_COD_RE = re.compile(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text(elem: Element | None, path: str) -> str:
    if elem is None:
        return ""
    found = elem.find(path)
    return (found.text or "").strip() if found is not None else ""


def attr(elem: Element | None, name: str) -> str:
    if elem is None:
        return ""
    for key, value in elem.attrib.items():
        if local_name(key) == name:
            return str(value).strip()
    return ""


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return int(round(number)) if number is not None else None


def parse_cod(value: Any) -> tuple[float | None, float | None]:
    """Formato JMA `+36.8+140.4-90000/`: lat, lon y profundidad en metros."""
    if value is None:
        return None, None
    match = _COD_RE.match(str(value).strip())
    if match is None:
        return None, None
    lat = to_float(match.group(1))
    lon = to_float(match.group(2))
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None, None
    return lat, lon


def parse_time(value: Any):
    return parse_iso(str(value)) if value is not None else None
