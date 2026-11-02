"""Every rejection carries a machine-readable code; the invalid-action stats count them."""

from enum import StrEnum


class Code(StrEnum):
    # Format
    MALFORMED = "malformed"
    NON_CANONICAL = "non_canonical"
    UNSUPPORTED_VERSION = "unsupported_version"

    # Transactions
    WRONG_CHAIN = "wrong_chain"
    UNKNOWN_KIND = "unknown_kind"
    UNEXPECTED_KIND = "unexpected_kind"
    INVALID_PUBLIC_KEY = "invalid_public_key"
    BAD_SIGNATURE = "bad_signature"
    SELF_TRANSFER = "self_transfer"
    INVALID_AMOUNT = "invalid_amount"
    FEE_TOO_LOW = "fee_too_low"
    BAD_NONCE = "bad_nonce"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    SUPPLY_OVERFLOW = "supply_overflow"

    # Blocks
    BAD_GENESIS = "bad_genesis"
    BAD_HEIGHT = "bad_height"
    BAD_PREV_HASH = "bad_prev_hash"
    BAD_DIFFICULTY = "bad_difficulty"
    BAD_POW = "bad_pow"
    BAD_TIMESTAMP = "bad_timestamp"
    TOO_MANY_TXS = "too_many_txs"
    BAD_MERKLE_ROOT = "bad_merkle_root"
    BAD_COINBASE = "bad_coinbase"
    DUPLICATE_TX = "duplicate_tx"

    # Mempool
    NONCE_TOO_LOW = "nonce_too_low"
    NONCE_CONFLICT = "nonce_conflict"
    MEMPOOL_FULL = "mempool_full"
    SENDER_LIMIT = "sender_limit"

    # Node policy, not chain rules: a browser replaying a chain never reports these
    FUTURE_TIMESTAMP = "future_timestamp"
    BELOW_CHECKPOINT = "below_checkpoint"
    INVALID_PARENT = "invalid_parent"
    KNOWN_INVALID = "known_invalid"
    DAY_CLOSED = "day_closed"


class ChainError(Exception):
    """The chain refused something. `code` is stable; `detail` is for people."""

    def __init__(self, code: Code, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else str(code))
        self.code = code
        self.detail = detail
