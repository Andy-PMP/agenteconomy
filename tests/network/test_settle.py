from dataclasses import replace

import pytest
from network_helpers import (
    BOB,
    CAROL,
    DAVE,
    PARAMS,
    BadPeer,
    assert_settled_correctly,
    build_on,
    deliver,
    next_block,
    pay,
    remined,
)

from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.validation import validate_chain
from agenteconomy.network.settle import Cluster, SettleError


@pytest.fixture
def cluster():
    return Cluster(PARAMS, seed=1)


def test_settling_an_agreed_chain_takes_one_round(cluster):
    build_on(cluster.nodes[0], 3)
    cluster.sim.run_until_idle()
    result = cluster.settle()
    assert result.rounds == 1
    assert result.height == 3
    assert_settled_correctly(cluster, result)


def test_result_describes_the_chain(cluster):
    a = cluster.nodes[0]
    tx = pay()
    a.submit_tx(tx)
    block = next_block(a.tip_block.header, [tx])
    a.submit_block(block)
    waiting = pay(nonce=1, amount=7)
    a.submit_tx(waiting)
    result = cluster.settle()

    assert result.tip == block.header.hash_hex
    assert [b.header.height for b in result.blocks] == [0, 1]
    assert validate_chain(result.blocks, PARAMS) == result.state
    assert [t.id for t in result.pending] == [waiting.id]
    assert result.orphaned == []
    assert result.reorgs == 0


def test_settle_closes_the_day(cluster):
    node = cluster.nodes[0]
    cluster.settle()
    with pytest.raises(ChainError) as e:
        node.submit_tx(pay())
    assert e.value.code == Code.DAY_CLOSED
    with pytest.raises(ChainError):
        node.mine(CAROL.public_hex, 10)
    node.open_day()
    node.submit_tx(pay())


def test_incoming_transactions_are_ignored_while_closed(cluster):
    a, b, _ = cluster.nodes
    tx = pay()
    a.submit_tx(tx)  # in flight when the day closes
    cluster.settle()
    assert tx not in b.mempool


def test_tie_resolves_to_the_lowest_hash_on_every_node(cluster):
    a, b, c = cluster.nodes
    cluster.network.partition([a.id], [b.id], [c.id])
    genesis = a.tip_block.header
    blocks = [next_block(genesis, miner=CAROL), next_block(genesis, miner=DAVE), next_block(genesis, miner=BOB)]
    for node, block in zip(cluster.nodes, blocks, strict=True):
        deliver(node, block)
    assert len({a.tip, b.tip, c.tip}) == 3

    result = cluster.settle()
    assert result.tip == min(block.header.hash_hex for block in blocks)
    assert_settled_correctly(cluster, result)
    assert len(result.orphaned) == 2


def test_two_ways_of_seeing_a_tie_reach_the_same_answer():
    def run(order):
        cluster = Cluster(PARAMS, seed=1)
        a, b, _ = cluster.nodes
        cluster.network.partition([a.id], [b.id])
        genesis = a.tip_block.header
        blocks = [next_block(genesis, miner=CAROL), next_block(genesis, miner=DAVE)]
        deliver(a, blocks[order[0]])
        deliver(b, blocks[order[1]])
        return cluster.settle().tip

    assert run((0, 1)) == run((1, 0))


def test_tied_block_only_one_node_saw_is_still_found(cluster):
    a, b, c = cluster.nodes
    cluster.network.partition([a.id], [b.id], [c.id])
    genesis = a.tip_block.header
    tied = sorted((next_block(genesis, miner=key) for key in (CAROL, DAVE, BOB)), key=lambda b: b.header.hash_hex)
    # c follows the highest hash but also privately holds the lowest, which it saw second.
    deliver(a, tied[1])
    deliver(b, tied[1])
    deliver(c, tied[2])
    deliver(c, tied[0], sender="private")
    assert c.tip == tied[2].header.hash_hex

    result = cluster.settle()
    assert_settled_correctly(cluster, result)
    assert result.tip == tied[0].header.hash_hex


def test_settling_reconnects_a_partition_and_most_work_wins(cluster):
    a, b, c = cluster.nodes
    cluster.network.partition([a.id], [b.id, c.id])
    build_on(a, 2, miner=CAROL)
    deep = build_on(b, 5, miner=DAVE)
    result = cluster.settle()
    assert result.tip == deep[-1].header.hash_hex
    assert_settled_correctly(cluster, result)
    assert len(result.orphaned) == 2


def test_settling_brings_back_an_offline_node(cluster):
    a, _, c = cluster.nodes
    cluster.network.set_offline(c.id)
    build_on(a, 3)
    result = cluster.settle()
    assert_settled_correctly(cluster, result)
    assert c.height == 3


def test_settling_delivers_what_is_still_in_flight(cluster):
    block = next_block(cluster.nodes[0].tip_block.header)
    cluster.nodes[0].submit_block(block)
    result = cluster.settle()
    assert result.height == 1
    assert_settled_correctly(cluster, result)


def test_settle_stops_miners(cluster):
    miner = cluster.add_miner(cluster.nodes[0], CAROL.public_hex, 1000)
    miner.start()
    cluster.run_for(1000)
    result = cluster.settle()
    height = result.height
    cluster.run_for(100_000)
    assert cluster.nodes[0].height == height


def test_tie_break_returns_the_losers_transactions(cluster):
    a, b, _ = cluster.nodes
    cluster.network.partition([a.id], [b.id])
    genesis = a.tip_block.header
    tx_a, tx_b = pay(to=CAROL), pay(BOB, DAVE)
    block_a = next_block(genesis, [tx_a], miner=CAROL)
    block_b = next_block(genesis, [tx_b], miner=DAVE)
    deliver(a, block_a)
    deliver(b, block_b)

    result = cluster.settle()
    loser_tx = tx_b if result.tip == block_a.header.hash_hex else tx_a
    assert [tx.id for tx in result.pending] == [loser_tx.id]


def test_settle_fails_cleanly_when_nodes_cannot_communicate():
    cluster = Cluster(PARAMS, seed=1, drop_rate=1.0)
    a, b, _ = cluster.nodes
    build_on(a, 1, miner=CAROL)
    build_on(b, 2, miner=DAVE)
    with pytest.raises(SettleError):
        cluster.settle(max_rounds=3)
    assert all(node.checkpoint_height == 0 for node in cluster.nodes)
    assert len(cluster.log.of_kind("settle_failed")) == 1
    assert cluster.log.of_kind("settled") == []


def test_settle_survives_heavy_message_loss():
    cluster = Cluster(PARAMS, seed=5, drop_rate=0.5)
    a, b, _ = cluster.nodes
    build_on(a, 3, miner=CAROL)
    build_on(b, 4, miner=DAVE)
    result = cluster.settle(max_rounds=50)
    assert_settled_correctly(cluster, result)
    assert result.height == 4
    assert result.rounds > 1  # guards the test: the loss must have actually mattered


def test_settle_refuses_a_node_whose_state_does_not_match_its_chain(cluster):
    a = cluster.nodes[0]
    build_on(a, 1)
    cluster.sim.run_until_idle()
    a.state.balances[CAROL.public_hex] += 1_000_000  # memory corruption, or a bug
    with pytest.raises(SettleError):
        cluster.settle()
    assert a.checkpoint_height == 0
    assert cluster.log.of_kind("settle_failed")[-1]["reason"] == "state differs from replay"


def test_settle_refuses_a_node_whose_chain_does_not_replay(cluster):
    # All three nodes hold the same bad block, so they agree on a tip: only the replay can catch it.
    build_on(cluster.nodes[0], 1)
    cluster.sim.run_until_idle()
    block = cluster.nodes[0].tip_block
    bad = remined(block, [replace(block.txs[0], body={**block.txs[0].body, "amount": 51})])
    for node in cluster.nodes:
        node._entries[node.tip].block = bad
    with pytest.raises(SettleError):
        cluster.settle()
    assert cluster.log.of_kind("settle_failed")[-1]["reason"] == "replay failed"


def test_settling_with_bad_peer_as_a_third_participant():
    # One of the three is hostile: the two honest nodes still agree, and it isn't asked to.
    cluster = Cluster(PARAMS, seed=1, node_count=2)
    a, b = cluster.nodes
    peer = BadPeer(cluster, "node-2")
    for data in (b"junk", b"{}", b'{"type":"status","tip":"x"}'):
        peer.send(a, data)
    block = next_block(a.tip_block.header, miner=DAVE)
    coinbase = block.txs[0]
    peer.send_block(a, remined(block, [replace(coinbase, body={**coinbase.body, "amount": 10**6})]))
    build_on(b, 2)
    result = cluster.settle()
    assert_settled_correctly(cluster, result)
    assert result.height == 2
    assert a.misbehavior["node-2"] == 4


# Checkpoints


def test_fork_below_a_checkpoint_is_refused(cluster):
    a = cluster.nodes[0]
    genesis = a.tip_block.header
    main = build_on(a, 3)
    cluster.sim.run_until_idle()
    result = cluster.settle()
    for node in cluster.nodes:
        node.open_day()

    # Even a much longer competing chain can't undo a settled day.
    parent = genesis
    for _ in range(6):
        block = next_block(parent, miner=DAVE)
        deliver(a, block)
        parent = block.header
    assert a.tip == result.tip == main[-1].header.hash_hex
    assert a.state.balance(DAVE.public_hex) == 0
    assert cluster.log.of_kind("reorg") == []
    ignored = cluster.log.of_kind("block_ignored")
    assert ignored[0]["code"] == str(Code.BELOW_CHECKPOINT)
    assert a.misbehavior == {}


def test_block_at_the_checkpoint_height_is_refused(cluster):
    a = cluster.nodes[0]
    parent = a.tip_block.header
    build_on(a, 1)
    cluster.settle()
    deliver(a, next_block(parent, miner=DAVE))
    assert cluster.log.of_kind("block_ignored")[-1]["code"] == str(Code.BELOW_CHECKPOINT)


def test_new_blocks_build_normally_after_a_checkpoint(cluster):
    a, b, _ = cluster.nodes
    build_on(a, 2)
    first = cluster.settle()
    for node in cluster.nodes:
        node.open_day()
    build_on(b, 2)
    second = cluster.settle()
    assert second.height == 4
    assert second.blocks[:3] == first.blocks
    assert_settled_correctly(cluster, second)


def test_reorg_above_a_checkpoint_still_works(cluster):
    a = cluster.nodes[0]
    build_on(a, 1)
    cluster.settle()
    for node in cluster.nodes:
        node.open_day()
    base = a.tip_block.header
    build_on(a, 1, miner=CAROL)
    side = next_block(base, miner=DAVE)
    deliver(a, side)
    deliver(a, next_block(side.header, miner=DAVE))
    assert a.state.balance(DAVE.public_hex) == 100
    assert len(cluster.log.of_kind("reorg")) == 1


def test_finalizing_drops_side_branches_and_old_states(cluster):
    a = cluster.nodes[0]
    genesis = a.tip_block.header
    build_on(a, 2)
    deliver(a, next_block(genesis, miner=DAVE))
    assert len(a.side_blocks()) == 1
    result = cluster.settle()
    assert a.side_blocks() == []
    assert len(result.orphaned) == 1
    states = [entry.state for entry in a._entries.values()]
    assert sum(state is not None for state in states) == 1
    assert a.state == result.state


def test_side_branch_child_is_not_connected_after_its_parent_was_dropped(cluster):
    a = cluster.nodes[0]
    genesis = a.tip_block.header
    build_on(a, 2)
    side = next_block(genesis, miner=DAVE)
    deliver(a, side)
    tip = cluster.settle().tip
    for node in cluster.nodes:
        node.open_day()
    side2 = next_block(side.header, miner=DAVE)
    side3 = next_block(side2.header, miner=DAVE)
    side4 = next_block(side3.header, miner=DAVE)
    for block in (side4, side3, side2):
        deliver(a, block)
    assert a.tip == tip


def test_a_new_node_can_start_from_a_settled_chain(cluster):
    a = cluster.nodes[0]
    a.submit_block(next_block(a.tip_block.header, [pay()]))
    result = cluster.settle()

    tomorrow = Cluster(PARAMS, seed=2, chain=result.blocks)
    node = tomorrow.nodes[0]
    assert node.tip == result.tip
    assert node.state == result.state
    assert node.checkpoint_height == result.height
    assert tomorrow.sim.now == result.blocks[-1].header.timestamp
    assert node.work == cluster.nodes[0].work

    build_on(node, 2)
    second = tomorrow.settle()
    assert second.height == result.height + 2
    assert validate_chain(second.blocks, PARAMS) == second.state


def test_a_node_refuses_to_start_from_a_bad_chain(cluster):
    a = cluster.nodes[0]
    build_on(a, 2)
    blocks = cluster.settle().blocks
    with pytest.raises(ChainError):
        Cluster(PARAMS, seed=2, chain=[blocks[0], blocks[2]])
