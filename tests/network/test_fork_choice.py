"""Fork choice and reorgs, with blocks delivered by hand so the exact order is under the test's control."""

from dataclasses import replace

import pytest
from network_helpers import ALICE, BOB, CAROL, DAVE, PARAMS, build_on, deliver, next_block, pay

from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.validation import cumulative_work, validate_chain
from agenteconomy.network.settle import Cluster


@pytest.fixture
def cluster():
    return Cluster(PARAMS, seed=1)


@pytest.fixture
def node(cluster):
    return cluster.nodes[0]


def test_nodes_start_on_the_same_genesis_but_share_no_state(cluster):
    first, second = cluster.nodes[0], cluster.nodes[1]
    assert first.tip == second.tip
    assert first.height == 0 and first.work == 0
    assert first.state is not second.state
    assert first.mempool is not second.mempool


def test_a_block_extending_the_tip_is_adopted(node):
    block = next_block(node.tip_block.header)
    deliver(node, block)
    assert node.tip == block.header.hash_hex
    assert node.height == 1
    assert node.work == 2**PARAMS.difficulty_bits
    assert node.state.balance(CAROL.public_hex) == 50


def test_work_matches_the_chain_rules_function(node):
    build_on(node, 5)
    assert node.work == cumulative_work([block.header for block in node.main_chain()])


def test_equal_work_keeps_the_block_seen_first(node):
    genesis = node.tip_block.header
    first, second = next_block(genesis, miner=CAROL), next_block(genesis, miner=DAVE)
    assert first.header.hash_hex != second.header.hash_hex
    deliver(node, first)
    deliver(node, second)
    assert node.tip == first.header.hash_hex
    assert node.knows(second.header.hash_hex)


def test_equal_work_really_depends_on_arrival_order(cluster):
    genesis = cluster.nodes[0].tip_block.header
    first, second = next_block(genesis, miner=CAROL), next_block(genesis, miner=DAVE)
    a, b = cluster.nodes[0], cluster.nodes[1]
    deliver(a, first)
    deliver(a, second)
    deliver(b, second)
    deliver(b, first)
    assert a.tip == first.header.hash_hex
    assert b.tip == second.header.hash_hex


def test_lower_work_is_stored_but_not_adopted(node):
    genesis = node.tip_block.header
    main = build_on(node, 2)
    side = next_block(genesis, miner=DAVE)
    deliver(node, side)
    assert node.tip == main[-1].header.hash_hex
    assert node.knows(side.header.hash_hex)
    assert side in node.side_blocks()
    assert node.state.balance(DAVE.public_hex) == 0


def test_side_branch_overtaking_causes_a_reorg(cluster, node):
    genesis = node.tip_block.header
    old = build_on(node, 1, miner=CAROL)[0]
    side1 = next_block(genesis, miner=DAVE)
    side2 = next_block(side1.header, miner=DAVE)
    deliver(node, side1)
    assert node.tip == old.header.hash_hex
    deliver(node, side2)

    assert node.tip == side2.header.hash_hex
    assert [block.header.hash_hex for block in node.main_chain()[1:]] == [side1.header.hash_hex, side2.header.hash_hex]
    assert node.state.balance(CAROL.public_hex) == 0
    assert node.state.balance(DAVE.public_hex) == 100

    reorg = cluster.log.of_kind("reorg")[-1]
    assert reorg["depth"] == 1
    assert reorg["old_tip"] == old.header.hash_hex
    assert reorg["new_tip"] == side2.header.hash_hex
    assert reorg["orphaned"] == [old.header.hash_hex]


def test_simple_extension_is_not_logged_as_a_reorg(cluster, node):
    build_on(node, 3)
    assert cluster.log.of_kind("reorg") == []


def test_reorg_returns_transactions_to_the_mempool(node):
    genesis = node.tip_block.header
    tx = pay()
    node.submit_tx(tx)
    block = next_block(genesis, node.mempool.select(node.state, 10))
    deliver(node, block)
    assert tx not in node.mempool

    side = next_block(genesis, miner=DAVE)
    deliver(node, side)
    deliver(node, next_block(side.header, miner=DAVE))

    assert tx in node.mempool
    assert node.state.nonce(ALICE.public_hex) == 0
    assert node.state.balance(ALICE.public_hex) == 1000


def test_returned_transaction_can_be_mined_again(node):
    genesis = node.tip_block.header
    tx = pay()
    deliver(node, next_block(genesis, [tx]))
    side = next_block(genesis, miner=DAVE)
    deliver(node, side)
    deliver(node, next_block(side.header, miner=DAVE))
    assert tx in node.mempool

    node.submit_block(next_block(node.tip_block.header, node.mempool.select(node.state, 10)))
    assert tx not in node.mempool
    assert node.state.nonce(ALICE.public_hex) == 1


def test_transaction_mined_on_both_branches_is_not_returned(cluster, node):
    genesis = node.tip_block.header
    tx = pay()
    deliver(node, next_block(genesis, [tx], miner=CAROL))
    side = next_block(genesis, [tx], miner=DAVE)
    deliver(node, side)
    deliver(node, next_block(side.header, miner=DAVE))

    assert tx not in node.mempool
    assert node.state.nonce(ALICE.public_hex) == 1
    reorg = cluster.log.of_kind("reorg")[-1]
    assert reorg["returned"] == [] and reorg["refused"] == {}


def test_double_spend_mined_on_two_branches_resolves_to_one(cluster, node):
    to_bob, to_carol = pay(to=BOB, amount=100), pay(to=CAROL, amount=100)
    genesis = node.tip_block.header
    deliver(node, next_block(genesis, [to_bob]))
    assert node.state.balance(BOB.public_hex) == 600

    side = next_block(genesis, [to_carol], miner=DAVE)
    deliver(node, side)
    deliver(node, next_block(side.header, miner=DAVE))

    assert node.state.balance(BOB.public_hex) == 500
    assert node.state.balance(CAROL.public_hex) == 100
    assert to_bob not in node.mempool
    assert cluster.log.of_kind("reorg")[-1]["refused"] == {to_bob.id.hex(): str(Code.NONCE_TOO_LOW)}


def test_coinbase_of_an_orphaned_block_disappears_and_is_never_returned(node):
    genesis = node.tip_block.header
    deliver(node, next_block(genesis, miner=CAROL))
    assert node.state.balance(CAROL.public_hex) == 50
    side = next_block(genesis, miner=DAVE)
    deliver(node, side)
    deliver(node, next_block(side.header, miner=DAVE))
    assert node.state.balance(CAROL.public_hex) == 0
    assert len(node.mempool) == 0


def test_transaction_spending_an_orphaned_reward_is_refused_on_return(cluster, node):
    genesis = node.tip_block.header
    first = next_block(genesis, miner=CAROL)
    spend = pay(CAROL, BOB, 40, nonce=0)
    second = next_block(first.header, [spend], miner=CAROL)
    deliver(node, first)
    deliver(node, second)

    side = next_block(genesis, miner=DAVE)
    side2 = next_block(side.header, miner=DAVE)
    deliver(node, side)
    deliver(node, side2)
    assert node.tip == second.header.hash_hex  # only equal so far
    deliver(node, next_block(side2.header, miner=DAVE))

    assert spend not in node.mempool
    assert cluster.log.of_kind("reorg")[-1]["refused"] == {spend.id.hex(): str(Code.INSUFFICIENT_BALANCE)}


def test_balances_are_correct_after_a_deep_reorg(cluster, node):
    genesis = node.tip_block.header
    parent = genesis
    for nonce in range(6):
        block = next_block(parent, [pay(amount=10 + nonce, nonce=nonce)])
        deliver(node, block)
        parent = block.header

    parent = genesis
    for _ in range(7):
        block = next_block(parent, miner=DAVE)
        deliver(node, block)
        parent = block.header

    assert cluster.log.of_kind("reorg")[-1]["depth"] == 6
    assert node.state.balance(ALICE.public_hex) == 1000
    assert node.state.balance(DAVE.public_hex) == 350
    assert len(node.mempool) == 6
    assert validate_chain(node.main_chain(), PARAMS) == node.state


def test_mempool_transaction_invalidated_by_a_new_block_is_dropped(node):
    waiting = pay(to=BOB, nonce=0)
    node.submit_tx(waiting)
    deliver(node, next_block(node.tip_block.header, [pay(to=CAROL, nonce=0)]))
    assert waiting not in node.mempool


def test_tip_listeners_fire_on_every_change(node):
    calls = []
    node.subscribe_tip(lambda: calls.append(node.tip))
    build_on(node, 2)
    assert len(calls) == 2
    assert calls[-1] == node.tip


def test_side_block_does_not_fire_tip_listeners(node):
    genesis = node.tip_block.header
    build_on(node, 1)
    calls = []
    node.subscribe_tip(lambda: calls.append(1))
    deliver(node, next_block(genesis, miner=DAVE))
    assert calls == []


def test_blocks_spread_to_other_nodes(cluster):
    a, b, c = cluster.nodes
    block = next_block(a.tip_block.header)
    a.submit_block(block)
    cluster.sim.run_until_idle()
    assert a.tip == b.tip == c.tip == block.header.hash_hex


def test_transactions_spread_to_other_nodes_once(cluster):
    a, b, c = cluster.nodes
    tx = pay()
    a.submit_tx(tx)
    cluster.sim.run_until_idle()
    assert tx in b.mempool and tx in c.mempool
    assert len(cluster.log.of_kind("tx_accepted")) == 3


def test_conflicting_transactions_race_and_each_node_keeps_the_first_it_saw(cluster):
    a, b, _ = cluster.nodes
    to_bob, to_carol = pay(to=BOB), pay(to=CAROL)
    a.submit_tx(to_bob)
    b.submit_tx(to_carol)
    cluster.sim.run_until_idle()
    assert to_bob in a.mempool and to_carol not in a.mempool
    assert to_carol in b.mempool and to_bob not in b.mempool
    assert a.misbehavior == {} and b.misbehavior == {}  # a double spend isn't provably the relaying peer's fault


def test_submit_tx_reports_why_it_was_refused(node):
    with pytest.raises(ChainError) as e:
        node.submit_tx(pay(amount=10**6))
    assert e.value.code == Code.INSUFFICIENT_BALANCE


def test_mine_with_a_budget_continues_rather_than_retrying_the_same_hashes(node):
    # A budget of 1 can only ever succeed if each call tries a new nonce.
    for _ in range(10_000):
        if node.mine(CAROL.public_hex, 1) is not None:
            break
    assert node.height == 1
    assert node.state.balance(CAROL.public_hex) == 50


def test_mine_budget_that_runs_out_returns_none_and_changes_nothing():
    hard = Cluster(replace(PARAMS, difficulty_bits=40), seed=1)
    node = hard.nodes[0]
    assert node.mine(CAROL.public_hex, 100) is None
    assert node.height == 0


def test_mined_block_includes_waiting_transactions_and_is_relayed(cluster):
    a, b, _ = cluster.nodes
    tx = pay()
    a.submit_tx(tx)
    while a.height == 0:
        a.mine(CAROL.public_hex, 1000)
    cluster.sim.run_until_idle()
    assert b.tip == a.tip
    assert tx in a.tip_block.txs
    assert tx not in b.mempool


def test_block_timestamps_follow_the_simulated_clock(cluster):
    a = cluster.nodes[0]
    cluster.run_for(5000)
    while a.height == 0:
        a.mine(CAROL.public_hex, 1000)
    assert a.tip_block.header.timestamp == 5000
