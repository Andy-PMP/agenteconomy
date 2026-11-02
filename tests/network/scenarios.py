"""A busy simulated day: miners racing, transactions, a partition, packet loss and a misbehaving peer.

Also run as a script, so a test can compare results across separate processes: python scenarios.py <seed>
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from agenteconomy.chain.errors import ChainError
from agenteconomy.chain.params import ChainParams
from agenteconomy.chain.transaction import sign_transfer
from agenteconomy.chain.vectors import sample_keys
from agenteconomy.network.settle import Cluster, SettleResult

KEYS = [sample_keys(name) for name in ("alice", "bob", "carol", "dave")]

# 256 expected hashes per block. At 256 hashes/s a miner finds a block about once a second.
PARAMS = ChainParams(
    chain_id="scenario", difficulty_bits=8, block_reward=50, min_fee=1, max_block_txs=20,
    genesis_allocations=tuple((key.public_hex, 1000) for key in KEYS),
)


def run_busy_day(seed: int | str, *, duration_ms: int = 30_000, hashes_per_second: int = 256, max_latency_ms: int = 800,
                 drop_rate: float = 0.0, partition: bool = True, during=None) -> tuple[Cluster, SettleResult]:
    """`during(cluster)` can start extra activity, and returns a function that stops it before settling."""
    cluster = Cluster(PARAMS, seed, max_latency_ms=max_latency_ms, drop_rate=drop_rate)
    stop_extra = during(cluster) if during else None
    for index, node in enumerate(cluster.nodes):
        cluster.add_miner(node, KEYS[index].public_hex, hashes_per_second)
    cluster.start_mining()

    rng = cluster.sim.rng("traffic")
    next_send = {"event": None}

    def send_one() -> None:
        # Agents can attempt anything, so some of this is stale, gapped or unaffordable.
        node = rng.choice(cluster.nodes)
        sender, to = rng.sample(KEYS, 2)
        nonce = node.state.nonce(sender.public_hex) + rng.choice([0, 0, 0, 1, 2])
        tx = sign_transfer(sender, PARAMS.chain_id, to=to.public_hex, amount=rng.choice([1, 5, 20, 400]), nonce=nonce,
                           fee=rng.choice([1, 1, 2, 5]))
        try:
            node.submit_tx(tx)
        except ChainError:
            pass
        next_send["event"] = cluster.sim.schedule(rng.randint(1, 300), send_one)

    next_send["event"] = cluster.sim.schedule(1, send_one)

    if partition:
        cluster.run_for(duration_ms // 3)
        cluster.network.partition([cluster.nodes[0].id], [node.id for node in cluster.nodes[1:]])
        cluster.run_for(duration_ms // 3)
        cluster.network.heal()
        cluster.run_for(duration_ms - 2 * (duration_ms // 3))
    else:
        cluster.run_for(duration_ms)

    next_send["event"].cancel()
    if stop_extra:
        stop_extra()
    return cluster, cluster.settle()


if __name__ == "__main__":
    finished, settled = run_busy_day(sys.argv[1])
    print(finished.log.digest(), settled.tip, settled.height, len(finished.log.of_kind("reorg")))
