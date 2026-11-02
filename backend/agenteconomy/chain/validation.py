"""Block and chain validation: the rules a node applies, whoever sent the data.

The order of checks is part of the format. scripts/verify_vectors.mjs follows the same order and must report the
same rejection code, so change them together.
"""

from collections.abc import Sequence

from .block import (
    BLOCK_VERSION,
    GENESIS_PREV_HASH,
    Block,
    BlockHeader,
    block_work,
    compute_merkle_root,
    meets_target,
)
from .errors import ChainError, Code
from .params import ChainParams
from .state import State, apply_block, apply_tx
from .transaction import COINBASE, USER_KINDS, Transaction, check_transaction, make_coinbase, make_genesis_tx


def make_genesis(params: ChainParams) -> Block:
    txs = tuple(make_genesis_tx(params, to=address, amount=amount) for address, amount in params.genesis_allocations)
    header = BlockHeader(version=BLOCK_VERSION, height=0, prev_hash=GENESIS_PREV_HASH,
                         merkle_root=compute_merkle_root(txs), timestamp=params.genesis_timestamp,
                         difficulty_bits=params.difficulty_bits, nonce=0)
    return Block(header, txs)


def build_block(params: ChainParams, parent: BlockHeader, *, miner: str, txs: Sequence[Transaction],
                timestamp: int) -> Block:
    """An unmined block on top of `parent`: run mine() on its header to finish it."""
    all_txs = (make_coinbase(params, to=miner, height=parent.height + 1), *txs)
    header = BlockHeader(version=BLOCK_VERSION, height=parent.height + 1, prev_hash=parent.hash_hex,
                         merkle_root=compute_merkle_root(all_txs), timestamp=timestamp,
                         difficulty_bits=params.difficulty_bits, nonce=0)
    return Block(header, all_txs)


def validate_block(block: Block, parent: BlockHeader, state: State, params: ChainParams) -> State:
    """Check `block` against its parent and the state after the parent. Returns the new state; raises ChainError."""
    header = block.header
    header.check_shape()
    if header.height != parent.height + 1:
        raise ChainError(Code.BAD_HEIGHT, f"expected {parent.height + 1}, got {header.height}")
    if header.prev_hash != parent.hash_hex:
        raise ChainError(Code.BAD_PREV_HASH)
    if header.difficulty_bits != params.difficulty_bits:
        raise ChainError(Code.BAD_DIFFICULTY, f"expected {params.difficulty_bits}, got {header.difficulty_bits}")
    if not meets_target(header.hash, header.difficulty_bits):
        raise ChainError(Code.BAD_POW)
    if header.timestamp < parent.timestamp:
        raise ChainError(Code.BAD_TIMESTAMP, "earlier than the parent block")
    if len(block.txs) > params.max_block_txs:
        raise ChainError(Code.TOO_MANY_TXS, f"{len(block.txs)} is over {params.max_block_txs}")
    if not block.txs:
        raise ChainError(Code.BAD_COINBASE, "no coinbase")
    if compute_merkle_root(block.txs) != header.merkle_root:
        raise ChainError(Code.BAD_MERKLE_ROOT)

    _check_coinbase(block.txs[0], header, params)
    if len({tx.id for tx in block.txs}) != len(block.txs):
        raise ChainError(Code.DUPLICATE_TX)

    new_state = state.copy()
    apply_tx(new_state, block.txs[0])
    for tx in block.txs[1:]:
        if tx.kind == COINBASE:
            raise ChainError(Code.BAD_COINBASE, "more than one coinbase")
        if tx.kind not in USER_KINDS:
            raise ChainError(Code.UNEXPECTED_KIND, tx.kind)
        check_transaction(tx, params)
        apply_tx(new_state, tx)
    return new_state


def _check_coinbase(tx: Transaction, header: BlockHeader, params: ChainParams) -> None:
    if tx.kind != COINBASE:
        raise ChainError(Code.BAD_COINBASE, "the first transaction must be the coinbase")
    check_transaction(tx, params)
    if tx.body["amount"] != params.block_reward:
        raise ChainError(Code.BAD_COINBASE, f"pays {tx.body['amount']}, reward is {params.block_reward}")
    if tx.body["height"] != header.height:
        raise ChainError(Code.BAD_COINBASE, "height doesn't match the block")


def validate_chain(blocks: Sequence[Block], params: ChainParams) -> State:
    """Replay a whole chain from genesis. Returns the final state; raises ChainError at the first problem."""
    if not blocks or blocks[0].to_dict() != make_genesis(params).to_dict():
        raise ChainError(Code.BAD_GENESIS)

    state = apply_block(State(), blocks[0])
    for parent, block in zip(blocks, blocks[1:], strict=False):
        state = validate_block(block, parent.header, state, params)
    return state


def cumulative_work(headers: Sequence[BlockHeader]) -> int:
    """Total proof of work in a chain. Genesis isn't mined and every chain shares it, so it doesn't count."""
    return sum(block_work(header.difficulty_bits) for header in headers if header.height > 0)
