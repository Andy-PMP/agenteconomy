"""Transactions. A `kind` plus a strictly checked `body`, so P3 can add kinds without changing the format.

Bodies accept only the listed fields, and strings must be hex or known values. That is what keeps free text off
the chain: there is nowhere to put it.
"""

import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from .canonical import MAX_SAFE_INT, TX_SIGN_TAG, TX_TAG, canonical_json, tagged_bytes, tagged_hash
from .errors import ChainError, Code
from .keys import KeyPair, is_usable_public_key, verify
from .params import ChainParams

TX_VERSION = 1
TRANSFER = "transfer"
COINBASE = "coinbase"
GENESIS = "genesis"
OFFER = "offer"
ACCEPT = "accept"
REJECT = "reject"

# Each kind's exact body fields. Kinds not in USER_KINDS are minted by the protocol: no sender, no signature.
_BODY_FIELDS = {
    TRANSFER: {"to", "amount"},
    COINBASE: {"to", "amount", "height"},
    GENESIS: {"to", "amount"},
    OFFER: {"to", "give_coins", "give_creatures", "want_coins", "want_creatures", "expires_at", "counter_of"},
    ACCEPT: {"offer_id"},
    REJECT: {"offer_id"},
}
USER_KINDS = frozenset({TRANSFER, OFFER, ACCEPT, REJECT})

_FIELDS = {"version", "chain_id", "kind", "sender", "nonce", "fee", "body", "signature"}
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_HEX_128 = re.compile(r"[0-9a-f]{128}")
_CHAIN_ID = re.compile(r"[a-z0-9-]{1,32}")


def is_int(value: Any) -> bool:
    return type(value) is int and abs(value) <= MAX_SAFE_INT


def is_hash_hex(value: Any) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _is_count(value: Any) -> bool:
    return is_int(value) and value >= 0


def _is_signature_hex(value: Any) -> bool:
    return isinstance(value, str) and _HEX_128.fullmatch(value) is not None


@dataclass(frozen=True)
class Transaction:
    version: int
    chain_id: str
    kind: str
    sender: str | None
    nonce: int
    fee: int
    body: dict[str, Any]
    signature: str | None

    def to_dict(self, *, signed: bool = True) -> dict[str, Any]:
        result = {"version": self.version, "chain_id": self.chain_id, "kind": self.kind, "sender": self.sender,
                  "nonce": self.nonce, "fee": self.fee, "body": dict(self.body)}
        if signed:
            result["signature"] = self.signature
        return result

    @classmethod
    def from_dict(cls, data: Any) -> Transaction:
        if not isinstance(data, dict) or set(data) != _FIELDS:
            raise ChainError(Code.MALFORMED, "transaction fields don't match the format")
        tx = cls(**data)
        tx.check_shape()
        return tx

    @cached_property
    def signing_bytes(self) -> bytes:
        return tagged_bytes(TX_SIGN_TAG, canonical_json(self.to_dict(signed=False)))

    @cached_property
    def id(self) -> bytes:
        """Covers the signature too, so the Merkle root commits to exactly what was authorized."""
        return tagged_hash(TX_TAG, canonical_json(self.to_dict()))

    @property
    def spend(self) -> int:
        """What the sender's balance must cover before any later checks."""
        return self.body.get("amount", 0) + self.fee

    def check_shape(self) -> None:
        """Types and format only, needing no chain context."""
        if type(self.version) is not int:
            raise ChainError(Code.MALFORMED, "version must be an integer")
        if self.version != TX_VERSION:
            raise ChainError(Code.UNSUPPORTED_VERSION, f"transaction version {self.version}")
        if not isinstance(self.chain_id, str) or not _CHAIN_ID.fullmatch(self.chain_id):
            raise ChainError(Code.MALFORMED, "bad chain_id")
        if not isinstance(self.kind, str):
            raise ChainError(Code.MALFORMED, "kind must be a string")
        if self.kind not in _BODY_FIELDS:
            raise ChainError(Code.UNKNOWN_KIND, self.kind)
        if not _is_count(self.nonce) or not _is_count(self.fee):
            raise ChainError(Code.MALFORMED, "nonce and fee must be non-negative integers")
        self._check_body_shape()

        if self.kind in USER_KINDS:
            if not is_hash_hex(self.sender) or not _is_signature_hex(self.signature):
                raise ChainError(Code.MALFORMED, "sender and signature must be lowercase hex of the right length")
        elif self.sender is not None or self.signature is not None or self.nonce != 0 or self.fee != 0:
            raise ChainError(Code.MALFORMED, f"{self.kind} has no sender, signature, nonce or fee")

    def _check_body_shape(self) -> None:
        body = self.body
        if not isinstance(body, dict) or set(body) != _BODY_FIELDS[self.kind]:
            raise ChainError(Code.MALFORMED, f"body fields don't match {self.kind}")
        if "to" in body and not is_hash_hex(body["to"]):
            raise ChainError(Code.MALFORMED, "to must be a lowercase hex address")
        if "amount" in body and not is_int(body["amount"]):
            raise ChainError(Code.MALFORMED, "amount must be an integer")
        if "height" in body and not _is_count(body["height"]):
            raise ChainError(Code.MALFORMED, "height must be a non-negative integer")
        if "offer_id" in body and not is_hash_hex(body["offer_id"]):
            raise ChainError(Code.MALFORMED, "offer_id must be a transaction ID")
        if self.kind == OFFER:
            self._check_offer_shape()

    def _check_offer_shape(self) -> None:
        body = self.body
        if not all(_is_count(body[field]) for field in ("give_coins", "want_coins", "expires_at")):
            raise ChainError(Code.MALFORMED, "coin amounts and expires_at must be non-negative integers")
        for field in ("give_creatures", "want_creatures"):
            if not isinstance(body[field], list) or not all(is_hash_hex(item) for item in body[field]):
                raise ChainError(Code.MALFORMED, f"{field} must be a list of creature IDs")
        if body["counter_of"] is not None and not is_hash_hex(body["counter_of"]):
            raise ChainError(Code.MALFORMED, "counter_of must be a transaction ID or null")


def check_transaction(tx: Transaction, params: ChainParams) -> None:
    """Everything that can be checked without account state, including the signature. Raises ChainError."""
    tx.check_shape()
    if tx.chain_id != params.chain_id:
        raise ChainError(Code.WRONG_CHAIN, tx.chain_id)
    if "to" in tx.body and not is_usable_public_key(tx.body["to"]):
        raise ChainError(Code.INVALID_PUBLIC_KEY, "recipient")
    if tx.kind in USER_KINDS and not is_usable_public_key(tx.sender):
        raise ChainError(Code.INVALID_PUBLIC_KEY, "sender")
    # A coinbase can be 0 once supply reaches the cap; whether it's the right amount is checked with the block.
    if "amount" in tx.body and tx.body["amount"] < (0 if tx.kind == COINBASE else 1):
        raise ChainError(Code.INVALID_AMOUNT, str(tx.body["amount"]))
    if tx.kind in USER_KINDS:
        if tx.sender == tx.body.get("to"):
            raise ChainError(Code.SELF_TRANSFER)
        if tx.kind == OFFER:
            _check_offer_terms(tx.body, params)
        if tx.fee < params.min_fee:
            raise ChainError(Code.FEE_TOO_LOW, f"{tx.fee} is under {params.min_fee}")
        if not verify(tx.sender, tx.signature, tx.signing_bytes):
            raise ChainError(Code.BAD_SIGNATURE)


def _check_offer_terms(body: dict[str, Any], params: ChainParams) -> None:
    gives, wants = body["give_creatures"], body["want_creatures"]
    if body["give_coins"] and body["want_coins"]:
        raise ChainError(Code.INVALID_OFFER, "coins can only move one way")
    if not (body["give_coins"] or body["want_coins"] or gives or wants):
        raise ChainError(Code.INVALID_OFFER, "the offer exchanges nothing")
    for creatures in (gives, wants):
        if len(creatures) > params.max_offer_creatures:
            raise ChainError(Code.INVALID_OFFER, "too many creatures")
        # One spelling per set of creatures, so a list can't be reordered into a different transaction.
        if creatures != sorted(set(creatures)):
            raise ChainError(Code.INVALID_OFFER, "creature IDs must be sorted with no repeats")
    if set(gives) & set(wants):
        raise ChainError(Code.INVALID_OFFER, "the same creature is on both sides")


def sign_tx(keys: KeyPair, chain_id: str, kind: str, body: dict[str, Any], *, nonce: int, fee: int) -> Transaction:
    unsigned = Transaction(version=TX_VERSION, chain_id=chain_id, kind=kind, sender=keys.public_hex, nonce=nonce,
                           fee=fee, body=body, signature=None)
    return Transaction(**{**unsigned.to_dict(), "signature": keys.sign(unsigned.signing_bytes)})


def sign_transfer(keys: KeyPair, chain_id: str, *, to: str, amount: int, nonce: int, fee: int) -> Transaction:
    return sign_tx(keys, chain_id, TRANSFER, {"to": to, "amount": amount}, nonce=nonce, fee=fee)


def sign_offer(keys: KeyPair, chain_id: str, *, to: str, give_coins: int = 0, give_creatures: tuple[str, ...] = (),
               want_coins: int = 0, want_creatures: tuple[str, ...] = (), expires_at: int,
               counter_of: str | None = None, nonce: int, fee: int) -> Transaction:
    body = {"to": to, "give_coins": give_coins, "give_creatures": list(give_creatures), "want_coins": want_coins,
            "want_creatures": list(want_creatures), "expires_at": expires_at, "counter_of": counter_of}
    return sign_tx(keys, chain_id, OFFER, body, nonce=nonce, fee=fee)


def sign_accept(keys: KeyPair, chain_id: str, *, offer_id: str, nonce: int, fee: int) -> Transaction:
    return sign_tx(keys, chain_id, ACCEPT, {"offer_id": offer_id}, nonce=nonce, fee=fee)


def sign_reject(keys: KeyPair, chain_id: str, *, offer_id: str, nonce: int, fee: int) -> Transaction:
    return sign_tx(keys, chain_id, REJECT, {"offer_id": offer_id}, nonce=nonce, fee=fee)


def make_coinbase(params: ChainParams, *, to: str, height: int, amount: int | None = None) -> Transaction:
    # The height makes every coinbase unique, so two blocks paying the same miner don't share a transaction ID.
    amount = params.block_reward if amount is None else amount
    return Transaction(version=TX_VERSION, chain_id=params.chain_id, kind=COINBASE, sender=None, nonce=0, fee=0,
                       body={"to": to, "amount": amount, "height": height}, signature=None)


def make_genesis_tx(params: ChainParams, *, to: str, amount: int) -> Transaction:
    return Transaction(version=TX_VERSION, chain_id=params.chain_id, kind=GENESIS, sender=None, nonce=0, fee=0,
                       body={"to": to, "amount": amount}, signature=None)
