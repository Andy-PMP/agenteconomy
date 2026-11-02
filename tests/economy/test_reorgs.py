"""The economy on real nodes: a block that loses a fork takes everything about it along."""

from dataclasses import replace

import pytest
from economy_helpers import ALICE, BOB, CAROL, DAVE, PARAMS, assert_sound, finished

from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.transaction import sign_accept, sign_offer, sign_reject
from agenteconomy.chain.validation import build_block, validate_chain
from agenteconomy.network.messages import encode_block, encode_tx
from agenteconomy.network.settle import Cluster


@pytest.fixture
def cluster():
    return Cluster(PARAMS, seed=1)


@pytest.fixture
def node(cluster):
    return cluster.nodes[0]


def block_on(parent, *, miner=CAROL, txs=(), supply=1500):
    return finished(build_block(PARAMS, parent, miner=miner.public_hex, txs=list(txs), timestamp=parent.timestamp,
                                supply=supply))


def deliver(node, block):
    node.on_message("peer-x", encode_block(block))


def offer_tx(keys, to, nonce=0, expires_at=40, **terms):
    return sign_offer(keys, PARAMS.chain_id, to=to.public_hex, expires_at=expires_at, nonce=nonce, fee=1, **terms)


def overtake(node, parent, count, *, miner=DAVE):
    """Deliver a competing branch of `count` blocks, built from `parent`."""
    state = node.state
    supply = state.supply
    blocks = []
    for _ in range(count):
        block = block_on(parent, miner=miner, supply=supply)
        supply += block.txs[0].body["amount"]
        blocks.append(block)
        parent = block.header
    for block in blocks:
        deliver(node, block)
    return blocks


def test_creature_from_an_orphaned_block_disappears(cluster, node):
    genesis = node.tip_block.header
    block = block_on(genesis, miner=CAROL)
    deliver(node, block)
    creature = block.header.hash_hex
    assert creature in node.state.creatures

    winners = overtake(node, genesis, 2)
    assert creature not in node.state.creatures
    assert creature not in node.state.owners
    assert len(node.state.creatures) == 2
    assert {block.header.hash_hex for block in winners} == set(node.state.creatures)
    assert cluster.log.of_kind("reorg")[-1]["orphaned"] == [creature]
    assert_sound(node.state)


def test_the_orphaned_miners_reward_goes_too(node):
    genesis = node.tip_block.header
    deliver(node, block_on(genesis, miner=CAROL))
    overtake(node, genesis, 2)
    assert node.state.balance(CAROL.public_hex) == 0
    # The soft cap's reward at this supply, not the 50 base.
    first = PARAMS.reward_at(1500)
    assert node.state.balance(DAVE.public_hex) == first + PARAMS.reward_at(1500 + first)


def test_a_trade_of_an_orphaned_creature_unwinds(node):
    genesis = node.tip_block.header
    hatched = block_on(genesis, miner=ALICE)
    creature = hatched.header.hash_hex
    offer = offer_tx(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    with_offer = block_on(hatched.header, txs=[offer])
    accept = sign_accept(BOB, PARAMS.chain_id, offer_id=offer.id.hex(), nonce=0, fee=1)
    with_accept = block_on(with_offer.header, txs=[accept])
    for block in (hatched, with_offer, with_accept):
        deliver(node, block)
    assert node.state.owners[creature] == BOB.public_hex

    overtake(node, genesis, 4)

    assert creature not in node.state.owners
    assert node.state.balance(BOB.public_hex) == 500
    assert node.state.offers == {}


def test_transactions_depending_on_an_orphaned_creature_are_refused_not_stuck(cluster, node):
    genesis = node.tip_block.header
    hatched = block_on(genesis, miner=ALICE)
    creature = hatched.header.hash_hex
    offer = offer_tx(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    deliver(node, hatched)
    deliver(node, block_on(hatched.header, txs=[offer]))

    overtake(node, genesis, 3)

    assert offer not in node.mempool
    assert cluster.log.of_kind("reorg")[-1]["refused"] == {offer.id.hex(): str(Code.NOT_OWNER)}

    # Alice's nonce 0 is free again, and nothing is holding up her queue.
    other = offer_tx(ALICE, BOB, give_coins=1)
    node.submit_tx(other)
    assert node.mempool.select(node.state, 10) == [other]


def test_an_offer_that_does_not_depend_on_the_lost_branch_comes_back(node):
    genesis = node.tip_block.header
    offer = offer_tx(ALICE, BOB, give_coins=5)
    deliver(node, block_on(genesis, txs=[offer]))
    assert offer.id.hex() in node.state.offers

    overtake(node, genesis, 2)

    assert node.state.offers == {}
    assert offer in node.mempool


def test_an_accept_comes_back_only_if_its_offer_is_open_on_the_new_branch(cluster, node):
    genesis = node.tip_block.header
    offer = offer_tx(ALICE, BOB, give_coins=5)
    both_sides = block_on(genesis, txs=[offer], miner=DAVE)
    accept = sign_accept(BOB, PARAMS.chain_id, offer_id=offer.id.hex(), nonce=0, fee=1)
    deliver(node, both_sides)
    deliver(node, block_on(both_sides.header, txs=[accept], miner=CAROL))
    assert node.state.balance(BOB.public_hex) == 500 + 5 - 1

    # The new branch includes the offer but not the accept.
    supply = node.state.supply
    second = block_on(both_sides.header, miner=DAVE, supply=supply)
    third = block_on(second.header, miner=DAVE, supply=supply + 50)
    deliver(node, second)
    deliver(node, third)

    assert node.state.offers.keys() == {offer.id.hex()}
    assert accept in node.mempool


def test_a_withdrawn_offer_reopens_if_the_withdrawal_is_orphaned(node):
    genesis = node.tip_block.header
    offer = offer_tx(ALICE, BOB, give_coins=5)
    opened = block_on(genesis, txs=[offer])
    withdraw = sign_reject(ALICE, PARAMS.chain_id, offer_id=offer.id.hex(), nonce=1, fee=1)
    deliver(node, opened)
    deliver(node, block_on(opened.header, txs=[withdraw]))
    assert node.state.offers == {}

    supply = node.state.supply
    second = block_on(opened.header, miner=DAVE, supply=supply)
    deliver(node, second)
    deliver(node, block_on(second.header, miner=DAVE, supply=supply + 50))
    assert offer.id.hex() in node.state.offers


def test_fork_where_the_same_creature_is_sold_two_ways_resolves_to_one(cluster, node):
    genesis = node.tip_block.header
    hatched = block_on(genesis, miner=ALICE)
    creature = hatched.header.hash_hex
    deliver(node, hatched)
    to_bob = offer_tx(ALICE, BOB, give_creatures=(creature,), want_coins=10)
    to_dave = offer_tx(ALICE, DAVE, give_creatures=(creature,), want_coins=10)
    deliver(node, block_on(hatched.header, txs=[to_bob]))

    supply = 1550
    second = block_on(hatched.header, txs=[to_dave], miner=DAVE, supply=supply)
    deliver(node, second)
    deliver(node, block_on(second.header, miner=DAVE, supply=supply + 50))
    assert node.state.offers.keys() == {to_dave.id.hex()}
    assert to_bob not in node.mempool  # same nonce as the offer that won


def test_state_after_a_reorg_equals_a_replay_of_the_winning_chain(node):
    genesis = node.tip_block.header
    deliver(node, block_on(genesis, miner=ALICE))
    overtake(node, genesis, 3)
    assert validate_chain(node.main_chain(), PARAMS) == node.state


def test_soft_cap_rewards_are_judged_per_branch():
    # Each branch has its own supply, so a block must pay what its own branch owes.
    params = replace(PARAMS, supply_cap=2_000, block_reward=1_000)
    cluster = Cluster(params, seed=1)
    node = cluster.nodes[0]
    template = node.build_template(CAROL.public_hex)
    assert template.txs[0].body["amount"] == params.reward_at(1500)
    while node.mine(CAROL.public_hex, 10_000) is None:
        pass
    assert node.state.supply == 1500 + params.reward_at(1500)
    next_template = node.build_template(CAROL.public_hex)
    assert next_template.txs[0].body["amount"] == params.reward_at(node.state.supply)
    assert next_template.txs[0].body["amount"] < template.txs[0].body["amount"]


def test_node_drops_and_logs_a_stale_transaction(cluster, node):
    offer = offer_tx(ALICE, BOB, give_coins=5)
    deliver(node, block_on(node.tip_block.header, txs=[offer]))
    accept = sign_accept(BOB, PARAMS.chain_id, offer_id=offer.id.hex(), nonce=0, fee=1)
    node.submit_tx(accept)
    withdraw = sign_reject(ALICE, PARAMS.chain_id, offer_id=offer.id.hex(), nonce=1, fee=1)
    deliver(node, block_on(node.tip_block.header, txs=[withdraw], supply=node.state.supply))

    assert accept not in node.mempool
    entry = cluster.log.of_kind("tx_dropped")[-1]
    assert entry["tx"] == accept.id.hex() and entry["code"] == str(Code.OFFER_NOT_OPEN)
    assert entry["sender"] == BOB.public_hex


def test_invalid_offer_from_a_peer_counts_against_it_but_a_stale_one_does_not(cluster, node):
    bad_terms = offer_tx(ALICE, BOB, give_coins=1, want_coins=1)
    node.on_message("peer-x", encode_tx(bad_terms))
    assert node.misbehavior == {"peer-x": 1}

    # Whether a creature is owned can differ between nodes in normal operation, so this one is forgiven.
    not_yet_mine = offer_tx(ALICE, BOB, give_creatures=("ab" * 32,), want_coins=1)
    node.on_message("peer-y", encode_tx(not_yet_mine))
    assert "peer-y" not in node.misbehavior
    with pytest.raises(ChainError):
        node.submit_tx(not_yet_mine)
