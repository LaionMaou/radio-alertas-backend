from __future__ import annotations

import hmac
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..announce import render
from ..models import Event, EventKind
from ..normalize import dedupe_key, sha1_12, utcnow
from ..typhoon import event_from_mapping
from .common import services

logger = logging.getLogger(__name__)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_FIXTURE_NAMES = frozenset({"eew", "quake", "tsunami", "typhoon"})
_KINDS = {
    "eew": EventKind.EEW,
    "earthquake": EventKind.EARTHQUAKE,
    "tsunami": EventKind.TSUNAMI,
    "typhoon": EventKind.TYPHOON,
}


class TriggerIn(BaseModel):
    event_key: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=2000)
    kind: str | None = Field(default=None, max_length=20)
    magnitude: float | None = Field(default=None, ge=0, le=12)
    title: str | None = Field(default=None, max_length=200)


class SimulateIn(BaseModel):
    payload: dict[str, Any] | None = None
    fixture: str | None = Field(default=None, max_length=20)
    typhoon: dict[str, Any] | None = None


def require_internal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = services(request).settings.internal_token
    provided = authorization or ""
    if not expected or not hmac.compare_digest(provided, f"Bearer {expected}"):
        raise HTTPException(
            status_code=401,
            detail="No autorizado",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal)])


@router.post("/trigger-tts")
async def trigger_tts(payload: TriggerIn, request: Request) -> dict[str, object]:
    service = services(request)
    event: Event | None = None
    if payload.event_key:
        event = service.store.get(payload.event_key)
        if event is None:
            raise HTTPException(status_code=404, detail="Evento no encontrado")
    elif payload.text:
        event = Event(
            event_key=f"manual:{sha1_12(payload.text)}",
            kind=_KINDS.get(str(payload.kind or ""), EventKind.EARTHQUAKE),
            issued_at=utcnow(),
            title=payload.title or "手動テスト",
            summary=payload.text[:200],
            epicenter={"magnitude": payload.magnitude} if payload.magnitude else None,
            source="manual",
        )
        event = await service.store.upsert(event)
    else:
        raise HTTPException(
            status_code=422,
            detail="Se requiere event_key o text",
        )

    text = payload.text or render(event)
    target = replace(event, title=payload.title) if payload.title else event
    key = dedupe_key(target.kind.value, target.event_key, target.rank)
    outcome = await service.tts.submit(target, text=text, dedupe_key=key)
    logger.info(
        "internal.trigger_tts",
        extra={
            "event_key": target.event_key,
            "status": outcome.status.value,
            "chars": len(text),
        },
    )
    return {
        "event_key": target.event_key,
        "status": outcome.status.value,
        "alert_id": outcome.alert_id,
        "duplicate_of": outcome.duplicate_of,
        "error": outcome.error,
        "text": text,
    }


@router.post("/stop-tts")
async def stop_tts(request: Request) -> dict[str, object]:
    service = services(request)
    result = await service.tts.stop()
    if result.get("error"):
        logger.error(
            "internal.stop_tts_failed", extra={"error": str(result.get("error"))}
        )
        raise HTTPException(status_code=502, detail="La cadena TTS no confirmó el corte")

    cleared = await service.store.clear_loops()
    logger.info(
        "internal.stop_tts",
        extra={
            "stopped": bool(result.get("stopped")),
            "status": result.get("status"),
            "cleared": cleared,
        },
    )
    return {
        "stopped": bool(result.get("stopped")),
        "status": result.get("status"),
        "cleared": cleared,
    }


@router.post("/simulate")
async def simulate(body: SimulateIn, request: Request) -> dict[str, object]:
    service = services(request)
    if not service.settings.enable_simulate:
        raise HTTPException(status_code=404, detail="No encontrado")

    if body.typhoon:
        event = event_from_mapping(body.typhoon, source="simulate")
        stored = await service.pipeline.handle_event(event, source="simulate")
        return {"event": stored.to_public()}

    payload = body.payload
    if body.fixture:
        payload = _load_fixture(body.fixture)
        if body.fixture == "typhoon":
            event = event_from_mapping(payload, source="simulate")
            stored = await service.pipeline.handle_event(event, source="simulate")
            return {"event": stored.to_public()}

    if not payload:
        raise HTTPException(status_code=422, detail="Se requiere payload o fixture")
    event = await service.pipeline.handle_p2p(payload, source="simulate")
    return {"event": event.to_public() if event else None}


def _load_fixture(name: str) -> dict[str, Any]:
    if name not in _FIXTURE_NAMES:
        raise HTTPException(status_code=422, detail="Fixture desconocido")
    path = _FIXTURES / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=500, detail="Fixture no disponible") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Fixture inválido")
    return data
