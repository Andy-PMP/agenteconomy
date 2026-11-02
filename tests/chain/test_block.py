import hashlib
from dataclasses import replace

import pytest
from chain_helpers import PARAMS, mined_block

from agenteconomy.chain.block import Block, BlockHeader, block_work, meets_target, mine
from agenteconomy.chain.canonical import BLOCK_TAG, canonical_json, tagged_hash
from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.validation import make_genesis

HEADER = BlockHeader(version=1, height=1, prev_hash="ab" * 32, merkle_root="cd" * 32, timestamp=5, difficulty_bits=8,
                     nonce=0)


def test_hash_covers_the_canonical_header():
    assert HEADER.hash == tagged_hash(BLOCK_TAG, canonical_json(HEADER.to_dict()))


@pytest.mark.parametrize("changes", [
    {"version": 2}, {"height": 2}, {"prev_hash": "ee" * 32}, {"merkle_root": "ee" * 32}, {"timestamp": 6},
    {"difficulty_bits": 9}, {"nonce": 1},
])
def test_every_header_field_changes_the_hash(changes):
    assert replace(HEADER, **changes).hash != HEADER.hash


def test_every_field_is_covered_by_the_table_above():
    assert set(HEADER.to_dict()) == {"version", "height", "prev_hash", "merkle_root", "timestamp", "difficulty_bits",
                                     "nonce"}


@pytest.mark.parametrize("value, bits, expected", [
    (bytes(32), 255, True),
    (bytes(32), 0, True),
    (b"\xff" * 32, 0, True),
    (b"\xff" * 32, 1, False),
    (b"\x7f" + b"\xff" * 31, 1, True),
    (b"\x00\x7f" + b"\xff" * 30, 9, True),
    (b"\x00\x7f" + b"\xff" * 30, 10, False),
    (b"\x00\x80" + b"\x00" * 30, 8, True),
    (b"\x00\x80" + b"\x00" * 30, 9, False),
])
def test_target_boundaries(value, bits, expected):
    assert meets_target(value, bits) is expected


def test_work_doubles_with_each_bit():
    assert block_work(0) == 1
    assert block_work(10) == 1024
    assert block_work(255) == 2**255


def test_mined_nonce_really_meets_the_target():
    # mine() takes a shortcut, so check its answer with the full hash, across many headers.
    for timestamp in range(30):
        mined = mine(replace(HEADER, timestamp=timestamp), max_attempts=1 << 16)
        assert mined is not None
        assert meets_target(mined.hash, mined.difficulty_bits)


def test_mining_finds_the_first_valid_nonce():
    mined = mine(HEADER, max_attempts=1 << 16)
    for nonce in range(mined.nonce):
        assert not meets_target(replace(HEADER, nonce=nonce).hash, HEADER.difficulty_bits)


def test_mining_is_deterministic():
    assert mine(HEADER, max_attempts=1 << 16) == mine(HEADER, max_attempts=1 << 16)


def test_mining_stops_at_the_attempt_limit():
    # Each agent's hash budget in P3 depends on this. At 255 bits no real attempt can succeed.
    assert mine(replace(HEADER, difficulty_bits=255), max_attempts=100) is None
    assert mine(HEADER, max_attempts=0) is None


def test_mining_resumes_where_it_left_off():
    target = mine(HEADER, max_attempts=1 << 16)
    assert target.nonce > 0, "pick a header whose answer isn't nonce 0"
    assert mine(HEADER, max_attempts=target.nonce) is None
    assert mine(replace(HEADER, nonce=target.nonce), max_attempts=1) == target


def test_mining_stops_at_the_largest_safe_nonce():
    header = replace(HEADER, nonce=2**53 - 1, difficulty_bits=255)
    assert mine(header, max_attempts=10) is None


def test_mining_with_zero_difficulty_takes_the_first_nonce():
    assert mine(replace(HEADER, difficulty_bits=0), max_attempts=1).nonce == 0


def test_hashing_is_the_documented_formula():
    expected = hashlib.sha256(BLOCK_TAG.encode() + b"\x00" + canonical_json(HEADER.to_dict())).digest()
    assert HEADER.hash == expected


def test_block_round_trip_through_a_dict():
    block = mined_block(make_genesis(PARAMS).header)
    assert Block.from_dict(block.to_dict()).to_dict() == block.to_dict()


@pytest.mark.parametrize("edit", [
    lambda h: h.update(extra=1),
    lambda h: h.pop("nonce"),
    lambda h: h.update(height=-1),
    lambda h: h.update(height=True),
    lambda h: h.update(timestamp=1.5),
    lambda h: h.update(nonce=2**53),
    lambda h: h.update(difficulty_bits=256),
    lambda h: h.update(difficulty_bits=-1),
    lambda h: h.update(prev_hash="AB" * 32),
    lambda h: h.update(merkle_root="short"),
    lambda h: h.update(version=True),
])
def test_malformed_headers_are_refused(edit):
    data = HEADER.to_dict()
    edit(data)
    with pytest.raises(ChainError) as e:
        BlockHeader.from_dict(data)
    assert e.value.code == Code.MALFORMED


@pytest.mark.parametrize("data", [None, [], {}, {"header": {}}, {"header": HEADER.to_dict(), "txs": "x"},
                                  {"header": HEADER.to_dict(), "txs": [], "extra": 1}])
def test_malformed_blocks_are_refused(data):
    with pytest.raises(ChainError):
        Block.from_dict(data)
