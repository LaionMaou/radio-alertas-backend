from __future__ import annotations

from typing import Any

from .announce import render
from .config import Settings
from .models import Decision, Event, EventKind, EventStatus
from .normalize import (
    VALID_SCALES,
    dedupe_key,
    grade_rank,
    probability_band,
    scale_code,
    typhoon_class_rank,
)


class Policy:
    """Decide si un evento se anuncia por radio, una vez por escalada real."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def decide(self, event: Event) -> Decision:
        rank = self.rank(event)

        if event.status == EventStatus.TEST:
            return self._no(event, rank, "test")
        if event.status == EventStatus.CANCELLED:
            return self._no(event, rank, "cancelled")
        if event.status == EventStatus.STALE:
            return self._no(event, rank, "stale")

        rejection = self._rejection(event)
        if rejection:
            return self._no(event, rank, rejection)

        announced = event.announced_rank
        if announced and rank <= announced:
            return self._no(event, rank, "no_escalation")

        loop = event.kind == EventKind.TSUNAMI and _major_tsunami(event)
        return Decision(
            announce=True,
            rank=rank,
            version=event.version,
            reason="escalation" if announced else "initial",
            text=render(event),
            dedupe_key=dedupe_key(event.kind.value, event.event_key, rank),
            tone="urgent" if loop or _severe_shaking(event) else "soft",
            loop=loop,
            preempt=loop,
        )

    def rank(self, event: Event) -> int:
        if event.kind in (EventKind.EEW, EventKind.EARTHQUAKE):
            scale = scale_code((event.max_intensity or {}).get("value"))
            magnitude = _magnitude(event) or 0.0
            return scale * 100 + int(min(max(magnitude, 0.0), 12.0) * 2)

        if event.kind == EventKind.TSUNAMI:
            data = event.tsunami or {}
            grade = str(data.get("grade") or data.get("domestic") or "Unknown")
            return grade_rank(grade) * 10 + (1 if data.get("immediate") else 0)

        if event.kind == EventKind.TYPHOON:
            data = event.typhoon or {}
            probability = self._typhoon_probability(event)
            class_text = str(data.get("class") or data.get("class_text") or "")
            return typhoon_class_rank(class_text) * 10 + probability_band(probability)

        return 0

    def _rejection(self, event: Event) -> str | None:
        if event.kind in (EventKind.EEW, EventKind.EARTHQUAKE):
            if event.kind == EventKind.EEW and not self.settings.eew_enabled:
                return "eew_disabled"
            scale = _usable_scale(event.max_intensity)
            magnitude = _magnitude(event)
            if scale is None and magnitude is None:
                return "insufficient_data"
            scale_ok = scale is not None and scale_code(scale) >= self.settings.quake_min_scale
            magnitude_ok = (
                magnitude is not None
                and magnitude >= self.settings.quake_min_magnitude
            )
            if not (scale_ok or magnitude_ok):
                return "below_threshold"
            return None

        if event.kind == EventKind.TSUNAMI:
            data = event.tsunami or {}
            grade = str(data.get("grade") or data.get("domestic") or "Unknown")
            if grade_rank(grade) < self.settings.tsunami_min_rank:
                return "below_threshold"
            return None

        if event.kind == EventKind.TYPHOON:
            if not self.settings.typhoon_enabled or not self.settings.typhoon_ann_tts:
                return "typhoon_web_only"
            probability = self._typhoon_probability(event)
            if probability is None:
                return "typhoon_no_area"
            if probability < self.settings.typhoon_probability_threshold:
                return "below_threshold"
            return None

        return "unsupported"

    def _typhoon_probability(self, event: Event) -> int | None:
        data = event.typhoon or {}
        codes = self.settings.typhoon_area_set
        if not codes:
            return None
        areas = data.get("areas")
        if not isinstance(areas, list):
            return None
        best: int | None = None
        for area in areas:
            if not isinstance(area, dict):
                continue
            if str(area.get("code") or "") not in codes:
                continue
            probability = _as_int(area.get("probability"))
            if probability is None:
                continue
            best = probability if best is None else max(best, probability)
        return best

    def _no(self, event: Event, rank: int, reason: str) -> Decision:
        return Decision(
            announce=False,
            rank=rank,
            version=event.version,
            reason=reason,
        )


def _major_tsunami(event: Event) -> bool:
    data = event.tsunami or {}
    return str(data.get("grade") or data.get("domestic") or "") == "MajorWarning"


def _severe_shaking(event: Event) -> bool:
    if event.kind not in (EventKind.EEW, EventKind.EARTHQUAKE):
        return False
    return scale_code((event.max_intensity or {}).get("value")) >= 70


def _usable_scale(intensity: dict[str, Any] | None) -> int | None:
    value = (intensity or {}).get("value")
    if value is None or isinstance(value, bool):
        return None
    try:
        code = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if code not in VALID_SCALES or code < 0:
        return None
    return code


def _magnitude(event: Event) -> float | None:
    value = (event.epicenter or {}).get("magnitude")
    if value is None or isinstance(value, bool):
        return None
    try:
        magnitude = float(value)
    except (TypeError, ValueError):
        return None
    return magnitude if magnitude >= 0 else None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None
