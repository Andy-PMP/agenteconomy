"""Shared setup: three funded-or-not accounts and a low difficulty, so tests mine instantly."""

from agenteconomy.chain.block import Block, BlockHeader, mine
from agenteconomy.chain.params import ChainParams
from agenteconomy.chain.state import State, apply_block
from agenteconomy.chain.transaction import Transaction, sign_transfer
from agenteconomy.chain.validation import build_block, make_genesis
from agenteconomy.chain.vectors import sample_keys

ALICE = sample_keys("alice")
BOB = sample_keys("bob")
CAROL = sample_keys("carol")

PARAMS = ChainParams(
    chain_id="test-chain", difficulty_bits=4, block_reward=50, min_fee=1, max_block_txs=10,
    genesis_allocations=((ALICE.public_hex, 1000), (BOB.public_hex, 500)),
)


def genesis_state() -> State:
    return apply_block(State(), make_genesis(PARAMS), PARAMS)


def pay(sender=ALICE, to=BOB, amount=10, *, nonce=0, fee=1, chain_id=PARAMS.chain_id) -> Transaction:
    return sign_transfer(sender, chain_id, to=to.public_hex, amount=amount, nonce=nonce, fee=fee)


def mined_block(parent: BlockHeader, txs=(), *, timestamp=None, miner=CAROL, params=PARAMS) -> Block:
    timestamp = parent.timestamp + 10 if timestamp is None else timestamp
    block = build_block(params, parent, miner=miner.public_hex, txs=list(txs), timestamp=timestamp)
    header = mine(block.header, max_attempts=1 << 20)
    assert header is not None
    return Block(header, block.txs)
