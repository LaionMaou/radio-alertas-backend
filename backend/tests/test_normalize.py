from __future__ import annotations

from datetime import UTC, datetime

from radio_alerts_backend.normalize import (
    clamp_text,
    dedupe_key,
    eew_event_key,
    format_jst_time,
    grade_rank,
    haversine_km,
    iso_z,
    jma_maxi_to_scale,
    parse_iso,
    parse_p2p_time,
    probability_band,
    quake_event_key,
    scale_code,
    scale_label,
    tsunami_event_key,
    typhoon_class_rank,
    typhoon_event_key,
)


def test_p2p_time_is_jst_and_converted_to_utc() -> None:
    assert parse_p2p_time("2026/10/25 13:05:18.123") == datetime(
        2026, 10, 25, 4, 5, 18, 123000, tzinfo=UTC
    )
    assert parse_p2p_time("2026/10/25 13:05:18") == datetime(
        2026, 10, 25, 4, 5, 18, tzinfo=UTC
    )
    assert parse_p2p_time("2026-10-25T04:05:18Z") == datetime(
        2026, 10, 25, 4, 5, 18, tzinfo=UTC
    )
    assert parse_p2p_time(None) is None
    assert parse_p2p_time("invalid") is None


def test_iso_helpers() -> None:
    value = datetime(2026, 10, 25, 4, 5, 18, 900000, tzinfo=UTC)
    assert iso_z(value) == "2026-10-25T04:05:18Z"
    assert parse_iso("2026-10-25T04:05:18Z") == datetime(
        2026, 10, 25, 4, 5, 18, tzinfo=UTC
    )
    assert parse_iso(None) is None


def test_jst_formatting_drops_zero_minutes() -> None:
    assert format_jst_time(datetime(2026, 10, 24, 18, 50, tzinfo=UTC)) == "3時50分"
    assert format_jst_time(datetime(2026, 10, 24, 18, 0, tzinfo=UTC)) == "3時"
    assert format_jst_time(None) is None


def test_scale_helpers_never_render_inexact_codes() -> None:
    assert scale_label(46) == "5弱以上"
    assert scale_label(99) == "5弱以上"
    assert scale_label(45) == "5弱"
    assert scale_label(70) == "7"
    assert scale_label(-1) == "不明"
    assert scale_label(None) == "不明"
    assert scale_code(46) == 45
    assert scale_code(99) == 45
    assert scale_code(70) == 70


def test_jma_maxi_mapping() -> None:
    assert jma_maxi_to_scale("5-") == 45
    assert jma_maxi_to_scale("5+") == 50
    assert jma_maxi_to_scale("6+") == 60
    assert jma_maxi_to_scale(99) is None


def test_grade_rank() -> None:
    assert grade_rank("None") == 0
    assert grade_rank("NonEffective") == 0
    assert grade_rank("Watch") == 1
    assert grade_rank("Warning") == 2
    assert grade_rank("MajorWarning") == 3
    assert grade_rank(None) == 0


def test_typhoon_class_rank() -> None:
    assert typhoon_class_rank("猛烈な台風") == 5
    assert typhoon_class_rank("非常に強い台風") == 4
    assert typhoon_class_rank("強い台風") == 3
    assert typhoon_class_rank("台風(STS)") == 2
    assert typhoon_class_rank("台風(TY)") == 3
    assert typhoon_class_rank("熱帯低気圧") == 0
    assert typhoon_class_rank(None) == 0


def test_probability_bands() -> None:
    assert probability_band(0) == 0
    assert probability_band(29) == 0
    assert probability_band(30) == 1
    assert probability_band(50) == 2
    assert probability_band(70) == 3


def test_haversine() -> None:
    distance = haversine_km(35.68, 139.76, 34.69, 135.50)
    assert distance is not None
    assert 390 <= distance <= 410
    assert haversine_km(None, 139.76, 34.69, 135.50) is None


def test_event_keys() -> None:
    at = datetime(2026, 10, 25, 4, 5, tzinfo=UTC)
    assert eew_event_key("20261025130518", at, "宮城県沖") == "eew:20261025130518"
    fallback = eew_event_key(None, at, "宮城県沖")
    assert fallback.startswith("eew:")
    assert fallback == eew_event_key(None, at, "宮城県沖")
    assert fallback != eew_event_key(None, at, "福島県沖")

    assert quake_event_key("20261025130518", "p2p") == "quake:20261025130518"
    assert quake_event_key(None, "p2p-1") == "quake:p2p-1"
    assert tsunami_event_key("TT1", at) == "tsunami:TT1"
    assert tsunami_event_key(None, at).startswith("tsunami:")
    assert typhoon_event_key("TC2637") == "typhoon:TC2637"


def test_dedupe_key_is_bounded() -> None:
    key = dedupe_key("earthquake", "quake:" + "x" * 200, 514)
    assert len(key) <= 120
    assert key.startswith("earthquake:")
    assert key.endswith(":514")


def test_clamp_text_collapses_whitespace_and_truncates() -> None:
    assert clamp_text("  hola \n mundo  ", 50) == "hola mundo"
    assert clamp_text("a" * 30, 10) == "a" * 9 + "…"
