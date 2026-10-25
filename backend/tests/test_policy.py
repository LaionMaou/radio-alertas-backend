from __future__ import annotations

from datetime import UTC, datetime

from radio_alerts_backend.config import Settings
from radio_alerts_backend.models import Event, EventKind, EventStatus
from radio_alerts_backend.policy import Policy

ISSUED = datetime(2026, 10, 25, 4, 5, tzinfo=UTC)


def settings(**kwargs) -> Settings:
    values = {"internal_token": "test-token"}
    values.update(kwargs)
    return Settings(**values)


def quake(**kwargs) -> Event:
    values = {
        "epicenter": {"name": "宮城県沖", "magnitude": 7.2},
        "max_intensity": {"value": 50, "label": "5強"},
        "tsunami": {"domestic": "None"},
    }
    values.update(kwargs)
    return Event(event_key="quake:1", kind=EventKind.EARTHQUAKE, issued_at=ISSUED, **values)


def tsunami(grade: str, **kwargs) -> Event:
    values = {
        "tsunami": {
            "grade": grade,
            "immediate": False,
            "areas": [{"name": "宮城県", "grade": grade}],
        },
    }
    values.update(kwargs)
    return Event(event_key="tsunami:1", kind=EventKind.TSUNAMI, issued_at=ISSUED, **values)


def typhoon_event(probability: int, code: str = "130000", **kwargs) -> Event:
    values = {
        "typhoon": {
            "number": "2631",
            "name": "IN-FA",
            "class": "台風(STS)",
            "areas": [{"code": code, "prefecture": "東京都", "probability": probability}],
        },
    }
    values.update(kwargs)
    return Event(event_key="typhoon:TC2637", kind=EventKind.TYPHOON, issued_at=ISSUED, **values)


def test_initial_quake_announces_with_rank() -> None:
    decision = Policy(settings()).decide(quake())

    assert decision.announce is True
    assert decision.reason == "initial"
    assert decision.rank == 5014
    assert decision.text is not None
    assert decision.text.startswith("地震情報。")
    assert len(decision.dedupe_key) <= 120
    assert decision.dedupe_key.startswith("earthquake:")


def test_quake_below_both_thresholds_is_ignored() -> None:
    decision = Policy(settings()).decide(
        quake(
            epicenter={"name": "茨城県北部", "magnitude": 6.8},
            max_intensity={"value": 40, "label": "4"},
        )
    )

    assert decision.announce is False
    assert decision.reason == "below_threshold"


def test_big_magnitude_qualifies_without_intensity() -> None:
    decision = Policy(settings()).decide(
        quake(
            max_intensity={"value": None, "label": "不明"},
            epicenter={"name": "遠地", "magnitude": 7.5},
        )
    )

    assert decision.announce is True
    assert decision.rank == 15


def test_quake_without_usable_data_is_ignored() -> None:
    decision = Policy(settings()).decide(
        quake(
            epicenter={"name": "", "magnitude": None},
            max_intensity={"value": None, "label": "不明"},
        )
    )

    assert decision.announce is False
    assert decision.reason == "insufficient_data"


def test_eew_can_be_disabled() -> None:
    item = Event(
        event_key="eew:1",
        kind=EventKind.EEW,
        issued_at=ISSUED,
        epicenter={"name": "宮城県沖", "magnitude": 7.2},
        max_intensity={"value": 50, "label": "5強"},
    )

    decision = Policy(settings(eew_enabled=False)).decide(item)

    assert decision.announce is False
    assert decision.reason == "eew_disabled"


def test_tsunami_watch_announces() -> None:
    decision = Policy(settings()).decide(tsunami("Watch"))

    assert decision.announce is True
    assert decision.rank == 10
    assert decision.text is not None
    assert decision.text.startswith("津波注意報。")


def test_tsunami_below_minimum_grade_is_ignored() -> None:
    decision = Policy(settings()).decide(tsunami("NonEffective"))

    assert decision.announce is False
    assert decision.reason == "below_threshold"


def test_tsunami_immediate_ranks_higher() -> None:
    item = tsunami("Warning")
    item.tsunami["immediate"] = True

    assert Policy(settings()).decide(item).rank == 21


def test_cancelled_and_test_events_never_announce() -> None:
    policy = Policy(settings())

    cancelled = policy.decide(quake(status=EventStatus.CANCELLED))
    test = policy.decide(quake(status=EventStatus.TEST))
    stale = policy.decide(quake(status=EventStatus.STALE))

    assert (cancelled.announce, cancelled.reason) == (False, "cancelled")
    assert (test.announce, test.reason) == (False, "test")
    assert (stale.announce, stale.reason) == (False, "stale")


def test_same_rank_is_not_announced_again() -> None:
    item = quake(
        announcements=[
            {"version": 1, "rank": 5014, "alert_id": "a1", "status": "ok", "at": "x"}
        ]
    )

    decision = Policy(settings()).decide(item)

    assert decision.announce is False
    assert decision.reason == "no_escalation"


def test_higher_rank_is_announced_as_escalation() -> None:
    item = quake(
        max_intensity={"value": 55, "label": "6弱"},
        announcements=[
            {"version": 1, "rank": 4514, "alert_id": "a1", "status": "ok", "at": "x"}
        ],
    )

    decision = Policy(settings()).decide(item)

    assert decision.announce is True
    assert decision.reason == "escalation"
    assert decision.rank == 5514


def test_open_ended_scale_can_escalate_to_exact_scale() -> None:
    policy = settings()
    announced = [
        {"version": 1, "rank": 4514, "alert_id": "a1", "status": "ok", "at": "x"}
    ]
    first = quake(
        max_intensity={"value": 99, "label": "5弱以上"},
        announcements=announced,
    )
    second = quake(max_intensity={"value": 50, "label": "5強"}, announcements=announced)

    assert Policy(policy).decide(first).announce is False
    decision = Policy(policy).decide(second)
    assert decision.announce is True
    assert decision.rank == 5014


def test_typhoon_is_web_only_by_default() -> None:
    decision = Policy(settings()).decide(typhoon_event(90))

    assert decision.announce is False
    assert decision.reason == "typhoon_web_only"


def test_typhoon_below_probability_threshold_is_ignored() -> None:
    policy = Policy(settings(typhoon_ann_tts=True, typhoon_areas="130000"))

    decision = policy.decide(typhoon_event(20))

    assert decision.announce is False
    assert decision.reason == "below_threshold"


def test_typhoon_in_configured_area_announces() -> None:
    policy = Policy(settings(typhoon_ann_tts=True, typhoon_areas="130000"))

    decision = policy.decide(typhoon_event(55))

    assert decision.announce is True
    assert decision.rank == 22
    assert decision.text is not None
    assert "東京都では暴風に警戒してください。" in decision.text


def test_typhoon_outside_configured_areas_is_ignored() -> None:
    policy = Policy(settings(typhoon_ann_tts=True, typhoon_areas="130000"))

    decision = policy.decide(typhoon_event(90, code="471000"))

    assert decision.announce is False
    assert decision.reason == "typhoon_no_area"
