from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .bus import EventBus
from .models import Event, EventKind, EventStatus, TtsStatus, default_tts
from .normalize import haversine_km, iso_z, utcnow

logger = logging.getLogger(__name__)

_REPLAY_RECORDS = 500
_FILE_PREFIX = "events-"


class EventStore:
    def __init__(
        self,
        data_dir: Path | str,
        *,
        history_limit: int = 50,
        history_days: int = 30,
        bus: EventBus | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._dir = Path(data_dir)
        self._limit = history_limit
        self._days = history_days
        self._bus = bus
        self._clock = clock
        self._events: OrderedDict[str, Event] = OrderedDict()
        self._seq = 0
        self._lock = asyncio.Lock()

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def bus(self) -> EventBus | None:
        return self._bus

    def get(self, event_key: str) -> Event | None:
        return self._events.get(event_key)

    def latest(self, kind: EventKind | str | None = None) -> Event | None:
        candidates = self._filter(kind)
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.issued_at, item.received_at))

    def latest_public(self, kind: EventKind | str | None = None) -> dict[str, Any] | None:
        event = self.latest(kind)
        return self._public(event) if event else None

    def history(
        self,
        limit: int = 20,
        kind: EventKind | str | None = None,
    ) -> list[dict[str, Any]]:
        items = sorted(
            self._filter(kind),
            key=lambda item: (item.issued_at, item.received_at),
            reverse=True,
        )
        return [self._public(item) for item in items[: max(1, int(limit))]]

    def latest_by_kind(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for kind in EventKind:
            event = self.latest(kind)
            if event is not None:
                result[kind.value] = self._public(event)
        return result

    def find_related_eew(
        self,
        origin_at: datetime | None,
        latitude: float | None,
        longitude: float | None,
        *,
        window_seconds: float = 900,
        max_km: float = 100,
    ) -> Event | None:
        if origin_at is None or latitude is None or longitude is None:
            return None
        best: Event | None = None
        best_distance = max_km
        for event in self._events.values():
            if event.kind != EventKind.EEW or event.status == EventStatus.CANCELLED:
                continue
            if event.is_expired(self._clock()):
                continue
            if event.origin_at is None:
                continue
            delta = abs((event.origin_at - origin_at).total_seconds())
            if delta > window_seconds:
                continue
            epicenter = event.epicenter or {}
            distance = haversine_km(
                latitude,
                longitude,
                epicenter.get("lat"),
                epicenter.get("lon"),
            )
            if distance is None or distance > best_distance:
                continue
            best = event
            best_distance = distance
        return best

    async def load(self) -> int:
        async with self._lock:
            records: deque[dict[str, Any]] = deque(maxlen=_REPLAY_RECORDS)
            for path in self._event_files():
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        for line in handle:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(record, dict):
                                records.append(record)
                except OSError as exc:
                    logger.warning(
                        "store.replay_error",
                        extra={"file": path.name, "error": str(exc)},
                    )
            for record in records:
                self._apply_record(record)
            self._trim()
            return len(self._events)

    async def upsert(self, event: Event) -> Event:
        async with self._lock:
            existing = self._events.get(event.event_key)
            if existing is not None:
                if event.version <= existing.version:
                    event.version = existing.version + 1
                event.announcements = _merge_announcements(
                    existing.announcements, event.announcements
                )
                if event.tts.get("status", TtsStatus.NONE.value) == TtsStatus.NONE.value:
                    event.tts = dict(existing.tts)
                if event.rank < existing.rank:
                    event.rank = existing.rank

            event.seq = self._next_seq()
            self._events[event.event_key] = event
            self._events.move_to_end(event.event_key)
            self._trim()
            await self._persist({"record_type": "event", **event.to_record()})
            await self._publish(event)
            return event

    async def update_tts(self, event_key: str, tts: dict[str, Any]) -> Event | None:
        async with self._lock:
            event = self._events.get(event_key)
            if event is None:
                return None
            merged = {**default_tts(), **event.tts, **tts}
            merged["updated_at"] = tts.get("updated_at") or iso_z(self._clock())
            event.tts = merged
            event.seq = self._next_seq()
            await self._persist(
                {
                    "record_type": "tts",
                    "event_key": event_key,
                    "seq": event.seq,
                    "tts": event.tts,
                }
            )
            await self._publish(event)
            return event

    async def clear_loops(self) -> int:
        keys = [
            key
            for key, event in list(self._events.items())
            if event.tts.get("loop_active")
        ]
        for key in keys:
            await self.update_tts(key, {"loop_active": False})
        return len(keys)

    async def record_announcement(
        self,
        event_key: str,
        announcement: dict[str, Any],
    ) -> Event | None:
        async with self._lock:
            event = self._events.get(event_key)
            if event is None:
                return None
            event.announcements = _merge_announcements(
                event.announcements, [announcement]
            )
            try:
                event.rank = max(event.rank, int(announcement.get("rank") or 0))
            except (TypeError, ValueError):
                pass
            try:
                event.version = max(
                    event.version, int(announcement.get("version") or 0)
                )
            except (TypeError, ValueError):
                pass
            event.seq = self._next_seq()
            await self._persist({"record_type": "event", **event.to_record()})
            await self._publish(event)
            return event

    def snapshot(self, limit: int = 50) -> dict[str, Any]:
        return {
            "seq": self._seq,
            "alert": self.latest_public(),
            "alerts": self.history(limit),
        }

    def status(self) -> dict[str, Any]:
        counts = {kind.value: 0 for kind in EventKind}
        for event in self._events.values():
            counts[event.kind.value] = counts.get(event.kind.value, 0) + 1
        return {
            "events": len(self._events),
            "by_kind": counts,
            "latest_seq": self._seq,
        }

    async def cleanup(self, now: datetime | None = None) -> int:
        cutoff = (now or self._clock()).astimezone(UTC).date() - timedelta(days=self._days)
        removed = 0
        for path in self._event_files():
            try:
                file_date = date.fromisoformat(path.stem[len(_FILE_PREFIX) :])
            except ValueError:
                continue
            if file_date >= cutoff:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning(
                    "store.cleanup_error", extra={"file": path.name, "error": str(exc)}
                )
        return removed

    def _filter(self, kind: EventKind | str | None) -> list[Event]:
        selected: list[Event] = []
        for event in self._events.values():
            if event.status == EventStatus.TEST:
                continue
            if kind is not None:
                expected = kind.value if isinstance(kind, EventKind) else str(kind)
                if event.kind.value != expected:
                    continue
            selected.append(event)
        return selected

    def _public(self, event: Event) -> dict[str, Any]:
        data = event.to_public()
        if (
            event.status in (EventStatus.ACTIVE, EventStatus.UPDATED)
            and event.is_expired(self._clock())
        ):
            data["status"] = EventStatus.EXPIRED.value
        return data

    async def _publish(self, event: Event) -> None:
        if self._bus is None:
            return
        await self._bus.publish("alert", self._public(event), seq=event.seq)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _trim(self) -> None:
        while len(self._events) > self._limit:
            oldest = min(
                self._events,
                key=lambda key: (self._events[key].issued_at, self._events[key].received_at),
            )
            del self._events[oldest]

    def _event_files(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(self._dir.glob(f"{_FILE_PREFIX}*.jsonl"))

    def _apply_record(self, record: dict[str, Any]) -> None:
        record_type = record.get("record_type", "event")
        if record_type == "tts":
            event = self._events.get(str(record.get("event_key") or ""))
            tts = record.get("tts")
            if event is not None and isinstance(tts, dict):
                event.tts = {**default_tts(), **tts}
                try:
                    event.seq = max(event.seq, int(record.get("seq") or 0))
                except (TypeError, ValueError):
                    pass
                self._seq = max(self._seq, event.seq)
            return
        try:
            event = Event.from_record(record)
        except (KeyError, TypeError, ValueError):
            return
        if not event.event_key:
            return
        existing = self._events.get(event.event_key)
        if existing is not None:
            if event.version < existing.version:
                event.version = existing.version
            event.announcements = _merge_announcements(
                existing.announcements, event.announcements
            )
        self._events[event.event_key] = event
        self._events.move_to_end(event.event_key)
        self._seq = max(self._seq, event.seq)

    async def _persist(self, record: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._append_line, record)
        except OSError as exc:
            logger.warning("store.persist_error", extra={"error": str(exc)})

    def _append_line(self, record: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        day = self._clock().astimezone(UTC).date().isoformat()
        path = self._dir / f"{_FILE_PREFIX}{day}.jsonl"
        line = json.dumps(record, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


def _merge_announcements(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for item in [*existing, *incoming]:
        key = (item.get("version"), item.get("rank"), item.get("at"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(item))
    return merged
