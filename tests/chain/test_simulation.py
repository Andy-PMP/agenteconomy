"""Random traffic through the mempool and blocks, seeded so failures reproduce. The rules hold after every block."""

import random
from dataclasses import replace

import pytest
from chain_helpers import ALICE, BOB, CAROL, PARAMS, mined_block, pay

from agenteconomy.chain.errors import ChainError
from agenteconomy.chain.mempool import Mempool
from agenteconomy.chain.state import State, apply_block
from agenteconomy.chain.transaction import make_genesis_tx
from agenteconomy.chain.validation import make_genesis, validate_block, validate_chain
from agenteconomy.chain.vectors import sample_keys

DAVE = sample_keys("dave")
ACCOUNTS = [ALICE, BOB, CAROL, DAVE]


def assert_invariants(state: State) -> None:
    assert all(balance >= 0 for balance in state.balances.values())
    assert sum(state.balances.values()) == state.minted - state.burned


def random_attempt(rng: random.Random, state: State):
    """Usually sensible, sometimes an attack, as agents can attempt anything."""
    sender, to = rng.sample(ACCOUNTS, 2)
    nonce = state.nonce(sender.public_hex) + rng.choice([0, 0, 0, 0, 1, 2, -1])
    amount = rng.choice([1, 5, 50, 500, 5000])
    tx = pay(sender, to, amount, nonce=max(nonce, 0), fee=rng.choice([0, 1, 1, 2, 10]))
    match rng.randrange(12):
        case 0:
            return replace(tx, body={**tx.body, "amount": amount + 1})  # changed after signing
        case 1:
            return replace(tx, sender=rng.choice(ACCOUNTS).public_hex)  # claims to be someone else
        case 2:
            return pay(sender, sender, amount, nonce=max(nonce, 0))  # pays itself
        case _:
            return tx


@pytest.mark.parametrize("seed", range(5))
def test_rules_hold_under_random_traffic(seed):
    rng = random.Random(seed)
    blocks = [make_genesis(PARAMS)]
    state = apply_block(State(), blocks[0])
    pool = Mempool(PARAMS)
    accepted = refused = included = 0

    for _ in range(40):
        for _ in range(rng.randrange(8)):
            try:
                pool.add(random_attempt(rng, state), state)
                accepted += 1
            except ChainError:
                refused += 1

        block = mined_block(blocks[-1].header, pool.select(state, PARAMS.max_block_txs - 1),
                            miner=rng.choice(ACCOUNTS))
        previous_supply = state.supply
        state = validate_block(block, blocks[-1].header, state, PARAMS)
        pool.on_new_tip(block, state)
        blocks.append(block)
        included += len(block.txs) - 1

        assert_invariants(state)
        burned_here = sum(tx.fee for tx in block.txs)
        assert state.supply == previous_supply + PARAMS.block_reward - burned_here

    assert validate_chain(blocks, PARAMS) == state
    # Guards the test itself: it must have exercised both outcomes.
    assert included > 20 and refused > 20 and accepted >= included


def test_same_seed_gives_the_same_chain():
    def run() -> str:
        rng = random.Random(7)
        blocks = [make_genesis(PARAMS)]
        state = apply_block(State(), blocks[0])
        pool = Mempool(PARAMS)
        for _ in range(10):
            for _ in range(5):
                try:
                    pool.add(random_attempt(rng, state), state)
                except ChainError:
                    pass
            block = mined_block(blocks[-1].header, pool.select(state, 5))
            state = validate_block(block, blocks[-1].header, state, PARAMS)
            pool.on_new_tip(block, state)
            blocks.append(block)
        return blocks[-1].header.hash_hex

    assert run() == run()


def test_no_transaction_can_mint_coins():
    state = apply_block(State(), make_genesis(PARAMS))
    mint = make_genesis_tx(PARAMS, to=ALICE.public_hex, amount=1)
    with pytest.raises(ChainError):
        Mempool(PARAMS).add(mint, state)
