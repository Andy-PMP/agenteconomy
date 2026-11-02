"""What nodes send each other. Everything crosses the network as bytes and is strictly parsed on arrival: nodes
share no objects, and a peer can send anything at all."""

import re
from dataclasses import dataclass
from typing import Any

from ..chain.block import Block
from ..chain.canonical import canonical_json, decode_strict
from ..chain.errors import ChainError, Code
from ..chain.transaction import Transaction, is_hash_hex, is_int

MAX_MESSAGE_BYTES = 1 << 24
MAX_LOCATOR_HASHES = 64
MAX_SYNC_BATCH = 100

_WORK = re.compile(r"[0-9]{1,100}")  # total work can pass 2^53, so it travels as a decimal string


@dataclass(frozen=True)
class TxMessage:
    tx: Transaction


@dataclass(frozen=True)
class BlockMessage:
    block: Block


@dataclass(frozen=True)
class BlocksMessage:
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class StatusMessage:
    tip: str
    height: int
    work: int  # a claim: nodes only ever trust work they recomputed from blocks


@dataclass(frozen=True)
class GetBlocksMessage:
    locator: tuple[str, ...]


Message = TxMessage | BlockMessage | BlocksMessage | StatusMessage | GetBlocksMessage


def encode_tx(tx: Transaction) -> bytes:
    return canonical_json({"type": "tx", "tx": tx.to_dict()})


def encode_block(block: Block) -> bytes:
    return canonical_json({"type": "block", "block": block.to_dict()})


def encode_blocks(blocks: list[Block]) -> bytes:
    return canonical_json({"type": "blocks", "blocks": [block.to_dict() for block in blocks]})


def encode_status(tip: str, height: int, work: int) -> bytes:
    return canonical_json({"type": "status", "tip": tip, "height": height, "work": str(work)})


def encode_get_blocks(locator: list[str]) -> bytes:
    return canonical_json({"type": "get_blocks", "locator": locator})


def decode(data: bytes) -> Message:
    """Parse a message from a peer, or raise ChainError."""
    if len(data) > MAX_MESSAGE_BYTES:
        raise ChainError(Code.MALFORMED, "message too large")
    value = decode_strict(data)
    if not isinstance(value, dict):
        raise ChainError(Code.MALFORMED, "message must be an object")

    match value.get("type"):
        case "tx":
            return TxMessage(Transaction.from_dict(_field(value, {"tx"}, "tx")))
        case "block":
            return BlockMessage(Block.from_dict(_field(value, {"block"}, "block")))
        case "blocks":
            return _decode_blocks(_field(value, {"blocks"}, "blocks"))
        case "status":
            return _decode_status(value)
        case "get_blocks":
            return _decode_get_blocks(_field(value, {"locator"}, "locator"))
    raise ChainError(Code.MALFORMED, "unknown message type")


def _field(value: dict[str, Any], fields: set[str], name: str) -> Any:
    if set(value) != fields | {"type"}:
        raise ChainError(Code.MALFORMED, "message fields don't match its type")
    return value[name]


def _decode_blocks(blocks: Any) -> BlocksMessage:
    if not isinstance(blocks, list) or len(blocks) > MAX_SYNC_BATCH:
        raise ChainError(Code.MALFORMED, "blocks must be a list within the batch limit")
    return BlocksMessage(tuple(Block.from_dict(block) for block in blocks))


def _decode_status(value: dict[str, Any]) -> StatusMessage:
    if set(value) != {"type", "tip", "height", "work"}:
        raise ChainError(Code.MALFORMED, "message fields don't match its type")
    tip, height, work = value["tip"], value["height"], value["work"]
    if not is_hash_hex(tip) or not is_int(height) or height < 0:
        raise ChainError(Code.MALFORMED, "bad tip or height")
    if not isinstance(work, str) or not _WORK.fullmatch(work):
        raise ChainError(Code.MALFORMED, "bad work")
    return StatusMessage(tip, height, int(work))


def _decode_get_blocks(locator: Any) -> GetBlocksMessage:
    if not isinstance(locator, list) or len(locator) > MAX_LOCATOR_HASHES:
        raise ChainError(Code.MALFORMED, "locator must be a list within the limit")
    if not all(is_hash_hex(item) for item in locator):
        raise ChainError(Code.MALFORMED, "locator entries must be block hashes")
    return GetBlocksMessage(tuple(locator))
