import pytest
from network_helpers import PARAMS, next_block, pay

from agenteconomy.chain.canonical import canonical_json
from agenteconomy.chain.errors import ChainError
from agenteconomy.chain.validation import make_genesis
from agenteconomy.network import messages as m

GENESIS = make_genesis(PARAMS)
HASH = "ab" * 32


def test_round_trips():
    tx = pay()
    block = next_block(GENESIS.header)
    assert m.decode(m.encode_tx(tx)).tx.id == tx.id
    assert m.decode(m.encode_block(block)).block.to_dict() == block.to_dict()
    assert m.decode(m.encode_blocks([block])).blocks[0].to_dict() == block.to_dict()
    assert m.decode(m.encode_get_blocks([HASH])).locator == (HASH,)
    assert m.decode(m.encode_status(HASH, 3, 48)) == m.StatusMessage(HASH, 3, 48)


def test_work_beyond_safe_integers_survives():
    work = 2**200
    assert m.decode(m.encode_status(HASH, 3, work)).work == work


def test_decoding_builds_new_objects_so_nodes_share_nothing():
    block = next_block(GENESIS.header)
    received = m.decode(m.encode_block(block)).block
    assert received is not block
    assert received.txs[0] is not block.txs[0]


@pytest.mark.parametrize("data", [
    b"",
    b"\xff\xfe",
    b"not json",
    b"[]",
    b"5",
    b'{"type":"nonsense"}',
    b'{"no":"type"}',
    b'{"type":["tx"]}',
    b'{"type":"tx"}',
    b'{"type":"tx","tx":{}}',
    b'{"type":"tx","tx":{},"extra":1}',
    b'{"type":"block","block":{}}',
    b'{"type":"blocks","blocks":"x"}',
    b'{"type":"status","tip":"x","height":1,"work":"1"}',
    b'{"type":"status","height":1,"work":"1"}',
    b'{"type":"get_blocks","locator":"x"}',
    b'{"type":"get_blocks","locator":["short"]}',
    b'{"type":"get_blocks","locator":[],"extra":1}',
    b'{"type":"tx","type":"block"}',
])
def test_junk_is_refused(data):
    with pytest.raises(ChainError):
        m.decode(data)


@pytest.mark.parametrize("fields", [
    {"tip": HASH, "height": -1, "work": "1"},
    {"tip": HASH, "height": True, "work": "1"},
    {"tip": HASH, "height": 1, "work": 1},
    {"tip": HASH, "height": 1, "work": "-1"},
    {"tip": HASH, "height": 1, "work": "1e9"},
    {"tip": HASH, "height": 1, "work": "1" * 101},
    {"tip": HASH.upper(), "height": 1, "work": "1"},
])
def test_bad_status_fields_are_refused(fields):
    with pytest.raises(ChainError):
        m.decode(canonical_json({"type": "status", **fields}))


def test_limits():
    with pytest.raises(ChainError):
        m.decode(m.encode_get_blocks([HASH] * (m.MAX_LOCATOR_HASHES + 1)))
    m.decode(m.encode_get_blocks([HASH] * m.MAX_LOCATOR_HASHES))

    block = next_block(GENESIS.header)
    with pytest.raises(ChainError):
        m.decode(m.encode_blocks([block] * (m.MAX_SYNC_BATCH + 1)))
    m.decode(m.encode_blocks([block] * m.MAX_SYNC_BATCH))

    # Padded with whitespace, so this is valid JSON that only the size limit can refuse.
    status = m.encode_status(HASH, 3, 48)
    m.decode(status + b" " * 100)
    with pytest.raises(ChainError):
        m.decode(status + b" " * m.MAX_MESSAGE_BYTES)
