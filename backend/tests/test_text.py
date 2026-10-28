from __future__ import annotations

from datetime import UTC, datetime

from radio_alerts_backend.announce import MAX_CHARS, render
from radio_alerts_backend.models import Event, EventKind

ISSUED = datetime(2026, 10, 25, 4, 5, tzinfo=UTC)


def event(kind: EventKind, **kwargs) -> Event:
    return Event(event_key=f"{kind.value}:1", kind=kind, issued_at=ISSUED, **kwargs)


INTRO = "緊急地震速報です。強い揺れに警戒してください。"


def test_eew_text_repeats_the_intro_and_lists_the_data() -> None:
    item = event(
        EventKind.EEW,
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 50, "label": "5強"},
        eew={"areas": [{"name": "宮城県"}]},
    )

    assert render(item) == (
        f"{INTRO}{INTRO}"
        "震源は宮城県沖。マグニチュード7.2。最大震度5強。"
    )


def test_eew_open_ended_scale_never_renders_99() -> None:
    item = event(
        EventKind.EEW,
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 99, "label": "5弱以上"},
        eew={"areas": [{"name": "宮城県"}]},
    )

    text = render(item)
    assert "99" not in text
    assert "最大震度5弱以上と予想されます。" in text
    assert "大地震です" not in text, (
        "99 es una intensidad abierta (5弱以上), no un 震度7"
    )


def test_eew_six_strong_asks_to_stay_calm() -> None:
    item = event(
        EventKind.EEW,
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 60, "label": "6強"},
    )

    assert render(item) == (
        f"{INTRO}{INTRO}"
        "緊急地震速報です。落ち着いて行動してください。まもなく地震が来ます。"
        "震源は宮城県沖。マグニチュード7.2。最大震度6強。"
    )


def test_eew_seven_warns_of_a_major_earthquake() -> None:
    item = event(
        EventKind.EEW,
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 70, "label": "7"},
    )

    assert render(item) == (
        f"{INTRO}{INTRO}"
        "大地震です。大地震です。身の安全を確保してください。"
        "震源は宮城県沖。マグニチュード7.2。最大震度7。"
    )


def test_eew_without_intensity_keeps_only_the_intro_and_the_data() -> None:
    item = event(
        EventKind.EEW,
        epicenter={"name": "宮城県沖"},
    )

    assert render(item) == f"{INTRO}{INTRO}震源は宮城県沖。"


def test_quake_text_without_tsunami_risk() -> None:
    item = event(
        EventKind.EARTHQUAKE,
        origin_at=datetime(2026, 10, 25, 3, 59, tzinfo=UTC),
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 50, "label": "5強"},
        tsunami={"domestic": "None", "foreign": None},
    )

    assert render(item) == (
        "地震情報。12時59分頃、宮城県沖で地震がありました。"
        "最大震度5強。マグニチュード7.2。津波の心配はありません。"
    )


def test_quake_with_tsunami_advisory_does_not_deny_risk() -> None:
    item = event(
        EventKind.EARTHQUAKE,
        origin_at=datetime(2026, 10, 25, 3, 59, tzinfo=UTC),
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 50, "label": "5強"},
        tsunami={"domestic": "Watch"},
    )

    text = render(item)
    assert "津波の心配はありません" not in text
    assert text.endswith("津波注意報が発表されています。海から離れてください。")


def test_quake_with_unknown_tsunami_asks_to_check() -> None:
    item = event(
        EventKind.EARTHQUAKE,
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 50, "label": "5強"},
        tsunami={"domestic": "Unknown"},
    )

    text = render(item)
    assert "津波の心配はありません" not in text
    assert "津波情報を確認してください。" in text


def test_quake_without_tsunami_field_does_not_claim_absence() -> None:
    item = event(
        EventKind.EARTHQUAKE,
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 50, "label": "5強"},
    )

    assert "津波" not in render(item)


def test_tsunami_warning_text() -> None:
    item = event(
        EventKind.TSUNAMI,
        tsunami={
            "grade": "Warning",
            "areas": [
                {"name": "宮城県", "grade": "Warning"},
                {"name": "岩手県", "grade": "Warning"},
            ],
        },
    )

    assert render(item) == (
        "津波警報。宮城県、岩手県に津波警報が発表されました。海岸から離れてください。"
    )


def test_tsunami_major_warning_text_is_fixed_regardless_of_areas() -> None:
    item = event(
        EventKind.TSUNAMI,
        tsunami={
            "grade": "MajorWarning",
            "areas": [{"name": "宮城県", "grade": "MajorWarning"}],
        },
    )

    assert render(item) == (
        "大津波警報が発表されました。海岸付近の方は高台に避難してください。"
    )


def test_quake_with_major_tsunami_does_not_stay_silent() -> None:
    item = event(
        EventKind.EARTHQUAKE,
        origin_at=datetime(2026, 10, 25, 3, 59, tzinfo=UTC),
        max_intensity={"value": 60, "label": "6強"},
        tsunami={"domestic": "MajorWarning"},
    )

    assert render(item).endswith(
        "大津波警報が発表されています。直ちに高台へ避難してください。"
    )


def test_tsunami_advisory_text() -> None:
    item = event(
        EventKind.TSUNAMI,
        tsunami={"grade": "Watch", "areas": [{"name": "宮城県", "grade": "Watch"}]},
    )

    text = render(item)
    assert text.startswith("津波注意報。宮城県に津波注意報が発表されました。")
    assert text.endswith("海から離れてください。")


def test_typhoon_text() -> None:
    item = event(
        EventKind.TYPHOON,
        typhoon={
            "number": "2631",
            "name": "IN-FA",
            "location": "マーシャル諸島",
            "pressure_hpa": 992,
            "wind_mps": 30,
            "areas": [{"code": "130000", "prefecture": "東京都", "probability": 55}],
        },
    )

    assert render(item) == (
        "台風情報。台風31号IN-FAは、マーシャル諸島にあります。"
        "中心気圧992ヘクトパスカル、最大風速30メートル。"
        "東京都では暴風に警戒してください。"
    )


def test_typhoon_without_measurements_still_warns() -> None:
    item = event(
        EventKind.TYPHOON,
        typhoon={"number": "2631", "name": "IN-FA", "location": "海上"},
    )

    assert render(item) == (
        "台風情報。台風31号IN-FAは、海上にあります。暴風や高波に警戒してください。"
    )


def test_long_area_list_is_summarized() -> None:
    areas = [
        {"name": f"とても長い地域名その{i}", "grade": "Warning"} for i in range(10)
    ]
    item = event(EventKind.TSUNAMI, tsunami={"grade": "Warning", "areas": areas})

    text = render(item)
    assert len(text) <= MAX_CHARS
    assert "ほか7地域" in text
    assert text.endswith("海岸から離れてください。")


def test_long_names_do_not_cut_sentences() -> None:
    item = event(
        EventKind.EEW,
        epicenter={"name": "あ" * 200, "magnitude": 7.2},
        max_intensity={"value": 50, "label": "5強"},
    )

    text = render(item)
    assert len(text) <= MAX_CHARS
    assert text.endswith("最大震度5強。")
    assert "…" in text
