from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from .common import services

router = APIRouter()


@router.get("/api/alert/latest")
def latest_alert(request: Request, kind: str | None = None) -> dict[str, object]:
    store = services(request).store
    return {"alert": store.latest_public(kind)}


@router.get("/api/alert/history")
def alert_history(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    kind: str | None = None,
) -> dict[str, object]:
    store = services(request).store
    alerts = store.history(limit=limit, kind=kind)
    return {"alerts": alerts, "count": len(alerts)}


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    return services(request).status()
