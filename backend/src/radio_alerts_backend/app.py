from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import internal, public, sse
from .bus import EventBus
from .config import Settings
from .discord import DiscordNotifier
from .jma.service import JMAService
from .normalize import iso_z, utcnow
from .p2p.client import P2PClient
from .pipeline import Pipeline
from .policy import Policy
from .services import Services
from .store import EventStore
from .tts_client import TtsClient
from .typhoon import event_from_snapshot

logger = logging.getLogger(__name__)

_CLEANUP_SECONDS = 24 * 3600


def build_services(settings: Settings) -> Services:
    bus = EventBus()
    store = EventStore(
        settings.data_dir,
        history_limit=settings.history_limit,
        history_days=settings.history_days,
        bus=bus,
    )
    notifier = DiscordNotifier(settings.discord_webhook_url)
    jma = JMAService(settings)
    policy = Policy(settings)
    tts = TtsClient(settings, store, notifier=notifier)
    pipeline = Pipeline(settings, store, policy, jma, tts)
    return Services(
        settings=settings,
        store=store,
        bus=bus,
        policy=policy,
        jma=jma,
        tts=tts,
        pipeline=pipeline,
        notifier=notifier,
    )


def create_app(
    settings: Settings,
    *,
    services: Services | None = None,
    start_background: bool = True,
) -> FastAPI:
    service = services or build_services(settings)
    settings = service.settings
    tasks: list[asyncio.Task[None]] = []
    stop = asyncio.Event()

    if start_background and service.p2p is None:
        service.p2p = _build_p2p(service)
    if service.p2p is None:
        stop.set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loaded = await service.store.load()
        logger.info(
            "app.start",
            extra={"replayed_events": loaded, "config": settings.safe_summary()},
        )
        if start_background:
            await service.store.cleanup()
            if service.p2p is not None:
                service.p2p.start()
            if settings.jma_enabled and settings.typhoon_enabled:
                tasks.append(asyncio.create_task(_typhoon_loop(service, stop)))
            tasks.append(asyncio.create_task(_cleanup_loop(service, stop)))
        try:
            yield
        finally:
            stop.set()
            if service.p2p is not None:
                await service.p2p.stop()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await service.tts.aclose()
            await service.jma.aclose()
            if service.notifier is not None:
                await service.notifier.aclose()
            logger.info("app.stop")

    app = FastAPI(
        title="Kuma Radio Alerts",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.services = service
    app.include_router(public.router)
    app.include_router(sse.router)
    app.include_router(internal.router)
    return app


def _build_p2p(service: Services) -> P2PClient:
    settings = service.settings
    notified = False
    client: P2PClient

    async def on_connection(connected: bool) -> None:
        nonlocal notified
        await service.bus.publish(
            "status",
            {
                "p2p": {"enabled": True, **client.status.to_dict()},
                "connected": connected,
                "at": iso_z(utcnow()),
            },
        )
        if connected:
            if notified and service.notifier is not None:
                await service.notifier("p2p_reconnected", {})
            notified = False

    async def on_attempts(attempts: int) -> None:
        nonlocal notified
        notified = True
        if service.notifier is not None:
            await service.notifier("p2p_down", {"attempts": attempts})

    client = P2PClient(
        settings.p2p_ws_url,
        settings.p2p_code_set,
        service.pipeline.handle_p2p,
        backoff_initial=settings.p2p_backoff_initial,
        backoff_factor=settings.p2p_backoff_factor,
        backoff_max=settings.p2p_backoff_max,
        backoff_jitter=settings.p2p_backoff_jitter,
        alert_after_attempts=settings.p2p_alert_after_attempts,
        on_connection_change=on_connection,
        on_attempts_exceeded=on_attempts,
    )
    return client


async def _typhoon_loop(service: Services, stop: asyncio.Event) -> None:
    failures = 0
    while not stop.is_set():
        snapshot = None
        try:
            snapshot = await service.jma.refresh_typhoons()
        except Exception as exc:
            logger.warning("jma.typhoon_error", extra={"error": type(exc).__name__})

        if snapshot is not None:
            event = event_from_snapshot(snapshot)
            if event is not None:
                existing = service.store.get(event.event_key)
                if existing is None or existing.typhoon != event.typhoon:
                    await service.pipeline.handle_event(event)

        if service.jma.status.last_error:
            failures += 1
            if failures == 3 and service.notifier is not None:
                await service.notifier(
                    "jma_failing", {"error": service.jma.status.last_error}
                )
        else:
            failures = 0

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=service.settings.typhoon_poll_seconds)


async def _cleanup_loop(service: Services, stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_CLEANUP_SECONDS)
        if stop.is_set():
            return
        try:
            await service.store.cleanup()
        except Exception as exc:
            logger.warning("store.cleanup_error", extra={"error": type(exc).__name__})
