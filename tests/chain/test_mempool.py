from dataclasses import replace

import pytest
from chain_helpers import ALICE, BOB, CAROL, PARAMS, genesis_state, mined_block, pay

from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.mempool import Mempool
from agenteconomy.chain.state import apply_block
from agenteconomy.chain.transaction import make_coinbase
from agenteconomy.chain.validation import make_genesis, validate_block

GENESIS = make_genesis(PARAMS)


def add_code(pool: Mempool, tx, state=None) -> Code | None:
    try:
        pool.add(tx, state or genesis_state())
    except ChainError as e:
        return e.code
    return None


def pool_with(*txs) -> Mempool:
    pool = Mempool(PARAMS)
    for tx in txs:
        pool.add(tx, genesis_state())
    return pool


def test_valid_transaction_is_admitted():
    tx = pay()
    pool = pool_with(tx)
    assert tx in pool
    assert len(pool) == 1


def test_duplicate_is_refused():
    tx = pay()
    assert add_code(pool_with(tx), tx) == Code.DUPLICATE_TX


def test_conflicting_nonce_is_refused_and_first_seen_stays():
    first, second = pay(to=BOB, nonce=0), pay(to=CAROL, nonce=0, fee=100)
    pool = pool_with(first)
    assert add_code(pool, second) == Code.NONCE_CONFLICT
    assert first in pool and second not in pool


@pytest.mark.parametrize("tx, code", [
    (replace(pay(), fee=5), Code.BAD_SIGNATURE),
    (pay(chain_id="other-chain"), Code.WRONG_CHAIN),
    (pay(fee=0), Code.FEE_TOO_LOW),
    (pay(ALICE, ALICE), Code.SELF_TRANSFER),
    (pay(amount=0), Code.INVALID_AMOUNT),
    (pay(amount=5000), Code.INSUFFICIENT_BALANCE),
    (make_coinbase(PARAMS, to=ALICE.public_hex, height=1), Code.UNEXPECTED_KIND),
])
def test_invalid_transactions_are_refused(tx, code):
    assert add_code(Mempool(PARAMS), tx) == code


def test_old_nonce_is_refused():
    block = mined_block(GENESIS.header, [pay(nonce=0)])
    state = apply_block(genesis_state(), block, PARAMS)
    assert add_code(Mempool(PARAMS), pay(amount=11, nonce=0), state) == Code.NONCE_TOO_LOW


def test_waiting_spends_add_up_against_the_balance():
    pool = pool_with(pay(amount=600, nonce=0))
    assert add_code(pool, pay(amount=400, nonce=1)) == Code.INSUFFICIENT_BALANCE
    assert add_code(pool, pay(amount=398, nonce=1)) is None


def test_sender_limit():
    pool = Mempool(PARAMS, max_per_sender=2)
    assert add_code(pool, pay(nonce=0)) is None
    assert add_code(pool, pay(nonce=1)) is None
    assert add_code(pool, pay(nonce=2)) == Code.SENDER_LIMIT
    assert add_code(pool, pay(BOB, ALICE, nonce=0)) is None


def test_pool_size_limit():
    pool = Mempool(PARAMS, max_size=1)
    assert add_code(pool, pay(nonce=0)) is None
    assert add_code(pool, pay(BOB, ALICE, nonce=0)) == Code.MEMPOOL_FULL


def test_refused_transaction_leaves_the_pool_unchanged():
    pool = Mempool(PARAMS)
    add_code(pool, pay(amount=5000))
    assert len(pool) == 0
    assert pool.select(genesis_state(), 10) == []


def test_selection_orders_by_fee():
    low, high = pay(ALICE, CAROL, fee=1), pay(BOB, CAROL, fee=5)
    assert pool_with(low, high).select(genesis_state(), 10) == [high, low]


def test_equal_fees_are_ordered_by_id():
    first, second = sorted([pay(ALICE, CAROL), pay(BOB, CAROL)], key=lambda tx: tx.id)
    assert pool_with(second, first).select(genesis_state(), 10) == [first, second]


def test_selection_keeps_each_senders_nonces_in_order_even_if_a_later_one_pays_more():
    cheap_first, rich_second = pay(nonce=0, fee=1), pay(nonce=1, fee=99)
    other = pay(BOB, CAROL, fee=50)
    assert pool_with(rich_second, cheap_first, other).select(genesis_state(), 10) == [other, cheap_first, rich_second]


def test_selection_ignores_arrival_order():
    txs = [pay(nonce=0, fee=3), pay(nonce=1, fee=2), pay(BOB, CAROL, fee=2), pay(BOB, CAROL, nonce=1, fee=7)]
    expected = pool_with(*txs).select(genesis_state(), 10)
    assert pool_with(*reversed(txs)).select(genesis_state(), 10) == expected


def test_selection_skips_a_nonce_gap():
    assert pool_with(pay(nonce=1)).select(genesis_state(), 10) == []
    held = pay(nonce=1)
    filler = pay(nonce=0)
    assert pool_with(held, filler).select(genesis_state(), 10) == [filler, held]


def test_selection_respects_the_limit():
    assert len(pool_with(pay(nonce=0), pay(nonce=1), pay(nonce=2)).select(genesis_state(), 2)) == 2


def test_selection_does_not_change_the_state_or_the_pool():
    pool = pool_with(pay())
    state = genesis_state()
    before = state.copy()
    pool.select(state, 10)
    assert state == before
    assert len(pool) == 1


def test_selection_skips_what_a_changed_state_can_no_longer_pay():
    pool = pool_with(pay(amount=900))
    poorer = genesis_state()
    poorer.balances[ALICE.public_hex] = 100
    assert pool.select(poorer, 10) == []


def test_selected_transactions_make_a_valid_block():
    pool = pool_with(pay(nonce=0, fee=2), pay(nonce=1, fee=9), pay(BOB, CAROL, fee=4))
    txs = pool.select(genesis_state(), PARAMS.max_block_txs - 1)
    block = mined_block(GENESIS.header, txs)
    validate_block(block, GENESIS.header, genesis_state(), PARAMS)


def test_new_tip_removes_included_transactions():
    included, waiting = pay(nonce=0), pay(BOB, CAROL)
    pool = pool_with(included, waiting)
    block = mined_block(GENESIS.header, [included])
    pool.on_new_tip(block, apply_block(genesis_state(), block, PARAMS))
    assert included not in pool
    assert waiting in pool


def test_new_tip_drops_transactions_it_made_invalid():
    # The same nonce was mined from a competing transaction this node never saw.
    mine_view = pay(to=BOB, nonce=0)
    pool = pool_with(mine_view)
    block = mined_block(GENESIS.header, [pay(to=CAROL, nonce=0)])
    pool.on_new_tip(block, apply_block(genesis_state(), block, PARAMS))
    assert len(pool) == 0


def test_new_tip_drops_what_can_no_longer_be_afforded():
    keeps, loses = pay(amount=300, nonce=1), pay(amount=300, nonce=2)
    pool = pool_with(pay(amount=10, nonce=0), keeps, loses)
    state = genesis_state()
    state.balances[ALICE.public_hex] = 350
    state.nonces[ALICE.public_hex] = 1
    pool.prune(state)
    assert keeps in pool and loses not in pool


def test_reorg_returns_transactions_to_the_pool():
    tx = pay()
    block = mined_block(GENESIS.header, [tx])
    pool = pool_with(tx)
    pool.on_new_tip(block, apply_block(genesis_state(), block, PARAMS))
    assert tx not in pool

    refused = pool.readd_after_reorg(list(block.txs), genesis_state())
    assert refused == {}
    assert tx in pool


def test_reorg_drops_transactions_invalid_on_the_new_chain():
    abandoned = pay(to=BOB, nonce=0)
    winning = mined_block(GENESIS.header, [pay(to=CAROL, nonce=0)])
    pool = Mempool(PARAMS)
    refused = pool.readd_after_reorg([abandoned], apply_block(genesis_state(), winning, PARAMS))
    assert refused == {abandoned.id: Code.NONCE_TOO_LOW}
    assert len(pool) == 0


def test_reorg_never_readds_a_coinbase():
    block = mined_block(GENESIS.header)
    pool = Mempool(PARAMS)
    assert pool.readd_after_reorg(list(block.txs), genesis_state()) == {}
    assert len(pool) == 0


def test_reorg_handles_nonces_in_any_order():
    second, first = pay(nonce=1), pay(nonce=0)
    pool = Mempool(PARAMS)
    assert pool.readd_after_reorg([second, first], genesis_state()) == {}
    assert pool.select(genesis_state(), 10) == [first, second]
