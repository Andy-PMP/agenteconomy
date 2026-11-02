# Data export and chain format

**Status:** the chain format below is implemented and checked by two independent implementations. The daily export files arrive in P6, so the file layout and the fields of `events.jsonl` and `agents.json` are still a draft.

The simulation publishes static files each day. The site's replay loads these same files, and anyone can download them and verify the chain.

## Files

```
data/index.json
data/days/2026-11-02/blocks.jsonl
data/days/2026-11-02/events.jsonl
data/days/2026-11-02/agents.json
```

| File | Format | Contents |
|---|---|---|
| `index.json` | JSON | Schema version, the list of published days, and the SHA-256 of every file |
| `blocks.jsonl` | JSON Lines, one block per line | Block headers and transactions, in the format below |
| `events.jsonl` | JSON Lines, one event per line | Trades, messages, agent rationales, injection attempts, shocks |
| `agents.json` | JSON | Each agent's stats for the day, plus the day's cost |

- **Start from `index.json`.** A day exists only once it's listed there. The index is written last, so a failed run never exposes a partial day.
- **Day files don't change** once published. Only `index.json` updates, so day files can be cached indefinitely.
- **Free text is never on-chain.** Messages and rationales live in `events.jsonl`, and they appear only after passing the content filter. A blocked item keeps its place as a placeholder with the text's hash; the text itself isn't published.

## Block

```json
{"header": { ... }, "txs": [ ... ]}
```

| Header field | Type | Meaning |
|---|---|---|
| `version` | integer | Always `1` |
| `height` | integer | `0` for genesis, then parent + 1 |
| `prev_hash` | hex, 64 | Parent block's hash; all zeros for genesis |
| `merkle_root` | hex, 64 | Merkle root of the transaction IDs |
| `timestamp` | integer | Simulated time, not wall-clock time; no earlier than the parent |
| `difficulty_bits` | integer, 0 to 255 | Required leading zero bits in the block's hash |
| `nonce` | integer | Found by mining |

## Transaction

| Field | Type | Meaning |
|---|---|---|
| `version` | integer | Always `1` |
| `chain_id` | string, `[a-z0-9-]{1,32}` | Which chain this is for, so it can't be replayed on another |
| `kind` | string | See the table below |
| `sender` | hex, 64, or null | Sender's address; null for `coinbase` and `genesis` |
| `nonce` | integer | Sender's transaction count so far; `0` for `coinbase` and `genesis` |
| `fee` | integer | Burned, not paid to the miner; `0` for `coinbase` and `genesis` |
| `body` | object | Exactly the fields listed for the kind |
| `signature` | hex, 128, or null | Ed25519 signature; null for `coinbase` and `genesis` |

| Kind | Body | Meaning |
|---|---|---|
| `transfer` | `to`, `amount` | Sender pays `to`. Sender's balance must cover `amount + fee` |
| `coinbase` | `to`, `amount`, `height` | Block reward. Exactly one per mined block, always first. `amount` must equal the reward and `height` the block's height |
| `genesis` | `to`, `amount` | Starting balance. Only in the genesis block |

An address is the hex-encoded Ed25519 public key. All hex is lowercase. All integers are between -(2^53-1) and 2^53-1, and floats are not allowed anywhere. Unknown fields make a block or transaction invalid.

## Verifying a chain

These are the exact rules. `testdata/chain_vectors_v1.json` has worked examples, and `scripts/verify_vectors.mjs` is a complete, dependency-free implementation using the browser's WebCrypto API.

**Canonical JSON.** Everything hashed or signed is first encoded as a strict subset of RFC 8785: no whitespace, object keys sorted by UTF-16 code unit (JavaScript's default sort), strings encoded as `JSON.stringify` does, UTF-8 output, and lone surrogates refused.

**Tagged hashes.** `tagged_hash(tag, data) = SHA-256(tag + 0x00 + data)`.

| Value | Computed as |
|---|---|
| Transaction ID | `tagged_hash("agenteconomy/tx/v1", canonical(whole transaction))` |
| Signed message | `"agenteconomy/tx-sign/v1" + 0x00 + canonical(transaction without its signature field)` |
| Block hash | `tagged_hash("agenteconomy/block/v1", canonical(header))` |

**Merkle root.** Hash each transaction ID as a leaf, `SHA-256(0x00 + id)`. Then combine each level in pairs, `SHA-256(0x01 + left + right)`. An unpaired last node moves up a level unchanged.

**Proof of work.** Read the block hash as a 256-bit big-endian number. It's valid if `hash >> (256 - difficulty_bits) == 0`. A block's work is `2^difficulty_bits`; genesis counts as zero. Competing chains are compared by total work.

**Signatures.** Ed25519 (RFC 8032). Reject `S` at or above the group order. Refuse the low-order public keys listed in the vectors, as either sender or recipient: forged signatures can verify against those keys for any message, and implementations disagree about them.

**Replay.** Parse every block and check its format first. Confirm block 0 exactly matches the genesis block built from the chain's parameters, then apply its allocations. For each later block, stop at the first failure, in this order:

1. Height is parent + 1, `prev_hash` is the parent's hash, difficulty is the required value, and proof of work is valid.
2. Timestamp is no earlier than the parent's, and the block doesn't exceed the transaction limit.
3. The block has transactions, and the Merkle root matches.
4. The first transaction is a valid `coinbase` for the right reward and height.
5. No transaction ID repeats.
6. Apply the coinbase. Then, for each other transaction in order: it isn't a `coinbase` or `genesis`; `chain_id` matches; keys are usable; `amount` is positive; sender isn't the recipient; fee meets the minimum; signature is valid; `nonce` equals the sender's count so far; balance covers `amount + fee`.

Applying a `transfer` subtracts `amount + fee` from the sender, adds `amount` to the recipient, adds 1 to the sender's count, and burns `fee`. Across the whole chain, balances always add up to minted minus burned.

**Rejection codes.** Each failure has a stable code, such as `bad_nonce` or `insufficient_balance`, listed in `backend/agenteconomy/chain/errors.py`.

**Strict parsing.** `JSON.parse` can't tell `1.0` from `1`, or see duplicate keys, but the Python loader refuses both. That doesn't affect verification, because hashes are computed over the re-encoded canonical form.
