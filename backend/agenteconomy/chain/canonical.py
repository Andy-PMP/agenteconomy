"""Canonical JSON and tagged hashing: the exact bytes every hash and signature covers.

A strict subset of RFC 8785 that Python and browsers encode identically: objects, arrays, strings, booleans,
null, and integers JavaScript can represent exactly. Floats aren't allowed anywhere.
"""

import hashlib
import json
from typing import Any

from .errors import ChainError, Code

MAX_SAFE_INT = 2**53 - 1  # Number.MAX_SAFE_INTEGER

TX_TAG = "agenteconomy/tx/v1"
TX_SIGN_TAG = "agenteconomy/tx-sign/v1"
BLOCK_TAG = "agenteconomy/block/v1"


def canonical_json(value: Any) -> bytes:
    try:
        return _encode(value).encode("utf-8")
    except RecursionError as e:
        raise ChainError(Code.NON_CANONICAL, "nested too deeply") from e


def _encode(value: Any) -> str:
    # bool first: in Python, True is also an int.
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise ChainError(Code.NON_CANONICAL, "integer outside JavaScript's safe range")
        return str(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ChainError(Code.NON_CANONICAL, "object keys must be strings")
        # RFC 8785 orders by UTF-16 code unit, which is what JavaScript's default sort does.
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(f"{_encode_string(key)}:{_encode(value[key])}" for key in keys) + "}"
    raise ChainError(Code.NON_CANONICAL, f"{type(value).__name__} isn't allowed")


def _encode_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ChainError(Code.NON_CANONICAL, "lone surrogate in string") from e
    return json.dumps(value, ensure_ascii=False)


def decode_strict(data: bytes | str) -> Any:
    """Parse JSON from outside, refusing what would parse differently elsewhere: duplicate keys and floats."""
    try:
        value = json.loads(data, object_pairs_hook=_reject_duplicate_keys, parse_float=_reject_number,
                           parse_constant=_reject_number)
    except (ValueError, RecursionError) as e:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise ChainError(Code.NON_CANONICAL, "not valid JSON") from e
    canonical_json(value)  # also refuses unsafe integers and lone surrogates
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ChainError(Code.NON_CANONICAL, "duplicate object key")
    return result


def _reject_number(text: str) -> None:
    raise ChainError(Code.NON_CANONICAL, f"number {text} isn't an integer")


def tagged_bytes(tag: str, data: bytes) -> bytes:
    """The tag keeps a hash or signature made for one type from being valid for another."""
    return tag.encode("ascii") + b"\x00" + data


def tagged_hash(tag: str, data: bytes) -> bytes:
    return hashlib.sha256(tagged_bytes(tag, data)).digest()
