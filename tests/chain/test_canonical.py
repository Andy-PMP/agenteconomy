import hashlib

import pytest

from agenteconomy.chain.canonical import MAX_SAFE_INT, canonical_json, decode_strict, tagged_hash
from agenteconomy.chain.errors import ChainError, Code


def test_sha256_known_answer():
    # FIPS 180 example: confirms the primitive before anything is built on it.
    assert hashlib.sha256(b"abc").hexdigest() == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_keys_are_sorted_with_no_whitespace():
    assert canonical_json({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'


def test_key_order_follows_rfc_8785_utf16_rule():
    # RFC 8785 section 3.2.3. Sorting by code point would wrongly put U+FB33 before the emoji.
    value = {"\u20ac": 0, "\r": 0, "\ufb33": 0, "1": 0, "\U0001f600": 0, "\u0080": 0, "\u00f6": 0}
    expected = '{"\\r":0,"1":0,"\u0080":0,"\u00f6":0,"\u20ac":0,"\U0001f600":0,"\ufb33":0}'
    assert canonical_json(value) == expected.encode("utf-8")


def test_strings_use_shortest_escapes_and_raw_utf8():
    assert canonical_json('"\\\n\t\b\f\r\u0001') == b'"\\"\\\\\\n\\t\\b\\f\\r\\u0001"'
    assert canonical_json("é😀") == '"é😀"'.encode()


def test_safe_integer_limits():
    assert canonical_json(MAX_SAFE_INT) == b"9007199254740991"
    assert canonical_json(-MAX_SAFE_INT) == b"-9007199254740991"


@pytest.mark.parametrize("value", [
    1.5, 1.0, float("nan"), float("inf"), 2**53, -(2**53), "\ud800", {"\udc00": 1}, {1: "x"}, (1, 2), b"x", {1, 2},
    {"nested": [1.5]},
])
def test_unsupported_values_are_refused(value):
    with pytest.raises(ChainError) as e:
        canonical_json(value)
    assert e.value.code == Code.NON_CANONICAL


def test_deep_nesting_is_refused_not_a_crash():
    value = []
    for _ in range(100_000):
        value = [value]
    with pytest.raises(ChainError):
        canonical_json(value)


def test_decode_round_trip():
    assert decode_strict(b'{"a":[1,"x",null,true]}') == {"a": [1, "x", None, True]}


@pytest.mark.parametrize("data", [
    b'{"a":1,"a":2}',  # other parsers might keep the first; we'd hash the last
    b'{"x":{"a":1,"a":1}}',
    b"1.0",
    b"1e3",
    b"NaN",
    b"Infinity",
    b"9007199254740992",
    b'"\\ud800"',
    b"\xff",
    b"{not json",
    b"[" * 200_000,
])
def test_decode_refuses_ambiguous_input(data):
    with pytest.raises(ChainError) as e:
        decode_strict(data)
    assert e.value.code == Code.NON_CANONICAL


def test_tags_separate_domains():
    assert tagged_hash("a", b"data") != tagged_hash("b", b"data")
    # The separator stops bytes moving between the tag and the data.
    assert tagged_hash("ab", b"c") != tagged_hash("a", b"bc")
