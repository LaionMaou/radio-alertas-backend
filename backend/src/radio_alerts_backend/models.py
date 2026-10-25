from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .normalize import iso_z, parse_iso, utcnow


class EventKind(StrEnum):
    EEW = "eew"
    EARTHQUAKE = "earthquake"
    TSUNAMI = "tsunami"
    TYPHOON = "typhoon"


class EventStatus(StrEnum):
    ACTIVE = "active"
    UPDATED = "updated"
    CANCELLED = "cancelled"
    STALE = "stale"
    EXPIRED = "expired"
    TEST = "test"


class TtsStatus(StrEnum):
    NONE = "none"
    QUEUED = "queued"
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    DRY_RUN = "dry-run"
    DUPLICATE = "duplicate"
    UNKNOWN = "unknown"


class TsunamiGrade(StrEnum):
    NONE = "None"
    UNKNOWN = "Unknown"
    CHECKING = "Checking"
    NON_EFFECTIVE = "NonEffective"
    WATCH = "Watch"
    WARNING = "Warning"
    MAJOR_WARNING = "MajorWarning"


def default_tts() -> dict[str, Any]:
    return {
        "status": TtsStatus.NONE.value,
        "alert_id": None,
        "error": None,
        "updated_at": None,
    }


@dataclass
class Event:
    event_key: str
    kind: EventKind
    issued_at: datetime
    received_at: datetime = field(default_factory=utcnow)
    origin_at: datetime | None = None
    status: EventStatus = EventStatus.ACTIVE
    version: int = 1
    title: str = ""
    summary: str = ""
    epicenter: dict[str, Any] | None = None
    max_intensity: dict[str, Any] | None = None
    tsunami: dict[str, Any] | None = None
    eew: dict[str, Any] | None = None
    typhoon: dict[str, Any] | None = None
    enrichment: dict[str, Any] | None = None
    tts: dict[str, Any] = field(default_factory=default_tts)
    announcements: list[dict[str, Any]] = field(default_factory=list)
    source: str = "p2p"
    raw_id: str = ""
    rank: int = 0
    seq: int = 0

    @property
    def announced_rank(self) -> int:
        ranks = []
        for item in self.announcements:
            try:
                ranks.append(int(item.get("rank", 0)))
            except (TypeError, ValueError):
                continue
        return max(ranks, default=0)

    def expires_at(self) -> datetime:
        hours = 12 if self.kind == EventKind.TYPHOON else 24
        return self.issued_at + timedelta(hours=hours)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at() < (now or utcnow())

    def to_public(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_key": self.event_key,
            "kind": self.kind.value,
            "status": self.status.value,
            "version": self.version,
            "issued_at": iso_z(self.issued_at),
            "origin_at": iso_z(self.origin_at),
            "received_at": iso_z(self.received_at),
            "expires_at": iso_z(self.expires_at()),
            "title": self.title,
            "summary": self.summary,
            "epicenter": self.epicenter,
            "max_intensity": self.max_intensity,
            "tsunami": self.tsunami,
            "eew": self.eew,
            "typhoon": self.typhoon,
            "enrichment": self.enrichment,
            "tts": dict(self.tts),
            "announcements": [dict(item) for item in self.announcements],
            "source": self.source,
        }

    def to_record(self) -> dict[str, Any]:
        data = self.to_public()
        data["rank"] = self.rank
        data["raw_id"] = self.raw_id
        return data

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> Event:
        issued_at = parse_iso(data.get("issued_at")) or utcnow()
        received_at = parse_iso(data.get("received_at")) or issued_at
        status = _enum_or(EventStatus, data.get("status"), EventStatus.ACTIVE)
        if status == EventStatus.TEST:
            status = EventStatus.ACTIVE
        return cls(
            event_key=str(data.get("event_key") or ""),
            kind=EventKind(str(data.get("kind") or "earthquake")),
            issued_at=issued_at,
            received_at=received_at,
            origin_at=parse_iso(data.get("origin_at")),
            status=status,
            version=int(data.get("version") or 1),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            epicenter=_dict_or_none(data.get("epicenter")),
            max_intensity=_dict_or_none(data.get("max_intensity")),
            tsunami=_dict_or_none(data.get("tsunami")),
            eew=_dict_or_none(data.get("eew")),
            typhoon=_dict_or_none(data.get("typhoon")),
            enrichment=_dict_or_none(data.get("enrichment")),
            tts=_dict_or_none(data.get("tts")) or default_tts(),
            announcements=[
                dict(item)
                for item in (data.get("announcements") or [])
                if isinstance(item, dict)
            ],
            source=str(data.get("source") or "p2p"),
            raw_id=str(data.get("raw_id") or ""),
            rank=int(data.get("rank") or 0),
            seq=int(data.get("seq") or 0),
        )


def _dict_or_none(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _enum_or(enum_type, value, default):  # type: ignore[no-untyped-def]
    try:
        return enum_type(str(value))
    except (ValueError, TypeError):
        return default


@dataclass
class Decision:
    announce: bool
    rank: int
    version: int
    reason: str
    text: str | None = None
    dedupe_key: str = ""


@dataclass
class P2PStatus:
    connected: bool = False
    attempts: int = 0
    last_message_at: str | None = None
    last_error: str | None = None
    next_retry_at: str | None = None
    connected_since: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JMAStatus:
    enabled: bool = True
    last_success_at: str | None = None
    last_error: str | None = None
    typhoon_updated_at: str | None = None
    typhoon_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_announcement(
    *,
    version: int,
    rank: int,
    alert_id: str | None,
    status: str,
    at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "version": int(version),
        "rank": int(rank),
        "alert_id": alert_id,
        "status": status,
        "at": iso_z(at or utcnow()),
    }
