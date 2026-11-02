"""Blocks and proof of work."""

import hashlib
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Any

from .canonical import BLOCK_TAG, MAX_SAFE_INT, canonical_json, tagged_bytes, tagged_hash
from .errors import ChainError, Code
from .merkle import merkle_root
from .transaction import Transaction, is_hash_hex, is_int

BLOCK_VERSION = 1
GENESIS_PREV_HASH = "00" * 32

_HEADER_FIELDS = {"version", "height", "prev_hash", "merkle_root", "timestamp", "difficulty_bits", "nonce"}


@dataclass(frozen=True)
class BlockHeader:
    version: int
    height: int
    prev_hash: str
    merkle_root: str
    timestamp: int
    difficulty_bits: int
    nonce: int

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "height": self.height, "prev_hash": self.prev_hash,
                "merkle_root": self.merkle_root, "timestamp": self.timestamp,
                "difficulty_bits": self.difficulty_bits, "nonce": self.nonce}

    @classmethod
    def from_dict(cls, data: Any) -> BlockHeader:
        if not isinstance(data, dict) or set(data) != _HEADER_FIELDS:
            raise ChainError(Code.MALFORMED, "header fields don't match the format")
        header = cls(**data)
        header.check_shape()
        return header

    @cached_property
    def hash(self) -> bytes:
        return tagged_hash(BLOCK_TAG, canonical_json(self.to_dict()))

    @property
    def hash_hex(self) -> str:
        return self.hash.hex()

    def check_shape(self) -> None:
        if type(self.version) is not int:
            raise ChainError(Code.MALFORMED, "version must be an integer")
        if self.version != BLOCK_VERSION:
            raise ChainError(Code.UNSUPPORTED_VERSION, f"block version {self.version}")
        if not is_hash_hex(self.prev_hash) or not is_hash_hex(self.merkle_root):
            raise ChainError(Code.MALFORMED, "prev_hash and merkle_root must be lowercase hex")
        for value in (self.height, self.timestamp, self.nonce):
            if not is_int(value) or value < 0:
                raise ChainError(Code.MALFORMED, "height, timestamp and nonce must be non-negative integers")
        if not is_int(self.difficulty_bits) or not 0 <= self.difficulty_bits <= 255:
            raise ChainError(Code.MALFORMED, "difficulty_bits must be 0 to 255")


@dataclass(frozen=True)
class Block:
    header: BlockHeader
    txs: tuple[Transaction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"header": self.header.to_dict(), "txs": [tx.to_dict() for tx in self.txs]}

    @classmethod
    def from_dict(cls, data: Any) -> Block:
        if not isinstance(data, dict) or set(data) != {"header", "txs"} or not isinstance(data["txs"], list):
            raise ChainError(Code.MALFORMED, "block fields don't match the format")
        return cls(BlockHeader.from_dict(data["header"]), tuple(Transaction.from_dict(tx) for tx in data["txs"]))


def compute_merkle_root(txs: tuple[Transaction, ...] | list[Transaction]) -> str:
    return merkle_root([tx.id for tx in txs]).hex()


def meets_target(block_hash: bytes, difficulty_bits: int) -> bool:
    """True if the hash starts with at least `difficulty_bits` zero bits."""
    return int.from_bytes(block_hash, "big") >> (256 - difficulty_bits) == 0


def block_work(difficulty_bits: int) -> int:
    """Expected number of hashes to find this block. Consensus adds these up. Never exported, so it can be large."""
    return 2**difficulty_bits


def mine(header: BlockHeader, *, max_attempts: int) -> BlockHeader | None:
    """Try nonces from header.nonce onward. Returns the mined header, or None if the attempts run out.

    Hashes the bytes around the nonce only once, about 20 times faster than re-encoding every attempt.
    A test checks the result against BlockHeader.hash.
    """
    before, after = canonical_json(replace(header, nonce=0).to_dict()).split(b'"nonce":0')
    prefix = hashlib.sha256(tagged_bytes(BLOCK_TAG, before + b'"nonce":'))
    shift = 256 - header.difficulty_bits

    last = min(header.nonce + max_attempts, MAX_SAFE_INT + 1)
    for nonce in range(header.nonce, last):
        attempt = prefix.copy()
        attempt.update(b"%d" % nonce + after)
        if int.from_bytes(attempt.digest(), "big") >> shift == 0:
            return replace(header, nonce=nonce)
    return None
