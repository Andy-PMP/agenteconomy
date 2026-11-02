"""Behaviors later checks would otherwise hide. Each of these was found by disabling a guard and seeing no test fail."""

from dataclasses import replace

import pytest
from network_helpers import CAROL, DAVE, PARAMS, deliver, next_block
from scenarios import KEYS
from scenarios import PARAMS as MINING_PARAMS

from agenteconomy.chain.block import Block, meets_target
from agenteconomy.chain.validation import validate_chain
from agenteconomy.network import miner as miner_module
from agenteconomy.network import settle as settle_module
from agenteconomy.network.miner import MiningProcess
from agenteconomy.network.settle import Cluster, SettleError


@pytest.fixture
def cluster():
    return Cluster(PARAMS, seed=1)


def orphan_with_unknown_parent(node) -> Block:
    parent = replace(node.tip_block.header, height=50, prev_hash="ab" * 32)
    return next_block(parent)


def without_proof_of_work(block: Block) -> Block:
    nonce = block.header.nonce
    while True:
        nonce += 1
        header = replace(block.header, nonce=nonce)
        if not meets_target(header.hash, header.difficulty_bits):
            return Block(header, block.txs)


def test_unmined_block_cannot_fill_the_orphan_pool(cluster):
    # Proof of work is what makes a flood of parentless blocks expensive.
    node = cluster.nodes[0]
    deliver(node, without_proof_of_work(orphan_with_unknown_parent(node)))
    assert node._orphans == {}


def test_wrong_difficulty_cannot_fill_the_orphan_pool(cluster):
    node = cluster.nodes[0]
    block = orphan_with_unknown_parent(node)
    deliver(node, Block(replace(block.header, difficulty_bits=0), block.txs))
    assert node._orphans == {}


def test_block_with_mismatched_transactions_cannot_fill_the_orphan_pool(cluster):
    node = cluster.nodes[0]
    block = orphan_with_unknown_parent(node)
    deliver(node, Block(block.header, (block.txs[0], block.txs[0])))
    assert node._orphans == {}


def test_failed_cheap_checks_are_not_remembered_as_invalid(cluster):
    node = cluster.nodes[0]
    deliver(node, without_proof_of_work(next_block(node.tip_block.header)))
    assert node._invalid == set()


def test_own_orphan_does_not_try_to_sync_from_nobody(cluster):
    node = cluster.nodes[0]
    node.submit_block(orphan_with_unknown_parent(node))
    assert len(node._orphans) == 1
    assert cluster.network.dropped == 0


def test_locator_lists_recent_blocks_then_spaces_them_out(cluster):
    node = cluster.nodes[0]
    parent = node.tip_block.header
    for _ in range(100):
        block = next_block(parent)
        deliver(node, block)
        parent = block.header

    heights = [node._entries[block_hash].height for block_hash in node._locator()]
    assert heights[:10] == list(range(100, 90, -1))  # small forks find a recent match
    gaps = [earlier - later for earlier, later in zip(heights[9:], heights[10:-1], strict=False)]
    assert gaps == sorted(gaps) and gaps[-1] > gaps[0]  # long ones still find one, without listing everything
    assert heights[-1] == 0


def test_resolving_when_already_on_the_lowest_hash_changes_nothing(cluster):
    node = cluster.nodes[0]
    genesis = node.tip_block.header
    low, high = sorted((next_block(genesis, miner=CAROL), next_block(genesis, miner=DAVE)),
                       key=lambda block: block.header.hash_hex)
    deliver(node, low)
    deliver(node, high)
    changes = len(cluster.log.of_kind("tip_changed"))
    node.resolve_tie()
    assert node.tip == low.header.hash_hex
    assert len(cluster.log.of_kind("tip_changed")) == changes


def test_failure_to_agree_is_reported_as_that():
    cluster = Cluster(PARAMS, seed=1, drop_rate=1.0)
    a, b, _ = cluster.nodes
    a.submit_block(next_block(a.tip_block.header, miner=CAROL))
    b.submit_block(next_block(b.tip_block.header, miner=DAVE))
    with pytest.raises(SettleError):
        cluster.settle(max_rounds=2)
    assert cluster.log.of_kind("settle_failed")[-1]["reason"] == "no agreement"


def test_settle_refuses_nodes_whose_replays_differ(cluster, monkeypatch):
    # Should be impossible with one tip. It's checked because publishing a wrong day can't be undone.
    a, b, _ = cluster.nodes
    a.submit_block(next_block(a.tip_block.header))
    cluster.sim.run_until_idle()
    b.state.balances[CAROL.public_hex] += 1
    states = iter([a.state, b.state, cluster.nodes[2].state])
    monkeypatch.setattr(settle_module, "validate_chain", lambda blocks, params: next(states))
    with pytest.raises(SettleError):
        cluster.settle()
    assert cluster.log.of_kind("settle_failed")[-1]["reason"] == "nodes replayed to different states"
    assert a.checkpoint_height == 0


@pytest.mark.parametrize("rate", [0, -1])
def test_miner_refuses_a_rate_that_would_break_its_timing(cluster, rate):
    with pytest.raises(ValueError):
        MiningProcess(cluster.sim, cluster.nodes[0], CAROL.public_hex, rate)


def test_long_search_continues_across_chunks_and_takes_the_right_time(monkeypatch):
    monkeypatch.setattr(miner_module, "_ATTEMPTS_PER_SEARCH", 8)
    cluster = Cluster(MINING_PARAMS, seed=1)
    node = cluster.nodes[0]
    cluster.add_miner(node, KEYS[0].public_hex, 1000).start()
    cluster.run_for(30_000)
    cluster.miners[0].stop()

    blocks = node.main_chain()
    assert len(blocks) > 2
    validate_chain(blocks, MINING_PARAMS)
    # Far more attempts than one chunk were needed, so this proves continuation, not a lucky first chunk.
    assert blocks[1].header.nonce >= 8
    first = cluster.log.of_kind("block_accepted")[0]
    chunks = blocks[1].header.nonce // 8
    remaining = blocks[1].header.nonce - chunks * 8 + 1
    assert first["time"] == chunks * 8 + remaining
