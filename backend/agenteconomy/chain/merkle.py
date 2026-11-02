"""Merkle tree over transaction IDs, RFC 6962 style.

Leaves and inner nodes hash with different prefixes, so an inner node can't pass as a leaf. An odd node moves up
unchanged. Bitcoin duplicates it instead, which lets two different lists share a root (CVE-2012-2459).
"""

import hashlib
from typing import Literal

Side = Literal["left", "right"]


def _leaf(item: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + item).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def merkle_root(items: list[bytes]) -> bytes:
    if not items:
        raise ValueError("a Merkle tree needs at least one item")
    level = [_leaf(item) for item in items]
    while len(level) > 1:
        level = _next_level(level)
    return level[0]


def _next_level(level: list[bytes]) -> list[bytes]:
    parents = [_node(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
    if len(level) % 2:
        parents.append(level[-1])
    return parents


def merkle_proof(items: list[bytes], index: int) -> list[tuple[Side, bytes]]:
    """The sibling hashes from one item up to the root, each with the side it sits on."""
    if not 0 <= index < len(items):
        raise IndexError(index)
    proof: list[tuple[Side, bytes]] = []
    level = [_leaf(item) for item in items]
    while len(level) > 1:
        sibling = index ^ 1
        if sibling < len(level):  # a promoted node has no sibling at this level
            proof.append(("left" if sibling < index else "right", level[sibling]))
        level = _next_level(level)
        index //= 2
    return proof


def verify_proof(item: bytes, proof: list[tuple[Side, bytes]], root: bytes) -> bool:
    current = _leaf(item)
    for side, sibling in proof:
        current = _node(sibling, current) if side == "left" else _node(current, sibling)
    return current == root
