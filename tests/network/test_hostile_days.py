"""Whole busy days with a hostile peer attacking throughout. Honest nodes must end up exactly where they should."""

from dataclasses import replace

import pytest
from network_helpers import BadPeer, assert_settled_correctly
from scenarios import KEYS, PARAMS, run_busy_day

from agenteconomy.chain.block import Block, compute_merkle_root, mine
from agenteconomy.chain.transaction import sign_transfer
from agenteconomy.chain.validation import validate_chain
from agenteconomy.network.messages import encode_block, encode_status, encode_tx
from agenteconomy.network.settle import Cluster


def inflated_block(template: Block) -> Block:
    """Properly mined and internally consistent, but paying itself far too much."""
    coinbase = template.txs[0]
    txs = (replace(coinbase, body={**coinbase.body, "amount": 10**6}), *template.txs[1:])
    header = mine(replace(template.header, merkle_root=compute_merkle_root(txs), nonce=0), max_attempts=1 << 20)
    return Block(header, txs)


def attack(cluster: Cluster, sent: list[str]):
    """Start a peer that sends something bad at random, and return how to stop it."""
    peer = BadPeer(cluster)
    rng = cluster.sim.rng("attacker")
    event = {}

    def send_one() -> None:
        node = rng.choice(cluster.nodes)
        template = node.build_template(KEYS[0].public_hex)
        kind = rng.choice(["garbage", "inflated", "swapped", "lie", "forged_tx", "future"])
        sent.append(kind)
        match kind:
            case "garbage":
                peer.send(node, rng.randbytes(rng.randint(0, 200)))
            case "inflated":
                peer.send_block(node, inflated_block(template))
            case "swapped":
                header = mine(template.header, max_attempts=1 << 20)
                peer.send_block(node, Block(header, (template.txs[0], template.txs[0])))
            case "lie":
                peer.send(node, encode_status("ab" * 32, 10**6, 2**200))
            case "forged_tx":
                tx = sign_transfer(KEYS[1], PARAMS.chain_id, to=KEYS[2].public_hex, amount=1, nonce=0, fee=1)
                peer.send(node, encode_tx(replace(tx, fee=9)))
            case "future":
                far = replace(template.header, timestamp=template.header.timestamp + 10**9)
                peer.send(node, encode_block(Block(mine(far, max_attempts=1 << 20), template.txs)))
        event["next"] = cluster.sim.schedule(rng.randint(1, 400), send_one)

    event["next"] = cluster.sim.schedule(1, send_one)
    return lambda: event["next"].cancel()


@pytest.mark.parametrize("seed", range(6))
def test_honest_nodes_agree_despite_an_attacker_all_day(seed):
    sent: list[str] = []
    cluster, result = run_busy_day(
        f"hostile-{seed}",
        duration_ms=15_000,
        max_latency_ms=[100, 800][seed % 2],
        drop_rate=[0.0, 0.2][(seed // 2) % 2],
        partition=seed % 3 == 0,
        during=lambda c: attack(c, sent),
    )
    assert_settled_correctly(cluster, result)
    assert validate_chain(result.blocks, PARAMS) == result.state
    assert result.height > 2

    # Guards the test itself: the attack must have happened, in every form, and been noticed.
    assert set(sent) == {"garbage", "inflated", "swapped", "lie", "forged_tx", "future"}
    assert any(node.misbehavior for node in cluster.nodes)

    # Only the attacker was ever blamed: honest nodes never accused each other.
    for node in cluster.nodes:
        assert set(node.misbehavior) <= {"bad-peer"}
        assert node.banned <= {"bad-peer"}

    # None of its blocks got in: every coinbase on the settled chain pays the real reward.
    assert all(block.txs[0].body["amount"] == PARAMS.block_reward for block in result.blocks[1:])
