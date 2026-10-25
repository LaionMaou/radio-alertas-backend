from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

JST: tzinfo = ZoneInfo("Asia/Tokyo")

# Códigos de intensidad de P2P/JMA.
SCALE_LABELS: dict[int, str] = {
    -1: "不明",
    0: "0",
    10: "1",
    20: "2",
    30: "3",
    40: "4",
    45: "5弱",
    46: "5弱以上",
    50: "5強",
    55: "6弱",
    60: "6強",
    70: "7",
    99: "5弱以上",
}

# Para ordenar alertas. 46 y 99 no son intensidades exactas: se comparan como
# el mínimo que garantizan (5弱) para que una observación de 5強 o más pueda
# escalar el mismo evento. Nunca se renderizan como "45" o "99".
SCALE_CODES: dict[int, int] = {
    -1: 0,
    0: 0,
    10: 10,
    20: 20,
    30: 30,
    40: 40,
    45: 45,
    46: 45,
    50: 50,
    55: 55,
    60: 60,
    70: 70,
    99: 45,
}

VALID_SCALES = frozenset(SCALE_CODES)

TSUNAMI_GRADE_RANK: dict[str, int] = {
    "Unknown": 0,
    "None": 0,
    "NonEffective": 0,
    "Watch": 1,
    "Warning": 2,
    "MajorWarning": 3,
}

_TYPHOON_CLASS_RANK: tuple[tuple[str, int], ...] = (
    ("猛烈", 5),
    ("SuperTY", 5),
    ("非常に強い", 4),
    ("STY", 4),
    ("強い", 3),
    ("TY", 3),
    ("STS", 2),
    ("発達中", 2),
    ("TS", 1),
    ("熱帯低気圧", 0),
    ("TD", 0),
)

_P2P_FORMATS = ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return to_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return to_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def parse_p2p_time(value: str | None) -> datetime | None:
    """P2P entrega horas en JST sin zona horaria."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in _P2P_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=JST).astimezone(UTC)
        except ValueError:
            continue
    return parse_iso(text)


def parse_jma_time(value: str | None) -> datetime | None:
    return parse_iso(value)


def format_jst_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    local = value.astimezone(JST)
    if local.minute == 0:
        return f"{local.hour}時"
    return f"{local.hour}時{local.minute:02d}分"


def scale_code(value: int | float | None) -> int:
    if value is None:
        return 0
    try:
        return SCALE_CODES.get(int(round(float(value))), 0)
    except (TypeError, ValueError):
        return 0


def scale_label(value: int | float | None) -> str:
    if value is None:
        return "不明"
    try:
        return SCALE_LABELS.get(int(round(float(value))), "不明")
    except (TypeError, ValueError):
        return "不明"


def jma_maxi_to_scale(value: object) -> int | None:
    """Convierte el campo `maxi` de las listas JMA (1..7, 5-, 5+)."""
    if value is None:
        return None
    text = str(value).strip()
    mapping = {
        "1": 10,
        "2": 20,
        "3": 30,
        "4": 40,
        "5-": 45,
        "5弱": 45,
        "5+": 50,
        "5強": 50,
        "6-": 55,
        "6弱": 55,
        "6+": 60,
        "6強": 60,
        "7": 70,
    }
    return mapping.get(text)


def grade_rank(value: str | None) -> int:
    return TSUNAMI_GRADE_RANK.get(str(value or "Unknown"), 0)


def typhoon_class_rank(value: str | None) -> int:
    text = str(value or "")
    for needle, rank in _TYPHOON_CLASS_RANK:
        if needle in text:
            return rank
    return 0


def probability_band(probability: int | float | None) -> int:
    try:
        value = float(probability or 0)
    except (TypeError, ValueError):
        return 0
    if value >= 70:
        return 3
    if value >= 50:
        return 2
    if value >= 30:
        return 1
    return 0


def haversine_km(
    lat1: float | None,
    lon1: float | None,
    lat2: float | None,
    lon2: float | None,
) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    try:
        phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
        dphi = math.radians(float(lat2) - float(lat1))
        dlambda = math.radians(float(lon2) - float(lon1))
    except (TypeError, ValueError):
        return None
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return round(6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


def sha1_12(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def eew_event_key(event_id: str | None, origin_at: datetime | None, name: str | None) -> str:
    ident = str(event_id or "").strip()
    if ident:
        return f"eew:{ident}"
    raw = f"{iso_z(origin_at) or ''}|{name or ''}"
    return f"eew:{sha1_12(raw)}"


def quake_event_key(jma_eid: str | None, p2p_id: str | None) -> str:
    ident = str(jma_eid or "").strip()
    if ident:
        return f"quake:{ident}"
    return f"quake:{str(p2p_id or '').strip() or sha1_12(str(utcnow()))}"


def tsunami_event_key(jma_eid: str | None, issue_time: datetime | None) -> str:
    ident = str(jma_eid or "").strip()
    if ident:
        return f"tsunami:{ident}"
    return f"tsunami:{sha1_12(iso_z(issue_time) or str(utcnow()))}"


def typhoon_event_key(event_id: str | None) -> str:
    return f"typhoon:{str(event_id or '').strip() or sha1_12(str(utcnow()))}"


def dedupe_key(kind: str, event_key: str, rank: int) -> str:
    raw = f"{kind}:{sha1_12(event_key)}:{int(rank)}"
    return raw[:120]


def clamp_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def within(value: datetime | None, reference: datetime, max_seconds: float) -> bool:
    if value is None:
        return False
    return abs((to_utc(value) - to_utc(reference)).total_seconds()) <= max_seconds


def hours_ago(value: datetime | None, hours: float) -> bool:
    return value is not None and to_utc(value) < utcnow() - timedelta(hours=hours)
