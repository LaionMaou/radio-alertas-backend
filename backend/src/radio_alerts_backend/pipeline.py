from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .announce import render
from .config import Settings
from .jma.service import JMAService
from .models import Event, EventKind, EventStatus, make_announcement
from .normalize import (
    iso_z,
    quake_event_key,
    scale_code,
    scale_label,
    tsunami_event_key,
    utcnow,
)
from .p2p.parse import parse_message
from .policy import Policy
from .store import EventStore
from .tts_client import TtsClient

logger = logging.getLogger(__name__)


class Pipeline:
    """P2P/JMA → evento normalizado → política → cadena TTS."""

    def __init__(
        self,
        settings: Settings,
        store: EventStore,
        policy: Policy,
        jma: JMAService,
        tts: TtsClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.policy = policy
        self.jma = jma
        self.tts = tts

    async def handle_p2p(
        self,
        payload: Mapping[str, Any],
        *,
        source: str = "p2p",
    ) -> Event | None:
        result = parse_message(payload)
        if result.event is None:
            logger.info("p2p.event_ignored", extra={"reason": result.reason})
            return None
        event = result.event
        event.source = source

        await self._enrich(event)

        age = (utcnow() - event.issued_at).total_seconds()
        if source != "simulate" and age > self.settings.p2p_stale_event_seconds:
            event.status = EventStatus.STALE

        logger.info(
            "p2p.event",
            extra={
                "kind": event.kind.value,
                "event_key": event.event_key,
                "status": event.status.value,
                "source": source,
            },
        )

        if event.kind == EventKind.EARTHQUAKE and event.status != EventStatus.STALE:
            related = self._related_eew(event)
            if related is not None:
                logger.info(
                    "p2p.correlated",
                    extra={"eew_key": related.event_key, "quake_key": event.event_key},
                )
                return await self._process(self._merge_quake(related, event))

        return await self._process(event)

    async def handle_event(self, event: Event, *, source: str | None = None) -> Event:
        if source is not None:
            event.source = source
        return await self._process(event)

    async def _enrich(self, event: Event) -> None:
        if not self.settings.jma_enabled or event.status == EventStatus.CANCELLED:
            return
        if event.kind == EventKind.EARTHQUAKE:
            epicenter = event.epicenter or {}
            enrichment = await self.jma.enrich_quake(
                origin_at=event.origin_at,
                name=epicenter.get("name"),
                lat=epicenter.get("lat"),
                lon=epicenter.get("lon"),
            )
            if enrichment is None:
                return
            event.event_key = quake_event_key(enrichment.eid, event.raw_id)
            event.enrichment = {
                **(event.enrichment or {}),
                "jma_eid": enrichment.eid,
                "detail": enrichment.detail,
                "source": "jma",
            }
            self._apply_intensity(event, enrichment.maxi)
            self._apply_magnitude(event, enrichment.magnitude)
        elif event.kind == EventKind.TSUNAMI:
            enrichment = await self.jma.enrich_tsunami(issued_at=event.issued_at)
            if enrichment is None:
                return
            event.event_key = tsunami_event_key(enrichment.eid, event.issued_at)
            event.enrichment = {
                **(event.enrichment or {}),
                "jma_eid": enrichment.eid,
                "detail": enrichment.detail,
                "source": "jma",
            }

    def _apply_intensity(self, event: Event, scale: int | None) -> None:
        if scale is None:
            return
        current = (event.max_intensity or {}).get("value")
        if current is not None and scale_code(current) >= scale_code(scale):
            return
        event.max_intensity = {"value": scale, "label": scale_label(scale)}

    def _apply_magnitude(self, event: Event, magnitude: float | None) -> None:
        if magnitude is None:
            return
        epicenter = dict(event.epicenter or {})
        if epicenter.get("magnitude") is None:
            epicenter["magnitude"] = magnitude
            event.epicenter = epicenter

    def _related_eew(self, earthquake: Event) -> Event | None:
        epicenter = earthquake.epicenter or {}
        related = self.store.find_related_eew(
            earthquake.origin_at,
            epicenter.get("lat"),
            epicenter.get("lon"),
        )
        if related is None or related.announced_rank <= 0:
            return None
        return related

    def _merge_quake(self, related: Event, observed: Event) -> Event:
        epicenter = observed.epicenter or {}
        enrichment = dict(related.enrichment or {})
        enrichment["quake"] = {
            "event_key": observed.event_key,
            "p2p_id": observed.raw_id,
            "max_intensity": observed.max_intensity,
            "epicenter": epicenter,
            "observed_at": iso_z(observed.issued_at),
        }
        related.enrichment = enrichment
        observed_scale = (observed.max_intensity or {}).get("value")
        related_scale = (related.max_intensity or {}).get("value")
        if scale_code(observed_scale) > scale_code(related_scale):
            related.max_intensity = dict(observed.max_intensity or {})
        if (related.epicenter or {}).get("magnitude") is None and epicenter.get("magnitude"):
            related.epicenter = {**(related.epicenter or {}), "magnitude": epicenter["magnitude"]}
        if observed.origin_at is not None:
            related.origin_at = observed.origin_at
        return related

    async def _process(self, event: Event) -> Event:
        event = await self.store.upsert(event)
        decision = self.policy.decide(event)
        logger.info(
            "policy.decision",
            extra={
                "kind": event.kind.value,
                "event_key": event.event_key,
                "rank": decision.rank,
                "announce": decision.announce,
                "reason": decision.reason,
                "tone": decision.tone,
                "loop": decision.loop,
            },
        )
        if not decision.announce:
            return event

        text = decision.text or render(event)
        outcome = await self.tts.submit(
            event,
            text=text,
            dedupe_key=decision.dedupe_key,
            tone=decision.tone,
            loop=decision.loop,
            preempt=decision.preempt,
        )
        announcement = make_announcement(
            version=event.version,
            rank=decision.rank,
            alert_id=outcome.alert_id,
            status=outcome.status.value,
            tone=decision.tone,
            loop=decision.loop,
        )
        updated = await self.store.record_announcement(event.event_key, announcement)
        return updated or event
