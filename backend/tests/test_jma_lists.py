from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from radio_alerts_backend.jma.quake_list import match_quake, parse_quake_list
from radio_alerts_backend.jma.tsunami_list import match_tsunami, parse_tsunami_list
from radio_alerts_backend.normalize import JST

FIXTURES = Path(__file__).parent / "fixtures" / "jma"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def jst(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=JST).astimezone(UTC)


def test_quake_list_is_normalized() -> None:
    entries = parse_quake_list(load("quake_list.json"))

    assert len(entries) == 3
    latest = entries[0]
    assert latest.eid == "20261025191443"
    assert latest.name == "茨城県北部"
    assert (latest.lat, latest.lon) == (36.8, 140.4)
    assert latest.magnitude == 3.8
    assert latest.maxi == 10
    assert entries[1].maxi == 50
    assert entries[1].detail == "20261025130000_1.json"


def test_quake_matches_by_name_and_time() -> None:
    entries = parse_quake_list(load("quake_list.json"))
    origin = jst(2026, 10, 25, 12, 59)

    matched = match_quake(
        entries,
        origin_at=origin,
        name="宮城県沖",
        lat=38.1,
        lon=142.0,
    )

    assert matched is not None
    assert matched.eid == "20261025125900"


def test_quake_matches_by_coordinates_when_name_differs() -> None:
    entries = parse_quake_list(load("quake_list.json"))
    origin = jst(2026, 10, 25, 12, 59)

    matched = match_quake(
        entries,
        origin_at=origin,
        name="別の表記",
        lat=38.2,
        lon=142.1,
    )

    assert matched is not None
    assert matched.eid == "20261025125900"


def test_quake_outside_time_window_is_not_matched() -> None:
    entries = parse_quake_list(load("quake_list.json"))

    matched = match_quake(
        entries,
        origin_at=jst(2026, 10, 25, 0, 59),
        name="宮城県沖",
        lat=38.1,
        lon=142.0,
    )

    assert matched is None


def test_quake_prefers_name_over_distance() -> None:
    origin = jst(2026, 10, 25, 12, 59)
    entries = parse_quake_list(
        [
            {
                "eid": "near",
                "at": "2026-10-25T12:59:10+09:00",
                "anm": "別の表記",
                "cod": "+38.1+142.0-50000/",
            },
            {
                "eid": "named",
                "at": "2026-10-25T12:59:30+09:00",
                "anm": "宮城県沖",
                "cod": "+39.0+143.0-50000/",
            },
        ]
    )

    matched = match_quake(
        entries, origin_at=origin, name="宮城県沖", lat=38.1, lon=142.0
    )

    assert matched is not None
    assert matched.eid == "named"


def test_quake_without_identity_is_not_guessed_when_ambiguous() -> None:
    origin = jst(2026, 10, 25, 12, 59)
    entries = parse_quake_list(
        [
            {"eid": "a", "at": "2026-10-25T12:59:10+09:00"},
            {"eid": "b", "at": "2026-10-25T12:59:20+09:00"},
        ]
    )

    assert match_quake(entries, origin_at=origin, name=None, lat=None, lon=None) is None


def test_tsunami_list_is_normalized() -> None:
    entries = parse_tsunami_list(load("tsunami_list.json"))

    assert len(entries) == 2
    assert entries[1].eid == "20261025125900"
    assert entries[1].title == "津波警報"
    assert entries[1].report_at == jst(2026, 10, 25, 13, 10)


def test_tsunami_matches_report_time() -> None:
    entries = parse_tsunami_list(load("tsunami_list.json"))

    matched = match_tsunami(entries, issued_at=jst(2026, 10, 25, 13, 10))

    assert matched is not None
    assert matched.eid == "20261025125900"


def test_tsunami_falls_back_to_origin_time() -> None:
    entries = parse_tsunami_list(load("tsunami_list.json"))

    matched = match_tsunami(entries, issued_at=jst(2026, 10, 10, 2, 56))

    assert matched is not None
    assert matched.eid == "20261010025608"


def test_tsunami_outside_window_is_not_matched() -> None:
    entries = parse_tsunami_list(load("tsunami_list.json"))

    assert match_tsunami(entries, issued_at=jst(2026, 9, 1, 0, 0)) is None


def test_non_tsunami_titles_are_ignored() -> None:
    entries = parse_tsunami_list(
        [{"eid": "x", "ttl": "震源・震度情報", "at": "2026-10-25T12:59:00+09:00"}]
    )
    assert entries == []
