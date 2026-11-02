"""The end-of-day step: stop, reconnect, and get every node onto one tip, or fail without publishing anything."""

from collections.abc import Sequence
from dataclasses import dataclass

from ..chain.block import Block
from ..chain.errors import ChainError
from ..chain.params import ChainParams
from ..chain.state import State
from ..chain.transaction import Transaction
from ..chain.validation import validate_chain
from .miner import MiningProcess
from .network import EventLog, Network
from .node import Node
from .simulator import Simulator

_ROUND_MS = 10_000  # long enough for a node's sync retry to come due between rounds


class SettleError(Exception):
    """The nodes couldn't agree. Nothing is checkpointed, so the last good day stands."""


@dataclass(frozen=True)
class SettleResult:
    tip: str
    height: int
    state: State
    blocks: list[Block]  # the settled chain from genesis
    orphaned: list[str]  # valid blocks some node held that didn't make the chain
    reorgs: int
    rounds: int
    pending: list[Transaction]  # still waiting, for the next day to carry or drop


def settle(sim: Simulator, network: Network, log: EventLog, nodes: Sequence[Node], miners: Sequence[MiningProcess] = (),
           *, max_rounds: int = 10) -> SettleResult:
    for miner in miners:
        miner.stop()
    for node in nodes:
        node.close_day()
    network.heal()
    sim.run_until_idle()

    rounds = _agree_on_tip(sim, nodes, max_rounds)
    if rounds is None:
        tips = sorted({node.tip for node in nodes})
        log.record("settle", "settle_failed", reason="no agreement", tips=tips)
        raise SettleError(f"no agreement after {max_rounds} rounds: {len(tips)} different tips")

    state = _check_replays_agree(nodes, log)
    result = SettleResult(
        tip=nodes[0].tip,
        height=nodes[0].height,
        state=state,
        blocks=nodes[0].main_chain(),
        orphaned=_orphaned(nodes),
        reorgs=len(log.of_kind("reorg")),
        rounds=rounds,
        pending=_pending(nodes),
    )
    for node in nodes:
        node.finalize()
    log.record("settle", "settled", tip=result.tip, height=result.height, rounds=rounds, orphaned=result.orphaned)
    return result


def _agree_on_tip(sim: Simulator, nodes: Sequence[Node], max_rounds: int) -> int | None:
    for round_number in range(1, max_rounds + 1):
        for node in nodes:
            node.announce()
        sim.run_until_idle()
        for node in nodes:
            node.resolve_tie()
        sim.run_until_idle()

        # A node can agree by coincidence while still missing a tied block someone else holds, but everyone being
        # on one tip is what settling needs: the rest is a side branch that gets dropped.
        if len({node.tip for node in nodes}) == 1:
            return round_number
        sim.run_until(sim.now + _ROUND_MS)
    return None


def _check_replays_agree(nodes: Sequence[Node], log: EventLog) -> State:
    """Agreeing on a hash should mean agreeing on everything. Prove it from the blocks, node by node."""
    states = []
    for node in nodes:
        try:
            state = validate_chain(node.main_chain(), node.params)
        except ChainError as e:
            log.record(node.id, "settle_failed", reason="replay failed", code=str(e.code))
            raise SettleError(f"{node.id} can't replay its own chain: {e}") from e
        if state != node.state:
            log.record(node.id, "settle_failed", reason="state differs from replay")
            raise SettleError(f"{node.id}'s state doesn't match a replay of its chain")
        states.append(state)
    if any(state != states[0] for state in states):
        log.record("settle", "settle_failed", reason="nodes replayed to different states")
        raise SettleError("nodes replayed the same tip to different states")
    return states[0]


def _orphaned(nodes: Sequence[Node]) -> list[str]:
    main = {block.header.hash_hex for block in nodes[0].main_chain()}
    return sorted({block.header.hash_hex for node in nodes for block in node.side_blocks()} - main)


def _pending(nodes: Sequence[Node]) -> list[Transaction]:
    by_id: dict[bytes, Transaction] = {}
    for node in nodes:
        for tx in node.mempool.waiting():
            by_id.setdefault(tx.id, tx)
    return [by_id[tx_id] for tx_id in sorted(by_id)]


class Cluster:
    """A network of independent nodes on one simulated clock: 3 by default, as the spec starts with."""

    def __init__(self, params: ChainParams, seed: int | str, *, node_count: int = 3,
                 chain: Sequence[Block] | None = None, min_latency_ms: int = 20, max_latency_ms: int = 200,
                 drop_rate: float = 0.0, **node_options):
        start = chain[-1].header.timestamp if chain else params.genesis_timestamp
        self.params = params
        self.sim = Simulator(seed, start_time=start)
        self.log = EventLog(self.sim)
        self.network = Network(self.sim, min_latency_ms=min_latency_ms, max_latency_ms=max_latency_ms,
                               drop_rate=drop_rate)
        self.nodes = [Node(f"node-{i}", params, self.sim, self.network, self.log, chain=chain, **node_options)
                      for i in range(node_count)]
        self.miners: list[MiningProcess] = []

    def add_miner(self, node: Node, address: str, hashes_per_second: int) -> MiningProcess:
        miner = MiningProcess(self.sim, node, address, hashes_per_second)
        self.miners.append(miner)
        return miner

    def start_mining(self) -> None:
        for miner in self.miners:
            miner.start()

    def run_for(self, duration_ms: int) -> None:
        self.sim.run_until(self.sim.now + duration_ms)

    def settle(self, *, nodes: Sequence[Node] | None = None, max_rounds: int = 10) -> SettleResult:
        return settle(self.sim, self.network, self.log, nodes or self.nodes, self.miners, max_rounds=max_rounds)
