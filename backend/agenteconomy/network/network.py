"""Simulated links between nodes: seeded latency, optional drops, partitions, and a shared event log."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..chain.canonical import canonical_json
from .simulator import Simulator


class EventLog:
    """What happened, in order. P6 builds the export from this, and its digest proves two runs were identical."""

    def __init__(self, sim: Simulator):
        self._sim = sim
        self.entries: list[dict[str, Any]] = []

    def record(self, node: str, kind: str, **fields: Any) -> None:
        self.entries.append({"time": self._sim.now, "node": node, "kind": kind, **fields})

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if entry["kind"] == kind]

    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.entries)).hexdigest()


@dataclass
class _Endpoint:
    handler: Callable[[str, bytes], None]
    on_reconnect: Callable[[], None] | None


class Network:
    def __init__(self, sim: Simulator, *, min_latency_ms: int = 20, max_latency_ms: int = 200, drop_rate: float = 0.0):
        if not 0 <= min_latency_ms <= max_latency_ms:
            raise ValueError("latency range must satisfy 0 <= min <= max")
        if not 0 <= drop_rate <= 1:
            raise ValueError("drop_rate must be between 0 and 1")
        self._sim = sim
        self.min_latency_ms = min_latency_ms
        self.max_latency_ms = max_latency_ms
        self.drop_rate = drop_rate
        self._endpoints: dict[str, _Endpoint] = {}
        self._groups: dict[str, int] = {}
        self._offline: set[str] = set()
        self.delivered = 0
        self.dropped = 0

    def register(self, endpoint_id: str, handler: Callable[[str, bytes], None],
                 on_reconnect: Callable[[], None] | None = None) -> None:
        self._endpoints[endpoint_id] = _Endpoint(handler, on_reconnect)

    def peers_of(self, endpoint_id: str) -> list[str]:
        return [other for other in self._endpoints if other != endpoint_id]

    def _connected(self, source: str, destination: str) -> bool:
        if source in self._offline or destination in self._offline:
            return False
        return self._groups.get(source, 0) == self._groups.get(destination, 0)

    def send(self, source: str, destination: str, data: bytes) -> None:
        if destination not in self._endpoints or not self._connected(source, destination):
            self.dropped += 1
            return
        if self.drop_rate and self._sim.rng("drops").random() < self.drop_rate:
            self.dropped += 1
            return
        latency = self._sim.rng("latency").randint(self.min_latency_ms, self.max_latency_ms)
        self._sim.schedule(latency, lambda: self._deliver(source, destination, data))

    def _deliver(self, source: str, destination: str, data: bytes) -> None:
        if not self._connected(source, destination):  # the link went down while this was in flight
            self.dropped += 1
            return
        self.delivered += 1
        self._endpoints[destination].handler(source, data)

    def partition(self, *groups: list[str]) -> None:
        """Split the network: endpoints can only reach others in their group. Anything unlisted is in group 0."""
        self._groups = {endpoint: index + 1 for index, group in enumerate(groups) for endpoint in group}

    def set_offline(self, endpoint_id: str, offline: bool = True) -> None:
        if offline:
            self._offline.add(endpoint_id)
        elif endpoint_id in self._offline:
            self._offline.discard(endpoint_id)
            self._reconnected(endpoint_id)

    def heal(self) -> None:
        """Restore every link. Each endpoint gets the chance to announce itself."""
        self._groups = {}
        self._offline = set()
        for endpoint_id in self._endpoints:
            self._reconnected(endpoint_id)

    def _reconnected(self, endpoint_id: str) -> None:
        on_reconnect = self._endpoints[endpoint_id].on_reconnect
        if on_reconnect is not None:
            on_reconnect()
