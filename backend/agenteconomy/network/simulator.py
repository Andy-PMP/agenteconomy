"""Discrete-event simulation: integer milliseconds of simulated time, no wall clock, no threads.

Events at the same time run in the order they were scheduled, so a run depends only on its seed.
"""

import heapq
import random
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(order=True)
class Event:
    time: int
    sequence: int
    action: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)

    def cancel(self) -> None:
        self.cancelled = True


class Simulator:
    def __init__(self, seed: int | str, start_time: int = 0):
        self.seed = seed
        self.now = start_time
        self._queue: list[Event] = []
        self._next_sequence = 0
        self._streams: dict[str, random.Random] = {}

    def rng(self, purpose: str) -> random.Random:
        """A separate random stream per purpose, so adding a miner doesn't shift every latency draw."""
        if purpose not in self._streams:
            self._streams[purpose] = random.Random(f"{self.seed}:{purpose}")
        return self._streams[purpose]

    def schedule(self, delay_ms: int, action: Callable[[], None]) -> Event:
        if type(delay_ms) is not int or delay_ms < 0:
            raise ValueError(f"delay must be a non-negative integer of milliseconds, got {delay_ms!r}")
        event = Event(self.now + delay_ms, self._next_sequence, action)
        self._next_sequence += 1
        heapq.heappush(self._queue, event)
        return event

    def _run_next(self) -> None:
        event = heapq.heappop(self._queue)
        if not event.cancelled:
            self.now = event.time
            event.action()

    def run_until(self, time: int) -> None:
        """Run everything due by `time`, then leave the clock at `time`."""
        while self._queue and self._queue[0].time <= time:
            self._run_next()
        self.now = max(self.now, time)

    def run_until_idle(self, max_events: int = 1_000_000) -> None:
        """Run until nothing is scheduled. Raises if it doesn't stop, so a message loop fails instead of hanging."""
        for _ in range(max_events):
            if not self._queue:
                return
            self._run_next()
        raise RuntimeError(f"still busy after {max_events} events")
