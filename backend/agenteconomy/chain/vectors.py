"""Builds testdata/chain_vectors_v1.json: known answers any implementation of the format must reproduce.

Seeds here are public test values. Everything is deterministic, so regenerating must not change a byte.
"""

import hashlib
import json
from dataclasses import replace
from typing import Any

from .block import Block, BlockHeader, block_work, compute_merkle_root, meets_target, mine
from .canonical import MAX_SAFE_INT, TX_TAG, canonical_json, tagged_hash
from .keys import _LOW_ORDER_KEYS, KeyPair
from .merkle import merkle_proof, merkle_root
from .params import ChainParams
from .state import State
from .transaction import Transaction, make_coinbase, make_genesis_tx, sign_transfer
from .validation import build_block, make_genesis, validate_chain

ED25519_ORDER = 2**252 + 27742317777372353535851937790883648493

# RFC 8032 section 7.1, tests 1 and 2.
_RFC_8032 = [
    {"seed": "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "public_key": "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "message": "",
     "signature": "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
                  "d25bf5f0595bbe24655141438e7a100b"},
    {"seed": "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "public_key": "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "message": "72",
     "signature": "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
                  "387b2eaeb4302aeeb00d291612bb0c00"},
]


def sample_keys(name: str) -> KeyPair:
    return KeyPair.from_seed(hashlib.sha256(f"agenteconomy public test seed: {name}".encode()).digest())


def malleate_signature(signature_hex: str) -> str:
    """Add the group order to S. Mathematically equivalent, so a lax verifier would accept a changed signature."""
    signature = bytes.fromhex(signature_hex)
    s = int.from_bytes(signature[32:], "little") + ED25519_ORDER
    return (signature[:32] + s.to_bytes(32, "little")).hex()


def params_to_dict(params: ChainParams) -> dict[str, Any]:
    return {"chain_id": params.chain_id, "difficulty_bits": params.difficulty_bits,
            "block_reward": params.block_reward,
            "genesis_allocations": [list(item) for item in params.genesis_allocations],
            "genesis_timestamp": params.genesis_timestamp, "max_block_txs": params.max_block_txs,
            "min_fee": params.min_fee}


def params_from_dict(data: dict[str, Any]) -> ChainParams:
    allocations = tuple((address, amount) for address, amount in data["genesis_allocations"])
    return ChainParams(**{**data, "genesis_allocations": allocations})


def validate_chain_dicts(blocks: list[dict[str, Any]], params: ChainParams) -> State:
    """Validate chain data as it arrives from a file: parse every block first, then replay."""
    return validate_chain([Block.from_dict(block) for block in blocks], params)


def _state_to_dict(state: State) -> dict[str, Any]:
    return {"balances": state.balances, "nonces": state.nonces, "minted": state.minted, "burned": state.burned}


def _canonical_vectors() -> dict[str, Any]:
    inputs = [
        None, True, False, 0, -1, MAX_SAFE_INT, -MAX_SAFE_INT, "", [], {},
        "quote \" backslash \\ newline \n tab \t backspace \b formfeed \f return \r",
        "control \u0000 \u001f delete \u007f line separator  ",
        "é € 😀",
        {"b": [1, {"d": None, "c": True}], "a": "x"},
        # RFC 8785 section 3.2.3's key-order example: UTF-16 order puts the emoji before U+FB33.
        {"€": 1, "\r": 2, "דּ": 3, "1": 4, "\U0001f600": 5, "\u0080": 6, "ö": 7},
    ]
    valid = []
    for value in inputs:
        encoded = canonical_json(value)
        valid.append({"input": value, "canonical": encoded.decode("utf-8"),
                      "sha256": hashlib.sha256(encoded).hexdigest()})
    reject = [
        {"name": "float", "input": 1.5},
        {"name": "integer above the safe range", "input": 2**53},
        {"name": "integer below the safe range", "input": -(2**53)},
        {"name": "nested float", "input": {"a": [1.5]}},
        {"name": "lone surrogate in a string", "input": "\ud800"},
        {"name": "lone surrogate in a key", "input": {"\udc00": 1}},
    ]
    return {"valid": valid, "reject": reject}


def _merkle_vectors() -> list[dict[str, Any]]:
    cases = []
    for count in range(1, 9):
        items = [hashlib.sha256(f"item {i}".encode()).digest() for i in range(count)]
        proofs = [[{"side": side, "hash": sibling.hex()} for side, sibling in merkle_proof(items, i)]
                  for i in range(count)]
        cases.append({"items": [item.hex() for item in items], "root": merkle_root(items).hex(), "proofs": proofs})
    return cases


def _ed25519_vectors() -> dict[str, Any]:
    first = _RFC_8032[0]
    flipped = bytes.fromhex(first["signature"])
    flipped = bytes([flipped[0] ^ 1]) + flipped[1:]
    return {
        "valid": _RFC_8032,
        "reject": [
            {"name": "S above the group order", "public_key": first["public_key"], "message": first["message"],
             "signature": malleate_signature(first["signature"])},
            {"name": "flipped bit", "public_key": first["public_key"], "message": first["message"],
             "signature": flipped.hex()},
            {"name": "wrong message", "public_key": first["public_key"], "message": "00",
             "signature": first["signature"]},
        ],
        "low_order_public_keys": sorted(_LOW_ORDER_KEYS),
    }


def _pow_vectors() -> list[dict[str, Any]]:
    cases = [(bytes(32), 255), (bytes(32), 0), (b"\xff" * 32, 0), (b"\xff" * 32, 1),
             (b"\x00\x7f" + b"\xff" * 30, 9), (b"\x00\x7f" + b"\xff" * 30, 10)]
    return [{"hash": value.hex(), "bits": bits, "meets": meets_target(value, bits), "work": str(block_work(bits))}
            for value, bits in cases]


class _Chain:
    """The sample chain, plus helpers for building broken versions of its last block."""

    def __init__(self):
        self.alice, self.bob, self.carol = sample_keys("alice"), sample_keys("bob"), sample_keys("carol")
        self.params = ChainParams(
            chain_id="vectors-v1", difficulty_bits=8, block_reward=50, min_fee=1, max_block_txs=4,
            genesis_timestamp=100,
            genesis_allocations=((self.alice.public_hex, 1000), (self.bob.public_hex, 500)),
        )
        self.genesis = make_genesis(self.params)
        block1 = self.mined(self.genesis.header, [
            self.pay(self.alice, self.bob, 100, nonce=0, fee=2),
            self.pay(self.bob, self.alice, 30, nonce=0, fee=1),
        ], timestamp=110)
        block2 = self.mined(block1.header, [self.pay(self.alice, self.carol, 10, nonce=1, fee=1)], timestamp=120)
        self.blocks = [self.genesis, block1, block2]

    def pay(self, sender: KeyPair, to: KeyPair, amount: int, *, nonce: int, fee: int = 1,
            chain_id: str | None = None) -> Transaction:
        return sign_transfer(sender, chain_id or self.params.chain_id, to=to.public_hex, amount=amount, nonce=nonce,
                             fee=fee)

    def mined(self, parent: BlockHeader, txs: list[Transaction], *, timestamp: int) -> Block:
        return self.remine(build_block(self.params, parent, miner=self.carol.public_hex, txs=txs, timestamp=timestamp))

    def remine(self, block: Block) -> Block:
        """Fix up the Merkle root and proof of work, so the block's only remaining fault is the one being tested."""
        header = replace(block.header, merkle_root=compute_merkle_root(block.txs), nonce=0)
        mined = mine(header, max_attempts=1 << 20)
        assert mined is not None
        return Block(mined, block.txs)

    def with_last_txs(self, txs: list[Transaction]) -> list[Block]:
        """The sample chain with block 2's user transactions replaced."""
        return [*self.blocks[:2], self.mined(self.blocks[1].header, txs, timestamp=120)]

    def tampered(self) -> list[dict[str, Any]]:
        alice, bob, carol, params = self.alice, self.bob, self.carol, self.params
        block1, block2 = self.blocks[1], self.blocks[2]
        good = block2.txs[1]
        coinbase = block2.txs[0]
        identity = "01" + "00" * 31

        def last_block(block: Block) -> list[Block]:
            return [*self.blocks[:2], block]

        def with_txs(*txs: Transaction) -> list[Block]:
            return last_block(self.remine(Block(block2.header, txs)))

        def header(**changes: Any) -> list[Block]:
            return last_block(Block(replace(block2.header, **changes), block2.txs))

        forged_amount = replace(good, body={**good.body, "amount": 11})
        unbalanced = [Block(block2.header, (coinbase, forged_amount))]
        block3 = self.mined(block2.header, [good], timestamp=130)
        early = self.remine(Block(replace(block2.header, timestamp=block1.header.timestamp - 1), block2.txs))
        other_genesis_tx = make_genesis_tx(params, to=alice.public_hex, amount=999_999)
        other_genesis = Block(self.genesis.header, (other_genesis_tx, self.genesis.txs[1]))

        cases: dict[str, tuple[str, list[Block]]] = {
            "genesis allocation changed": ("bad_genesis", [other_genesis, *self.blocks[1:]]),
            "unsupported block version": ("unsupported_version", header(version=2)),
            "wrong height": ("bad_height", header(height=5)),
            "previous hash changed": ("bad_prev_hash", header(prev_hash="ab" * 32)),
            "difficulty lowered": ("bad_difficulty", header(difficulty_bits=0)),
            "nonce changed, no proof of work": ("bad_pow", header(nonce=block2.header.nonce + 1)),
            "timestamp before parent": ("bad_timestamp", last_block(early)),
            "too many transactions": ("too_many_txs", self.with_last_txs(
                [self.pay(alice, carol, 1, nonce=n) for n in range(1, 5)])),
            "transaction changed, Merkle root not updated": ("bad_merkle_root", [*self.blocks[:2], *unbalanced]),
            "no transactions": ("bad_coinbase", last_block(Block(block2.header, ()))),
            "no coinbase": ("bad_coinbase", with_txs(good)),
            "inflated coinbase": ("bad_coinbase", with_txs(
                replace(coinbase, body={**coinbase.body, "amount": 51}), good)),
            "coinbase height wrong": ("bad_coinbase", with_txs(
                replace(coinbase, body={**coinbase.body, "height": 9}), good)),
            "second coinbase": ("bad_coinbase", with_txs(
                coinbase, make_coinbase(params, to=alice.public_hex, height=2))),
            "same transaction twice": ("duplicate_tx", with_txs(coinbase, good, good)),
            "genesis transaction in a later block": ("unexpected_kind", with_txs(coinbase, make_genesis_tx(
                params, to=alice.public_hex, amount=5))),
            "amount changed after signing": ("bad_signature", with_txs(coinbase, forged_amount)),
            "signature from someone else": ("bad_signature", with_txs(coinbase, replace(
                good, signature=bob.sign(good.signing_bytes)))),
            "S above the group order": ("bad_signature", with_txs(coinbase, replace(
                good, signature=malleate_signature(good.signature)))),
            "low-order sender with a forged signature": ("invalid_public_key", with_txs(coinbase, replace(
                good, sender=identity, signature=identity + "00" * 32))),
            "low-order recipient": ("invalid_public_key", with_txs(coinbase, sign_transfer(
                alice, params.chain_id, to=identity, amount=1, nonce=1, fee=1))),
            "signed for another chain": ("wrong_chain", with_txs(coinbase, self.pay(
                alice, carol, 10, nonce=1, chain_id="other-chain"))),
            "pays itself": ("self_transfer", with_txs(coinbase, self.pay(alice, alice, 10, nonce=1))),
            "zero amount": ("invalid_amount", with_txs(coinbase, self.pay(alice, carol, 0, nonce=1))),
            "negative amount": ("invalid_amount", with_txs(coinbase, self.pay(alice, carol, -5, nonce=1))),
            "fee under the minimum": ("fee_too_low", with_txs(coinbase, self.pay(alice, carol, 10, nonce=1, fee=0))),
            "replay of an earlier transaction": ("bad_nonce", [*self.blocks, block3]),
            "double spend in one block": ("bad_nonce", with_txs(
                coinbase, self.pay(alice, carol, 10, nonce=1), self.pay(alice, bob, 10, nonce=1))),
            "skipped nonce": ("bad_nonce", with_txs(coinbase, self.pay(alice, carol, 10, nonce=2))),
            "spends more than the balance": ("insufficient_balance", with_txs(
                coinbase, self.pay(alice, carol, 1_000_000, nonce=1))),
            "balance covers the amount but not the fee": ("insufficient_balance", with_txs(
                coinbase, self.pay(alice, carol, 928, nonce=1, fee=1))),
        }
        vectors = [{"name": name, "code": code, "blocks": [block.to_dict() for block in blocks]}
                   for name, (code, blocks) in cases.items()]

        # These can't be expressed as a valid object, so they're edited as raw data, the way a bad file would arrive.
        vectors += [
            self._raw_tamper("free text in a transfer", "malformed", lambda tx: tx["body"].update(memo="hello")),
            self._raw_tamper("uppercase hex signature", "malformed",
                             lambda tx: tx.update(signature=tx["signature"].upper())),
            self._raw_tamper("unknown field", "malformed", lambda tx: tx.update(extra=1)),
            self._raw_tamper("unknown transaction kind", "unknown_kind", lambda tx: tx.update(kind="mint")),
            self._raw_tamper("unsupported transaction version", "unsupported_version", lambda tx: tx.update(version=2)),
            self._raw_tamper("true used as an integer", "malformed", lambda tx: tx.update(nonce=True)),
        ]
        return vectors

    def _raw_tamper(self, name: str, code: str, edit: Any) -> dict[str, Any]:
        blocks = [block.to_dict() for block in self.blocks]
        edit(blocks[2]["txs"][1])
        return {"name": name, "code": code, "blocks": blocks}


def _transaction_vectors(chain: _Chain) -> list[dict[str, Any]]:
    txs = [
        chain.blocks[1].txs[1],
        chain.blocks[1].txs[0],
        chain.genesis.txs[0],
    ]
    return [{"tx": tx.to_dict(), "signing_bytes": tx.signing_bytes.hex(), "id": tx.id.hex()} for tx in txs]


def build_vectors() -> dict[str, Any]:
    chain = _Chain()
    final = validate_chain(chain.blocks, chain.params)
    return {
        "format": "agenteconomy chain vectors",
        "version": 1,
        "notes": "All keys and seeds here are public test values. See docs/DATA.md for the rules.",
        "canonical": _canonical_vectors(),
        "tagged_hash": [{"tag": TX_TAG, "data": "616263", "hash": tagged_hash(TX_TAG, b"abc").hex()}],
        "merkle": _merkle_vectors(),
        "ed25519": _ed25519_vectors(),
        "proof_of_work": _pow_vectors(),
        "transactions": _transaction_vectors(chain),
        "blocks": [{"header": block.header.to_dict(), "hash": block.header.hash_hex} for block in chain.blocks],
        "chain": {"params": params_to_dict(chain.params), "blocks": [block.to_dict() for block in chain.blocks],
                  "final_state": _state_to_dict(final), "tip": chain.blocks[-1].header.hash_hex},
        "tampered_chains": chain.tampered(),
    }


def render_vectors() -> str:
    return json.dumps(build_vectors(), indent=1, sort_keys=True, ensure_ascii=True) + "\n"

