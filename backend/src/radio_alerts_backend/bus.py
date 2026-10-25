from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BusMessage:
    seq: int
    type: str
    data: dict[str, Any]


@dataclass
class Subscription:
    queue: asyncio.Queue[BusMessage]
    backlog: list[BusMessage]

    async def messages(self) -> AsyncIterator[BusMessage]:
        for message in self.backlog:
            yield message
        while True:
            yield await self.queue.get()


class EventBus:
    """Fan-out para SSE con replay corto y sin bloquear al publicador."""

    def __init__(self, ring_size: int = 200, queue_size: int = 100) -> None:
        self._seq = 0
        self._ring: deque[BusMessage] = deque(maxlen=ring_size)
        self._subscribers: set[asyncio.Queue[BusMessage]] = set()
        self._queue_size = queue_size
        self._lock = asyncio.Lock()

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(
        self,
        message_type: str,
        data: dict[str, Any],
        seq: int | None = None,
    ) -> BusMessage:
        async with self._lock:
            if seq is None or seq <= self._seq:
                self._seq += 1
            else:
                self._seq = seq
            message = BusMessage(seq=self._seq, type=message_type, data=data)
            self._ring.append(message)
            for queue in self._subscribers:
                self._offer(queue, message)
            return message

    @staticmethod
    def _offer(queue: asyncio.Queue[BusMessage], message: BusMessage) -> None:
        while True:
            try:
                queue.put_nowait(message)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

    @asynccontextmanager
    async def subscribe(
        self,
        last_event_id: int | None = None,
    ) -> AsyncIterator[Subscription]:
        queue: asyncio.Queue[BusMessage] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            backlog = (
                [message for message in self._ring if message.seq > last_event_id]
                if last_event_id is not None
                else []
            )
            self._subscribers.add(queue)
        try:
            yield Subscription(queue=queue, backlog=backlog)
        finally:
            self._subscribers.discard(queue)
