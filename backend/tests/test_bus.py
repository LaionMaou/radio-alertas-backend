from __future__ import annotations

import asyncio

from radio_alerts_backend.bus import EventBus


async def test_publish_assigns_monotonic_sequences() -> None:
    bus = EventBus()

    first = await bus.publish("alert", {"n": 1})
    second = await bus.publish("alert", {"n": 2})

    assert (first.seq, second.seq) == (1, 2)
    assert bus.seq == 2


async def test_publish_accepts_explicit_sequence() -> None:
    bus = EventBus()

    message = await bus.publish("alert", {"n": 1}, seq=10)
    following = await bus.publish("alert", {"n": 2})

    assert message.seq == 10
    assert following.seq == 11


async def test_subscribe_replays_messages_after_last_event_id() -> None:
    bus = EventBus()
    first = await bus.publish("alert", {"n": 1})
    second = await bus.publish("alert", {"n": 2})
    third = await bus.publish("alert", {"n": 3})

    async with bus.subscribe(last_event_id=first.seq) as subscription:
        assert [message.seq for message in subscription.backlog] == [
            second.seq,
            third.seq,
        ]
        assert bus.subscriber_count == 1

    assert bus.subscriber_count == 0


async def test_subscription_yields_backlog_then_live_messages() -> None:
    bus = EventBus()
    first = await bus.publish("alert", {"n": 1})
    second = await bus.publish("alert", {"n": 2})

    async with bus.subscribe(last_event_id=first.seq) as subscription:
        stream = subscription.messages()
        assert (await anext(stream)).seq == second.seq
        third = await bus.publish("alert", {"n": 3})
        assert (await asyncio.wait_for(anext(stream), 1)).seq == third.seq


async def test_full_subscriber_queue_drops_oldest_message() -> None:
    bus = EventBus(queue_size=2)

    async with bus.subscribe() as subscription:
        for index in range(3):
            await bus.publish("alert", {"n": index})

        assert subscription.queue.qsize() == 2
        first = subscription.queue.get_nowait()
        second = subscription.queue.get_nowait()

    assert [first.data["n"], second.data["n"]] == [1, 2]


async def test_ring_buffer_is_bounded() -> None:
    bus = EventBus(ring_size=2)
    await bus.publish("alert", {"n": 1})
    await bus.publish("alert", {"n": 2})
    await bus.publish("alert", {"n": 3})

    async with bus.subscribe(last_event_id=0) as subscription:
        assert [message.data["n"] for message in subscription.backlog] == [2, 3]
