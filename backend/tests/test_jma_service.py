from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from defusedxml import ElementTree as ET

from radio_alerts_backend.config import Settings
from radio_alerts_backend.jma.service import (
    EXTRA_FEED_URL,
    QUAKE_LIST_URL,
    TSUNAMI_LIST_URL,
    JMAService,
)
from radio_alerts_backend.normalize import JST

FIXTURES = Path(__file__).parent / "fixtures" / "jma"


def jst(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=JST).astimezone(UTC)


def feed_url(stamp: str, name: str) -> str:
    return (
        "https://www.data.jma.go.jp/developer/xml/data/"
        f"{stamp}_0_{name}_010000.xml"
    )


class FakeClient:
    def __init__(self, *, json_data=None, xml_files=None) -> None:
        self.json_data = json_data or {}
        self.xml_files = xml_files or {}
        self.calls: list[str] = []
        self.closed = False

    async def get_json(self, url: str, *, ttl: float):
        self.calls.append(url)
        return self.json_data.get(url)

    async def get_xml(self, url: str, *, ttl: float):
        self.calls.append(url)
        text = self.xml_files.get(url)
        return ET.fromstring(text) if text is not None else None

    async def aclose(self) -> None:
        self.closed = True


def settings(**kwargs) -> Settings:
    values = {
        "internal_token": "test-token",
        "typhoon_areas": "130000",
        "jma_enabled": True,
        "typhoon_enabled": True,
    }
    values.update(kwargs)
    return Settings(**values)


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


async def test_enrich_quake_returns_jma_identifier() -> None:
    client = FakeClient(json_data={QUAKE_LIST_URL: load_json("quake_list.json")})
    service = JMAService(settings(), client=client)

    enrichment = await service.enrich_quake(
        origin_at=jst(2026, 10, 25, 12, 59),
        name="宮城県沖",
        lat=38.1,
        lon=142.0,
    )

    assert enrichment is not None
    assert enrichment.eid == "20261025125900"
    assert enrichment.maxi == 50
    assert enrichment.magnitude == 7.2
    assert service.status.last_error is None
    assert service.status.last_success_at is not None


async def test_enrich_quake_without_match_keeps_event_alive() -> None:
    client = FakeClient(json_data={QUAKE_LIST_URL: load_json("quake_list.json")})
    service = JMAService(settings(), client=client)

    enrichment = await service.enrich_quake(
        origin_at=jst(2026, 9, 1, 0, 0),
        name="宮城県沖",
        lat=38.1,
        lon=142.0,
    )

    assert enrichment is None
    assert service.status.last_error is None


async def test_enrich_tsunami_returns_jma_identifier() -> None:
    client = FakeClient(json_data={TSUNAMI_LIST_URL: load_json("tsunami_list.json")})
    service = JMAService(settings(), client=client)

    enrichment = await service.enrich_tsunami(issued_at=jst(2026, 10, 25, 13, 10))

    assert enrichment is not None
    assert enrichment.eid == "20261025125900"


async def test_refresh_typhoons_combines_both_bulletins() -> None:
    client = FakeClient(
        xml_files={
            EXTRA_FEED_URL: load_text("extra.xml"),
            feed_url("20261025184826", "VPTW61"): load_text("vptw61.xml"),
            feed_url("20261025185500", "VPTA51"): load_text("vpta51.xml"),
        }
    )
    service = JMAService(settings(), client=client)

    snapshot = await service.refresh_typhoons()

    assert snapshot is not None
    assert snapshot.info is not None
    assert snapshot.info.name == "IN-FA"
    assert len(snapshot.probabilities) == 1
    assert snapshot.probabilities[0].probability == 55
    assert service.status.typhoon_name == "台風31号 IN-FA"
    assert service.status.typhoon_updated_at is not None
    assert not any("VPTW60" in call for call in client.calls)


async def test_refresh_typhoons_keeps_previous_snapshot_on_failure() -> None:
    client = FakeClient(
        xml_files={
            EXTRA_FEED_URL: load_text("extra.xml"),
            feed_url("20261025184826", "VPTW61"): load_text("vptw61.xml"),
            feed_url("20261025185500", "VPTA51"): load_text("vpta51.xml"),
        }
    )
    service = JMAService(settings(), client=client)

    first = await service.refresh_typhoons()
    client.xml_files.clear()
    second = await service.refresh_typhoons()

    assert first is not None
    assert second is first
    assert service.status.last_error is not None


async def test_disabled_jma_does_not_call_upstream() -> None:
    client = FakeClient()
    service = JMAService(settings(jma_enabled=False), client=client)

    assert await service.enrich_quake(
        origin_at=jst(2026, 10, 25, 12, 59),
        name="宮城県沖",
        lat=38.1,
        lon=142.0,
    ) is None
    assert await service.refresh_typhoons() is None
    assert client.calls == []
