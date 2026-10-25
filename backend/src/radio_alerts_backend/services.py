from __future__ import annotations

from dataclasses import dataclass

from .bus import EventBus
from .config import Settings
from .discord import DiscordNotifier
from .jma.service import JMAService
from .p2p.client import P2PClient
from .pipeline import Pipeline
from .policy import Policy
from .store import EventStore
from .tts_client import TtsClient


@dataclass
class Services:
    settings: Settings
    store: EventStore
    bus: EventBus
    policy: Policy
    jma: JMAService
    tts: TtsClient
    pipeline: Pipeline
    notifier: DiscordNotifier | None = None
    p2p: P2PClient | None = None

    def status(self) -> dict[str, object]:
        p2p_status: dict[str, object]
        if self.p2p is None:
            p2p_status = {"enabled": False, "connected": False}
        else:
            p2p_status = {"enabled": True, **self.p2p.status.to_dict()}
        jma_status = {"enabled": self.jma.enabled, **self.jma.status.to_dict()}
        degraded = bool(self.p2p is not None and not self.p2p.status.connected)
        degraded = degraded or bool(self.jma.status.last_error)
        return {
            "status": "degraded" if degraded else "ok",
            "dry_run": self.settings.dry_run,
            "p2p": p2p_status,
            "jma": jma_status,
            "tts": self.tts.status(),
            "store": self.store.status(),
            "sse_clients": self.bus.subscriber_count,
            "station": self.settings.station_name,
        }
