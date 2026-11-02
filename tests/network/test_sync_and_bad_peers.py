"""A node that falls behind catches up, and a peer sending anything at all can't break agreement."""

from dataclasses import replace

import pytest
from network_helpers import ALICE, BOB, CAROL, DAVE, PARAMS, BadPeer, build_on, deliver, next_block, pay, remined

from agenteconomy.chain.block import Block
from agenteconomy.chain.errors import Code
from agenteconomy.chain.transaction import make_coinbase
from agenteconomy.network.messages import (
    MAX_SYNC_BATCH,
    decode,
    encode_block,
    encode_blocks,
    encode_get_blocks,
    encode_status,
    encode_tx,
)
from agenteconomy.network.settle import Cluster


@pytest.fixture
def cluster():
    return Cluster(PARAMS, seed=1)


def rejected_codes(cluster, node=None):
    return [entry["code"] for entry in cluster.log.of_kind("rejected") if node is None or entry["node"] == node.id]


# Falling behind


def test_offline_node_catches_up_when_it_returns(cluster):
    a, b, c = cluster.nodes
    cluster.network.set_offline(c.id)
    build_on(a, 5)
    cluster.sim.run_until_idle()
    assert b.tip == a.tip
    assert c.height == 0

    cluster.network.set_offline(c.id, False)
    cluster.sim.run_until_idle()
    assert c.tip == a.tip
    assert c.state == a.state


def test_catching_up_works_past_a_single_batch(cluster):
    a, _, c = cluster.nodes
    cluster.network.set_offline(c.id)
    build_on(a, MAX_SYNC_BATCH * 2 + 7)
    cluster.sim.run_until_idle()

    cluster.network.set_offline(c.id, False)
    cluster.sim.run_until_idle()
    assert c.height == MAX_SYNC_BATCH * 2 + 7
    assert c.tip == a.tip


def test_node_ahead_tells_a_returning_node_it_is_behind(cluster):
    a, _, c = cluster.nodes
    cluster.network.set_offline(c.id)
    build_on(a, 2)
    cluster.sim.run_until_idle()
    cluster.network.set_offline(c.id, False)
    cluster.sim.run_until_idle()
    assert c.height == 2


def test_partitioned_sides_build_competing_chains_and_more_work_wins(cluster):
    a, b, c = cluster.nodes
    cluster.network.partition([a.id], [b.id, c.id])
    build_on(a, 2, miner=CAROL)
    build_on(b, 4, miner=DAVE)
    cluster.sim.run_until_idle()
    assert a.height == 2 and c.height == 4
    assert a.tip != c.tip

    cluster.network.heal()
    cluster.sim.run_until_idle()
    assert a.tip == b.tip == c.tip
    assert a.height == 4
    assert a.state.balance(CAROL.public_hex) == 0
    assert any(entry["node"] == a.id and entry["depth"] == 2 for entry in cluster.log.of_kind("reorg"))


def test_transactions_from_the_losing_side_return_to_its_mempool(cluster):
    a, b, c = cluster.nodes
    cluster.network.partition([a.id], [b.id, c.id])
    tx = pay()
    a.submit_tx(tx)
    a.submit_block(next_block(a.tip_block.header, [tx]))
    build_on(b, 2, miner=DAVE)
    cluster.sim.run_until_idle()

    cluster.network.heal()
    cluster.sim.run_until_idle()
    assert a.tip == b.tip
    assert tx in a.mempool
    assert a.state.nonce(ALICE.public_hex) == 0


def test_block_arriving_before_its_parent_waits_then_connects(cluster):
    node = cluster.nodes[0]
    first = next_block(node.tip_block.header)
    second = next_block(first.header)
    third = next_block(second.header)
    deliver(node, third)
    deliver(node, second)
    assert node.height == 0
    deliver(node, first)
    assert node.tip == third.header.hash_hex


def test_unknown_parent_asks_the_sender_for_the_chain(cluster):
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    first = next_block(node.tip_block.header)
    second = next_block(first.header)
    peer.send_block(node, second)
    cluster.sim.run_until_idle()
    assert any(data.startswith(b'{"locator"') for data in peer.received)


def test_sync_request_is_not_repeated_while_one_is_outstanding(cluster):
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    claims = [encode_status("ab" * 32, 50, 10**6), encode_status("cd" * 32, 51, 10**6)]
    for claim in claims:
        peer.send(node, claim)
    cluster.sim.run_until_idle()
    assert sum(data.startswith(b'{"locator"') for data in peer.received) == 1


def test_sync_is_retried_after_a_timeout(cluster):
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    peer.send(node, encode_status("ab" * 32, 50, 10**6))
    cluster.sim.run_until_idle()
    cluster.run_for(6_000)
    peer.send(node, encode_status("ab" * 32, 50, 10**6))
    cluster.sim.run_until_idle()
    assert sum(data.startswith(b'{"locator"') for data in peer.received) == 2


def test_get_blocks_returns_what_follows_the_newest_shared_hash(cluster):
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    blocks = build_on(node, 5)
    peer.send(node, encode_get_blocks(["ee" * 32, blocks[1].header.hash_hex, node.main_chain()[0].header.hash_hex]))
    cluster.sim.run_until_idle()
    reply = [data for data in peer.received if data.startswith(b'{"blocks"')][-1]
    assert [block.header.hash_hex for block in decode(reply).blocks] == [b.header.hash_hex for b in blocks[2:]]


def test_get_blocks_ignores_hashes_on_side_branches(cluster):
    node = cluster.nodes[0]
    genesis = node.tip_block.header
    peer = BadPeer(cluster)
    main = build_on(node, 3)
    side = next_block(genesis, miner=DAVE)
    deliver(node, side)
    peer.send(node, encode_get_blocks([side.header.hash_hex]))
    cluster.sim.run_until_idle()
    reply = [data for data in peer.received if data.startswith(b'{"blocks"')][-1]
    assert [block.header.hash_hex for block in decode(reply).blocks] == [b.header.hash_hex for b in main]


def test_locator_stays_within_its_limit_on_a_long_chain(cluster):
    node = cluster.nodes[0]
    build_on(node, 300)
    locator = node._locator()
    assert len(locator) <= 64
    assert locator[0] == node.tip
    assert locator[-1] == node.main_chain()[0].header.hash_hex


# Bad peers. Each must leave the honest nodes in agreement.


def unmined(block: Block) -> Block:
    """Adjust the nonce until the hash fails the target, so proof of work is what's wrong."""
    nonce = block.header.nonce
    while True:
        nonce += 1
        header = replace(block.header, nonce=nonce)
        if int.from_bytes(header.hash, "big") >> (256 - PARAMS.difficulty_bits):
            return Block(header, block.txs)


def forged(tx):
    return replace(tx, body={**tx.body, "amount": tx.body["amount"] + 1})


def inflated_coinbase(block: Block) -> Block:
    coinbase = block.txs[0]
    return remined(block, [replace(coinbase, body={**coinbase.body, "amount": 10**6})])


@pytest.mark.parametrize("make_bad, code", [
    (lambda block: unmined(block), Code.BAD_POW),
    (lambda block: Block(replace(block.header, difficulty_bits=0), block.txs), Code.BAD_DIFFICULTY),
    (lambda block: inflated_coinbase(block), Code.BAD_COINBASE),
    (lambda block: remined(block, [block.txs[0], forged(pay())]), Code.BAD_SIGNATURE),
    (lambda block: remined(block, [block.txs[0], pay(nonce=5)]), Code.BAD_NONCE),
    (lambda block: remined(block, [block.txs[0], pay(amount=10**6)]), Code.INSUFFICIENT_BALANCE),
    (lambda block: remined(block, [block.txs[0], block.txs[0]]), Code.DUPLICATE_TX),
    (lambda block: remined(replace(block, header=replace(block.header, height=9))), Code.BAD_HEIGHT),
    (lambda block: Block(block.header, ()), Code.BAD_COINBASE),
])
def test_invalid_blocks_are_rejected_and_not_relayed(cluster, make_bad, code):
    a, b, c = cluster.nodes
    peer = BadPeer(cluster)
    bad = make_bad(next_block(a.tip_block.header))
    peer.send_block(a, bad)
    cluster.sim.run_until_idle()

    assert rejected_codes(cluster, a) == [str(code)]
    assert a.height == 0 and b.height == 0 and c.height == 0
    assert rejected_codes(cluster, b) == []  # the honest node stopped it: nothing reached the others
    assert a.misbehavior == {peer.id: 1}


def test_timestamp_before_the_parent_is_rejected(cluster):
    node = cluster.nodes[0]
    first = next_block(node.tip_block.header, timestamp=500)
    deliver(node, first)
    deliver(node, next_block(first.header, timestamp=100))
    assert rejected_codes(cluster, node) == [str(Code.BAD_TIMESTAMP)]
    assert node.height == 1


@pytest.mark.parametrize("swap", [
    lambda block: Block(block.header, (block.txs[0], pay(amount=999))),
    lambda block: Block(block.header, (block.txs[0],)),
    lambda block: Block(block.header, ()),
    lambda block: Block(block.header, (block.txs[0],) * 50),
])
def test_swapped_transactions_cannot_poison_the_honest_block(cluster, swap):
    """A real header with different transactions must not get the real block refused afterwards."""
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    honest = next_block(node.tip_block.header, [pay(amount=5)])
    peer.send_block(node, swap(honest))
    cluster.sim.run_until_idle()
    assert node.height == 0

    deliver(node, honest, sender="honest-peer")
    assert node.tip == honest.header.hash_hex
    assert node.misbehavior.get("honest-peer") is None


def test_child_of_an_invalid_block_is_rejected(cluster):
    node = cluster.nodes[0]
    bad = inflated_coinbase(next_block(node.tip_block.header))
    child = next_block(bad.header)
    deliver(node, bad)
    deliver(node, child)
    assert rejected_codes(cluster, node) == [str(Code.BAD_COINBASE), str(Code.INVALID_PARENT)]
    assert node.height == 0


def test_orphan_child_is_rejected_when_its_parent_turns_out_invalid(cluster):
    node = cluster.nodes[0]
    bad = inflated_coinbase(next_block(node.tip_block.header))
    child = next_block(bad.header)
    grandchild = next_block(child.header)
    deliver(node, grandchild)
    deliver(node, child)
    deliver(node, bad)
    assert rejected_codes(cluster, node) == [str(Code.BAD_COINBASE), str(Code.INVALID_PARENT), str(Code.INVALID_PARENT)]
    assert node.height == 0
    deliver(node, child)
    assert rejected_codes(cluster, node)[-1] == str(Code.KNOWN_INVALID)


def test_resending_a_known_invalid_block_counts_against_the_peer(cluster):
    node = cluster.nodes[0]
    bad = inflated_coinbase(next_block(node.tip_block.header))
    deliver(node, bad)
    deliver(node, bad)
    assert rejected_codes(cluster, node) == [str(Code.BAD_COINBASE), str(Code.KNOWN_INVALID)]
    assert node.misbehavior["peer-x"] == 2


@pytest.mark.parametrize("data", [
    b"", b"garbage", b"\xff\x00", b"{}", b'{"type":"unknown"}', b"[1,2,3]",
    b'{"type":"block","block":null}', b'{"type":"tx","tx":{"memo":"hello"}}', b'{"type":"blocks","blocks":[1]}',
    b'{"type":"get_blocks","locator":["x"]}', b'{"type":"status","tip":"x","height":0,"work":"1"}',
    b'{"a":1,"a":2}', b"1.5",
])
def test_junk_messages_count_against_the_peer_and_nothing_else(cluster, data):
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    tip = node.tip
    peer.send(node, data)
    cluster.sim.run_until_idle()
    assert node.misbehavior == {peer.id: 1}
    assert node.tip == tip


def test_oversized_locator_is_refused(cluster):
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    peer.send(node, encode_get_blocks(["ab" * 32] * 65))
    cluster.sim.run_until_idle()
    assert node.misbehavior == {peer.id: 1}
    assert peer.received == []


def test_orphan_flood_stays_within_the_limit_and_real_blocks_still_work(cluster):
    node = Cluster(PARAMS, seed=2, max_orphans=5).nodes[0]
    genesis = node.tip_block.header
    fake_parent = replace(genesis, height=1000, prev_hash="ab" * 32)
    for i in range(20):
        deliver(node, next_block(fake_parent, miner=CAROL, timestamp=i))
    assert len(node._orphans) == 5
    block = next_block(genesis)
    deliver(node, block)
    assert node.tip == block.header.hash_hex


def test_far_future_block_is_ignored_without_penalty_and_accepted_later(cluster):
    node = cluster.nodes[0]
    block = next_block(node.tip_block.header, timestamp=500_000)
    deliver(node, block)
    assert node.height == 0
    assert node.misbehavior == {}
    assert cluster.log.of_kind("block_ignored")[-1]["code"] == str(Code.FUTURE_TIMESTAMP)

    cluster.run_for(500_000)
    deliver(node, block)
    assert node.tip == block.header.hash_hex


def test_block_at_the_future_limit_is_accepted(cluster):
    node = cluster.nodes[0]
    deliver(node, next_block(node.tip_block.header, timestamp=60_000))
    assert node.height == 1
    second = Cluster(PARAMS, seed=2).nodes[0]
    deliver(second, next_block(second.tip_block.header, timestamp=60_001))
    assert second.height == 0


def test_lying_about_work_changes_nothing_and_does_not_loop(cluster):
    a, _, _ = cluster.nodes
    peer = BadPeer(cluster)
    tip = a.tip
    peer.send(a, encode_status("ab" * 32, 10**6, 2**200))
    cluster.sim.run_until_idle()
    assert a.tip == tip
    assert a.work == 0
    assert sum(data.startswith(b'{"locator"') for data in peer.received) == 1


def test_low_status_gets_a_correction_but_equal_does_not(cluster):
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    build_on(node, 1)
    cluster.sim.run_until_idle()
    peer.received.clear()
    peer.send(node, encode_status(node.tip, 1, node.work))
    cluster.sim.run_until_idle()
    assert peer.received == []
    peer.send(node, encode_status("ab" * 32, 0, 0))
    cluster.sim.run_until_idle()
    assert len(peer.received) == 1


def test_equal_work_status_does_not_trigger_sync_during_the_day(cluster):
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    peer.send(node, encode_status("ab" * 32, 0, 0))
    cluster.sim.run_until_idle()
    assert peer.received == []


def test_junk_sync_reply_is_rejected(cluster):
    node = cluster.nodes[0]
    peer = BadPeer(cluster)
    block = next_block(node.tip_block.header)
    peer.send(node, encode_blocks([inflated_coinbase(block)]))
    cluster.sim.run_until_idle()
    assert rejected_codes(cluster, node) == [str(Code.BAD_COINBASE)]
    assert node.height == 0


def test_invalid_block_in_a_batch_does_not_undo_the_valid_ones_before_it(cluster):
    node = cluster.nodes[0]
    good = next_block(node.tip_block.header)
    bad = inflated_coinbase(next_block(good.header))
    node.on_message("peer-x", encode_blocks([good, bad]))
    assert node.tip == good.header.hash_hex
    assert rejected_codes(cluster, node) == [str(Code.BAD_COINBASE)]


@pytest.mark.parametrize("tx, code", [
    (forged(pay()), Code.BAD_SIGNATURE),
    (pay(ALICE, ALICE), Code.SELF_TRANSFER),
    (pay(fee=0), Code.FEE_TOO_LOW),
    (pay(amount=0), Code.INVALID_AMOUNT),
    (make_coinbase(PARAMS, to=ALICE.public_hex, height=1), Code.UNEXPECTED_KIND),
])
def test_provably_bad_transactions_count_against_the_peer_and_stop_here(cluster, tx, code):
    a, b, _ = cluster.nodes
    peer = BadPeer(cluster)
    peer.send(a, encode_tx(tx))
    cluster.sim.run_until_idle()
    assert rejected_codes(cluster, a) == [str(code)]
    assert a.misbehavior == {peer.id: 1}
    assert tx not in b.mempool


@pytest.mark.parametrize("tx", [pay(nonce=0, amount=10**6), pay(BOB, ALICE, amount=10**6)])
def test_transaction_that_is_merely_unaffordable_is_not_held_against_the_peer(cluster, tx):
    # Balances differ between nodes in normal operation, so an honest peer can relay one of these.
    node = cluster.nodes[0]
    node.on_message("peer-x", encode_tx(tx))
    assert node.misbehavior == {}
    assert tx not in node.mempool


def test_transactions_are_ignored_once_already_known(cluster):
    node = cluster.nodes[0]
    tx = pay()
    node.on_message("peer-x", encode_tx(tx))
    node.on_message("peer-x", encode_tx(tx))
    assert node.misbehavior == {}
    assert len(node.mempool) == 1


def test_repeat_offender_is_banned_and_ignored(cluster):
    node = Cluster(PARAMS, seed=3, ban_threshold=3).nodes[0]
    for _ in range(3):
        node.on_message("offender", b"junk")
    assert node.banned == {"offender"}

    block = next_block(node.tip_block.header)
    node.on_message("offender", encode_block(block))
    assert node.height == 0
    node.on_message("other", encode_block(block))
    assert node.height == 1


def test_ban_is_logged_once_and_only_at_the_threshold():
    cluster = Cluster(PARAMS, seed=3, ban_threshold=3)
    node = cluster.nodes[0]
    node.on_message("offender", b"junk")
    node.on_message("offender", b"junk")
    assert node.banned == set()
    node.on_message("offender", b"junk")
    node.on_message("offender", b"junk")
    assert len(cluster.log.of_kind("peer_banned")) == 1


def test_banned_peer_gets_no_more_relays():
    cluster = Cluster(PARAMS, seed=3, ban_threshold=1, node_count=1)
    a = cluster.nodes[0]
    peer = BadPeer(cluster)
    peer.send(a, b"junk")
    cluster.sim.run_until_idle()
    assert peer.id in a.banned
    a.submit_block(next_block(a.tip_block.header))
    cluster.sim.run_until_idle()
    assert peer.received == []


def test_honest_nodes_are_never_penalized_in_normal_traffic(cluster):
    a, b, c = cluster.nodes
    a.submit_tx(pay(nonce=0))
    b.submit_tx(pay(BOB, ALICE, nonce=0))
    build_on(c, 3)
    cluster.sim.run_until_idle()
    assert a.misbehavior == b.misbehavior == c.misbehavior == {}


def test_own_invalid_block_is_refused_without_blaming_a_peer(cluster):
    node = cluster.nodes[0]
    node.submit_block(inflated_coinbase(next_block(node.tip_block.header)))
    assert node.height == 0
    assert node.misbehavior == {}
    assert rejected_codes(cluster, node) == [str(Code.BAD_COINBASE)]

