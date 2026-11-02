from dataclasses import replace

import pytest
from chain_helpers import ALICE, BOB, CAROL, PARAMS, genesis_state, mined_block, pay

from agenteconomy.chain.block import Block, compute_merkle_root, mine
from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.params import ChainParams
from agenteconomy.chain.state import State, apply_block, apply_tx
from agenteconomy.chain.transaction import make_coinbase, make_genesis_tx
from agenteconomy.chain.validation import build_block, cumulative_work, make_genesis, validate_block, validate_chain

GENESIS = make_genesis(PARAMS)


def code_of_block(block: Block, parent=GENESIS.header, state=None) -> Code | None:
    try:
        validate_block(block, parent, state or genesis_state(), PARAMS)
    except ChainError as e:
        return e.code
    return None


def rebuilt(block: Block, txs=None, **header_changes) -> Block:
    """Keep the Merkle root and proof of work right, so only the intended fault remains."""
    txs = block.txs if txs is None else tuple(txs)
    header = replace(block.header, merkle_root=compute_merkle_root(txs), nonce=0, **header_changes)
    return Block(mine(header, max_attempts=1 << 20), txs)


def test_genesis_funds_the_allocations():
    state = genesis_state()
    assert state.balance(ALICE.public_hex) == 1000
    assert state.balance(BOB.public_hex) == 500
    assert state.minted == 1500


def test_genesis_is_deterministic():
    assert make_genesis(PARAMS).header.hash == GENESIS.header.hash


def test_params_refuse_bad_genesis():
    with pytest.raises(ValueError):
        ChainParams("c", 4, 50, genesis_allocations=())
    with pytest.raises(ValueError):
        ChainParams("c", 4, 50, genesis_allocations=(("a", 1), ("a", 2)))


def test_valid_block_applies_transfer_fee_and_reward():
    block = mined_block(GENESIS.header, [pay(amount=100, fee=3)])
    state = validate_block(block, GENESIS.header, genesis_state(), PARAMS)
    assert state.balance(ALICE.public_hex) == 897
    assert state.balance(BOB.public_hex) == 600
    assert state.balance(CAROL.public_hex) == 50
    assert state.nonce(ALICE.public_hex) == 1
    assert state.burned == 3  # the fee goes to nobody, including the miner
    assert state.supply == 1547


def test_block_with_only_a_coinbase_is_valid():
    assert code_of_block(mined_block(GENESIS.header)) is None


def test_validating_does_not_change_the_old_state():
    state = genesis_state()
    before = state.copy()
    validate_block(mined_block(GENESIS.header, [pay()]), GENESIS.header, state, PARAMS)
    assert state == before


def test_failed_block_leaves_no_partial_effect():
    state = genesis_state()
    before = state.copy()
    block = mined_block(GENESIS.header, [pay(nonce=0), pay(nonce=5)])
    with pytest.raises(ChainError):
        validate_block(block, GENESIS.header, state, PARAMS)
    assert state == before


@pytest.mark.parametrize("changes, code", [
    ({"version": 2}, Code.UNSUPPORTED_VERSION),
    ({"height": 2}, Code.BAD_HEIGHT),
    ({"prev_hash": "ee" * 32}, Code.BAD_PREV_HASH),
    ({"difficulty_bits": 0}, Code.BAD_DIFFICULTY),
    ({"difficulty_bits": 5}, Code.BAD_DIFFICULTY),
    ({"nonce": 123456}, Code.BAD_POW),
    ({"timestamp": 999}, Code.BAD_POW),
    ({"merkle_root": "ee" * 32}, Code.BAD_POW),
])
def test_changing_any_header_field_is_caught(changes, code):
    block = mined_block(GENESIS.header, [pay()])
    assert code_of_block(Block(replace(block.header, **changes), block.txs)) == code


def test_unmined_block_is_rejected():
    params = replace(PARAMS, difficulty_bits=30)
    block = build_block(params, GENESIS.header, miner=CAROL.public_hex, txs=[], timestamp=1)
    with pytest.raises(ChainError) as e:
        validate_block(block, GENESIS.header, genesis_state(), params)
    assert e.value.code == Code.BAD_POW


def test_timestamp_equal_to_parent_is_fine_but_earlier_is_not():
    parent = mined_block(GENESIS.header, timestamp=50)
    state = validate_block(parent, GENESIS.header, genesis_state(), PARAMS)
    assert code_of_block(mined_block(parent.header, timestamp=50), parent.header, state) is None
    assert code_of_block(mined_block(parent.header, timestamp=49), parent.header, state) == Code.BAD_TIMESTAMP


def test_too_many_transactions():
    txs = [pay(nonce=n) for n in range(PARAMS.max_block_txs)]  # plus the coinbase is one too many
    assert code_of_block(mined_block(GENESIS.header, txs)) == Code.TOO_MANY_TXS
    assert code_of_block(mined_block(GENESIS.header, txs[:-1])) is None


def test_transaction_swapped_without_updating_merkle_root():
    block = mined_block(GENESIS.header, [pay(amount=10)])
    swapped = Block(block.header, (block.txs[0], pay(amount=11)))
    assert code_of_block(swapped) == Code.BAD_MERKLE_ROOT


def test_reordered_transactions_break_the_merkle_root():
    block = mined_block(GENESIS.header, [pay(nonce=0), pay(nonce=1)])
    reordered = Block(block.header, (block.txs[0], block.txs[2], block.txs[1]))
    assert code_of_block(reordered) == Code.BAD_MERKLE_ROOT


def test_no_transactions_at_all():
    block = mined_block(GENESIS.header)
    assert code_of_block(Block(block.header, ())) == Code.BAD_COINBASE


def test_no_coinbase():
    block = mined_block(GENESIS.header)
    assert code_of_block(rebuilt(block, [pay()])) == Code.BAD_COINBASE


def test_first_transaction_of_another_kind_paying_exactly_the_reward():
    # The amount check would catch most of these; this one only the kind check can.
    block = mined_block(GENESIS.header)
    transfer = pay(amount=PARAMS.block_reward)
    mint = make_genesis_tx(PARAMS, to=CAROL.public_hex, amount=PARAMS.block_reward)
    assert code_of_block(rebuilt(block, [transfer])) == Code.BAD_COINBASE
    assert code_of_block(rebuilt(block, [mint])) == Code.BAD_COINBASE


def test_coinbase_not_first():
    block = mined_block(GENESIS.header, [pay()])
    assert code_of_block(rebuilt(block, [block.txs[1], block.txs[0]])) == Code.BAD_COINBASE


@pytest.mark.parametrize("amount", [51, 49, 10**9])
def test_wrong_reward(amount):
    block = mined_block(GENESIS.header)
    coinbase = replace(block.txs[0], body={**block.txs[0].body, "amount": amount})
    assert code_of_block(rebuilt(block, [coinbase])) == Code.BAD_COINBASE


def test_coinbase_for_the_wrong_height():
    block = mined_block(GENESIS.header)
    assert code_of_block(rebuilt(block, [make_coinbase(PARAMS, to=CAROL.public_hex, height=2)])) == Code.BAD_COINBASE


def test_second_coinbase():
    block = mined_block(GENESIS.header)
    extra = make_coinbase(PARAMS, to=ALICE.public_hex, height=1)
    assert code_of_block(rebuilt(block, [block.txs[0], extra])) == Code.BAD_COINBASE


def test_genesis_style_mint_in_a_later_block():
    block = mined_block(GENESIS.header)
    mint = make_genesis_tx(PARAMS, to=ALICE.public_hex, amount=1_000_000)
    assert code_of_block(rebuilt(block, [block.txs[0], mint])) == Code.UNEXPECTED_KIND


def test_duplicate_transaction():
    tx = pay()
    assert code_of_block(mined_block(GENESIS.header, [tx, tx])) == Code.DUPLICATE_TX


def test_double_spend_within_a_block():
    block = mined_block(GENESIS.header, [pay(to=BOB, nonce=0), pay(to=CAROL, nonce=0)])
    assert code_of_block(block) == Code.BAD_NONCE


def test_double_spend_across_blocks():
    first = mined_block(GENESIS.header, [pay(to=BOB, nonce=0)])
    state = validate_block(first, GENESIS.header, genesis_state(), PARAMS)
    second = mined_block(first.header, [pay(to=CAROL, nonce=0)])
    assert code_of_block(second, first.header, state) == Code.BAD_NONCE


def test_replay_of_the_identical_transaction():
    tx = pay()
    first = mined_block(GENESIS.header, [tx])
    state = validate_block(first, GENESIS.header, genesis_state(), PARAMS)
    assert code_of_block(mined_block(first.header, [tx]), first.header, state) == Code.BAD_NONCE


def test_skipped_nonce():
    assert code_of_block(mined_block(GENESIS.header, [pay(nonce=1)])) == Code.BAD_NONCE


def test_overspending():
    assert code_of_block(mined_block(GENESIS.header, [pay(amount=1001)])) == Code.INSUFFICIENT_BALANCE


def test_balance_must_cover_the_fee_too():
    assert code_of_block(mined_block(GENESIS.header, [pay(amount=1000, fee=1)])) == Code.INSUFFICIENT_BALANCE
    assert code_of_block(mined_block(GENESIS.header, [pay(amount=999, fee=1)])) is None


def test_spending_everything_over_two_transactions_then_one_more():
    block = mined_block(GENESIS.header, [pay(amount=500, nonce=0), pay(amount=498, nonce=1), pay(amount=1, nonce=2)])
    assert code_of_block(block) == Code.INSUFFICIENT_BALANCE


def test_coins_received_earlier_in_a_block_can_be_spent_later_in_it():
    block = mined_block(GENESIS.header, [pay(ALICE, CAROL, 100, nonce=0), pay(CAROL, BOB, 99, nonce=0)])
    assert code_of_block(block) is None


def test_miner_can_spend_reward_only_after_the_coinbase_applies():
    block = mined_block(GENESIS.header, [pay(CAROL, BOB, 49, nonce=0)])
    assert code_of_block(block) is None  # the coinbase comes first in the block
    too_much = mined_block(GENESIS.header, [pay(CAROL, BOB, 50, nonce=0)])
    assert code_of_block(too_much) == Code.INSUFFICIENT_BALANCE


def test_invalid_transactions_inside_a_block_are_caught():
    block = mined_block(GENESIS.header, [pay(chain_id="other-chain")])
    assert code_of_block(block) == Code.WRONG_CHAIN
    tx = pay()
    assert code_of_block(mined_block(GENESIS.header, [replace(tx, fee=5)])) == Code.BAD_SIGNATURE


def test_supply_overflow_is_refused():
    state = State(minted=2**53 - 1)
    before = state.copy()
    with pytest.raises(ChainError) as e:
        apply_tx(state, make_coinbase(PARAMS, to=ALICE.public_hex, height=1), PARAMS)
    assert e.value.code == Code.SUPPLY_OVERFLOW
    assert state == before


def build_chain(length=4) -> list[Block]:
    blocks = [GENESIS]
    for i in range(length):
        blocks.append(mined_block(blocks[-1].header, [pay(amount=10 + i, nonce=i)]))
    return blocks


def test_validate_chain_replays_to_the_final_state():
    blocks = build_chain()
    state = validate_chain(blocks, PARAMS)
    expected = genesis_state()
    for block in blocks[1:]:
        expected = apply_block(expected, block, PARAMS)
    assert state == expected
    assert state.supply == sum(state.balances.values())


def test_genesis_alone_is_a_valid_chain():
    assert validate_chain([GENESIS], PARAMS) == genesis_state()


@pytest.mark.parametrize("blocks", [
    [],
    [Block(GENESIS.header, ())],
    [Block(replace(GENESIS.header, timestamp=99), GENESIS.txs)],
    [Block(GENESIS.header, (make_genesis_tx(PARAMS, to=ALICE.public_hex, amount=10**6), GENESIS.txs[1]))],
])
def test_bad_genesis(blocks):
    with pytest.raises(ChainError) as e:
        validate_chain(blocks, PARAMS)
    assert e.value.code == Code.BAD_GENESIS


def test_genesis_for_another_chain_is_rejected():
    with pytest.raises(ChainError) as e:
        validate_chain(build_chain(1), replace(PARAMS, chain_id="other-chain"))
    assert e.value.code == Code.BAD_GENESIS


def test_blocks_are_judged_by_the_verifiers_reward_not_the_miners():
    with pytest.raises(ChainError) as e:
        validate_chain(build_chain(1), replace(PARAMS, block_reward=51))
    assert e.value.code == Code.BAD_COINBASE


def test_tampering_with_an_old_block_breaks_every_later_link():
    blocks = build_chain()
    blocks[1] = rebuilt(blocks[1], timestamp=blocks[1].header.timestamp + 1)
    with pytest.raises(ChainError) as e:
        validate_chain(blocks, PARAMS)
    assert e.value.code == Code.BAD_PREV_HASH


def test_blocks_out_of_order_or_missing():
    blocks = build_chain()
    for broken in ([blocks[0], blocks[2], blocks[1]], [blocks[0], blocks[2]]):
        with pytest.raises(ChainError) as e:
            validate_chain(broken, PARAMS)
        assert e.value.code == Code.BAD_HEIGHT


def test_cumulative_work():
    headers = [block.header for block in build_chain(3)]
    assert cumulative_work(headers) == 3 * 2**PARAMS.difficulty_bits
    assert cumulative_work(headers[:1]) == 0  # genesis isn't mined


def test_cumulative_work_prefers_difficulty_over_length():
    # What P2's most-work rule depends on: fewer, harder blocks outweigh more, easier ones.
    headers = [block.header for block in build_chain(3)]
    hard = [replace(header, difficulty_bits=8) for header in headers[:2]]
    assert cumulative_work(hard) > cumulative_work(headers)
