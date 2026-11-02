"""Whole simulated days: miners really racing over a slow network, so nothing here is scripted."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from network_helpers import assert_settled_correctly
from scenarios import KEYS, PARAMS, run_busy_day

from agenteconomy.chain.validation import validate_chain
from agenteconomy.network.settle import Cluster

SCENARIOS = Path(__file__).with_name("scenarios.py")


def test_forks_really_occur_with_slow_links():
    cluster, result = run_busy_day("forks", partition=False)
    reorgs = cluster.log.of_kind("reorg")
    # This test means nothing if no forks happened, so it fails unless they did.
    assert reorgs, "no forks happened: raise the latency or the hash rate"
    assert result.orphaned
    assert result.height > 10
    assert_settled_correctly(cluster, result)


def test_forks_are_rare_on_a_fast_network_and_common_on_a_slow_one():
    def orphan_share(max_latency_ms):
        counts = []
        for seed in range(3):
            _, result = run_busy_day(f"rate-{seed}", max_latency_ms=max_latency_ms, partition=False)
            counts.append((len(result.orphaned), result.height))
        return sum(orphaned for orphaned, _ in counts) / sum(height for _, height in counts)

    assert orphan_share(20) < orphan_share(2000)


def test_real_mining_is_paced_by_hash_rate():
    def height_at(rate):
        cluster = Cluster(PARAMS, seed=1)
        cluster.add_miner(cluster.nodes[0], KEYS[0].public_hex, rate)
        cluster.start_mining()
        cluster.run_for(60_000)
        height = cluster.nodes[0].height
        cluster.miners[0].stop()
        return height

    slow, fast = height_at(64), height_at(1024)
    assert 5 < slow < fast
    assert 100 < fast < 400  # about one block every 250ms at this rate and difficulty


def test_found_block_is_valid_and_timed_by_attempts():
    cluster = Cluster(PARAMS, seed=1)
    node = cluster.nodes[0]
    cluster.add_miner(node, KEYS[0].public_hex, 256).start()
    cluster.run_for(10_000)
    cluster.miners[0].stop()
    blocks = node.main_chain()
    assert len(blocks) > 1
    validate_chain(blocks, PARAMS)
    # The search starts when the tip changes, so a block's arrival reflects its nonce at this rate.
    first = next(entry for entry in cluster.log.of_kind("block_accepted"))
    assert first["time"] == max(1, (blocks[1].header.nonce + 1) * 1000 // 256)


def test_honest_miners_never_produce_a_block_another_node_rejects():
    cluster, _ = run_busy_day("stale", partition=False)
    assert cluster.log.of_kind("rejected") == []


def test_partition_day_causes_a_deep_reorg_and_settles():
    cluster, result = run_busy_day("partition")
    deepest = max(entry["depth"] for entry in cluster.log.of_kind("reorg"))
    assert deepest >= 3
    assert_settled_correctly(cluster, result)


def test_mining_keeps_going_through_a_partition_on_both_sides():
    cluster = Cluster(PARAMS, seed=4)
    a, b, _ = cluster.nodes
    cluster.add_miner(a, KEYS[0].public_hex, 256)
    cluster.add_miner(b, KEYS[1].public_hex, 256)
    cluster.network.partition([a.id], [b.id])
    cluster.start_mining()
    cluster.run_for(10_000)
    assert a.height > 2 and b.height > 2
    assert a.tip != b.tip
    assert_settled_correctly(cluster, cluster.settle())


def test_same_seed_gives_an_identical_run():
    first, first_result = run_busy_day("repeat")
    second, second_result = run_busy_day("repeat")
    assert first.log.digest() == second.log.digest()
    assert first_result.tip == second_result.tip
    assert first.log.entries == second.log.entries


def test_different_seeds_give_different_runs():
    first, _ = run_busy_day("one", duration_ms=5000)
    second, _ = run_busy_day("two", duration_ms=5000)
    assert first.log.digest() != second.log.digest()


def test_identical_across_processes_with_different_hash_seeds():
    """Python randomizes string and bytes hashing per process, so set order is a classic way to lose determinism."""
    outputs = set()
    for hash_seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        run = subprocess.run([sys.executable, str(SCENARIOS), "cross-process"], capture_output=True, text=True,
                             env=env, check=True)
        outputs.add(run.stdout)
    assert len(outputs) == 1, outputs


@pytest.mark.parametrize("seed", range(12))
def test_rules_hold_on_random_days(seed):
    cluster, result = run_busy_day(
        f"random-{seed}",
        duration_ms=15_000,
        max_latency_ms=[100, 800, 3000][seed % 3],
        drop_rate=[0.0, 0.1, 0.3][(seed // 3) % 3],
        partition=seed % 2 == 0,
    )
    assert_settled_correctly(cluster, result)
    assert validate_chain(result.blocks, PARAMS) == result.state
    # No honest node ever accused another: all the traffic here is honest, if badly timed.
    for node in cluster.nodes:
        assert node.misbehavior == {}
        assert node.banned == set()
    # A pending transaction hasn't also been mined.
    mined = {tx.id for block in result.blocks for tx in block.txs}
    assert not mined & {tx.id for tx in result.pending}
    assert result.height > 2
