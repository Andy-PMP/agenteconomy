"""A miner that keeps racing on a node's tip. Hashing is real; simulated time follows the number of attempts.

P3's agents use Node.mine() instead, with a fixed budget per action.
"""

from dataclasses import replace

from ..chain.block import Block, BlockHeader, mine
from .node import Node
from .simulator import Event, Simulator

_ATTEMPTS_PER_SEARCH = 1 << 20


class MiningProcess:
    def __init__(self, sim: Simulator, node: Node, miner: str, hashes_per_second: int):
        if hashes_per_second <= 0:
            raise ValueError("hashes_per_second must be positive")
        self._sim = sim
        self._node = node
        self._miner = miner
        self._rate = hashes_per_second
        self._pending: Event | None = None
        self._running = False
        node.subscribe_tip(self._on_tip_changed)

    def start(self) -> None:
        self._running = True
        self._search()

    def stop(self) -> None:
        self._running = False
        self._cancel()

    def _cancel(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def _on_tip_changed(self) -> None:
        if self._running:
            self._search()  # whatever was being mined no longer extends the best chain

    def _delay_for(self, attempts: int) -> int:
        return max(1, attempts * 1000 // self._rate)

    def _search(self) -> None:
        self._cancel()
        self._continue(self._node.build_template(self._miner), 0)

    def _continue(self, template: Block, nonce: int) -> None:
        header = mine(replace(template.header, nonce=nonce), max_attempts=_ATTEMPTS_PER_SEARCH)
        if header is None:
            delay = self._delay_for(_ATTEMPTS_PER_SEARCH)
            self._pending = self._sim.schedule(delay, lambda: self._continue(template, nonce + _ATTEMPTS_PER_SEARCH))
        else:
            delay = self._delay_for(header.nonce - nonce + 1)
            self._pending = self._sim.schedule(delay, lambda: self._found(template, header))

    def _found(self, template: Block, header: BlockHeader) -> None:
        self._pending = None
        self._node.submit_block(Block(header, template.txs))
