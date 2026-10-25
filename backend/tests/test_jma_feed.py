from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from defusedxml import ElementTree as ET

from radio_alerts_backend.jma.feed import (
    parse_feed,
    parse_vpta51,
    parse_vptw61,
    select_latest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "jma"


def xml_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def xml(name: str):
    return ET.fromstring(xml_text(name))


def test_feed_entries_are_parsed() -> None:
    entries = parse_feed(xml("extra.xml"))

    assert len(entries) == 5
    assert entries[0].title == "地震情報"
    assert entries[0].url.endswith("VXSE53_010000.xml")
    assert entries[0].updated_at == datetime(2026, 10, 25, 18, 53, 4, tzinfo=UTC)


def test_feed_selects_latest_typhoon_files_only() -> None:
    latest = select_latest(parse_feed(xml("extra.xml")))

    assert set(latest) == {"VPTW61", "VPTA51"}
    assert "20261025184826" in latest["VPTW61"].url
    assert "20261025185500" in latest["VPTA51"].url
    assert all("VPTW60" not in entry.url for entry in latest.values())


def test_feed_without_typhoons_returns_empty() -> None:
    entries = [
        entry
        for entry in parse_feed(xml("extra.xml"))
        if "VPT" not in entry.url
    ]
    assert select_latest(entries) == {}


def test_vptw61_uses_current_observation() -> None:
    info = parse_vptw61(xml("vptw61.xml"), source_url="https://example/vptw61.xml")

    assert info is not None
    assert info.event_id == "TC2637"
    assert info.report_at == datetime(2026, 10, 25, 18, 50, tzinfo=UTC)
    assert info.serial == "23"
    assert info.info_type == "発表"
    assert info.name == "IN-FA"
    assert info.name_kana == "インファ"
    assert info.number == "2631"
    assert info.number_label == "台風31号"
    assert info.class_text == "台風(STS)"
    assert info.location == "マーシャル諸島"
    assert info.direction == "北西"
    assert info.pressure_hpa == 992
    assert info.wind_mps == 30
    assert info.gust_mps == 45
    assert (info.lat, info.lon) == (13.6, 164.6)


def test_vpta51_keeps_maximum_probability_per_configured_area() -> None:
    result = parse_vpta51(xml("vpta51.xml"), {"130000"})

    assert result.event_id == "TC2637"
    assert result.report_at == datetime(2026, 10, 25, 18, 55, tzinfo=UTC)
    assert len(result.areas) == 1
    area = result.areas[0]
    assert area.code == "130000"
    assert area.name == "東京都"
    assert area.prefecture == "東京都"
    assert area.probability == 55
    assert area.at == datetime(2026, 10, 25, 21, 0, tzinfo=UTC)
    assert area.window == "3時間"


def test_vpta51_aggregates_each_area_code() -> None:
    result = parse_vpta51(xml("vpta51.xml"), {"011000", "130000", "999999"})

    by_code = {area.code: area for area in result.areas}
    assert set(by_code) == {"011000", "130000"}
    assert by_code["011000"].probability == 5
    assert by_code["011000"].name == "宗谷地方"
    assert by_code["011000"].window == "1日間"
    assert by_code["130000"].probability == 55


def test_vpta51_without_areas_returns_empty() -> None:
    result = parse_vpta51(xml("vpta51.xml"), set())

    assert result.areas == []
    assert result.event_id == "TC2637"


def test_vpta51_ignores_areas_outside_the_configuration() -> None:
    result = parse_vpta51(xml("vpta51.xml"), {"130000"})

    assert all(area.code != "471000" for area in result.areas)
