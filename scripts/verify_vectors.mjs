// Independent check of the chain format. Shares no code with the Python, and uses only WebCrypto and standard
// JavaScript, so everything here also runs in a browser. P9's browser verifier starts from this file.
// Usage: node scripts/verify_vectors.mjs testdata/chain_vectors_v1.json

import { readFileSync } from "node:fs";

const TX_TAG = "agenteconomy/tx/v1";
const TX_SIGN_TAG = "agenteconomy/tx-sign/v1";
const BLOCK_TAG = "agenteconomy/block/v1";

const HEX_64 = /^[0-9a-f]{64}$/;
const HEX_128 = /^[0-9a-f]{128}$/;
const CHAIN_ID = /^[a-z0-9-]{1,32}$/;

const BODY_FIELDS = { transfer: ["amount", "to"], coinbase: ["amount", "height", "to"], genesis: ["amount", "to"] };
const USER_KINDS = new Set(["transfer"]);
const TX_FIELDS = ["body", "chain_id", "fee", "kind", "nonce", "sender", "signature", "version"];
const HEADER_FIELDS = ["difficulty_bits", "height", "merkle_root", "nonce", "prev_hash", "timestamp", "version"];

const LOW_ORDER_KEYS = new Set(
  [
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
  ].flatMap((key) => {
    const last = parseInt(key.slice(-2), 16) | 0x80;
    return [key, key.slice(0, -2) + last.toString(16).padStart(2, "0")];
  }),
);

class Rejected extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

const reject = (code) => {
  throw new Rejected(code);
};

// Encoding and hashing

const utf8 = new TextEncoder();

function canonical(value) {
  if (value === null || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) reject("non_canonical");
    return Object.is(value, -0) ? "0" : String(value);
  }
  if (typeof value === "string") {
    if (!value.isWellFormed()) reject("non_canonical");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  // JavaScript's default sort compares UTF-16 code units, which is RFC 8785's order.
  const parts = Object.keys(value)
    .sort()
    .map((key) => `${canonical(key)}:${canonical(value[key])}`);
  return `{${parts.join(",")}}`;
}

const fromHex = (hex) => Uint8Array.from(hex.match(/../g) ?? [], (byte) => parseInt(byte, 16));
const toHex = (bytes) => Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");

function concat(...arrays) {
  const result = new Uint8Array(arrays.reduce((total, array) => total + array.length, 0));
  let offset = 0;
  for (const array of arrays) {
    result.set(array, offset);
    offset += array.length;
  }
  return result;
}

const sha256 = async (bytes) => new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
const taggedBytes = (tag, data) => concat(utf8.encode(tag), new Uint8Array([0]), data);
const taggedHash = (tag, data) => sha256(taggedBytes(tag, data));

const txId = (tx) => taggedHash(TX_TAG, utf8.encode(canonical(tx)));
const headerHash = async (header) => toHex(await taggedHash(BLOCK_TAG, utf8.encode(canonical(header))));

function signingBytes(tx) {
  const { signature, ...unsigned } = tx;
  return taggedBytes(TX_SIGN_TAG, utf8.encode(canonical(unsigned)));
}

async function verifySignature(publicHex, signatureHex, message) {
  try {
    const key = await crypto.subtle.importKey("raw", fromHex(publicHex), { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify("Ed25519", key, fromHex(signatureHex), message);
  } catch {
    return false;
  }
}

// Merkle tree

const leaf = (item) => sha256(concat(new Uint8Array([0]), item));
const node = (left, right) => sha256(concat(new Uint8Array([1]), left, right));

async function merkleRoot(items) {
  let level = await Promise.all(items.map(leaf));
  while (level.length > 1) {
    const parents = [];
    for (let i = 0; i + 1 < level.length; i += 2) parents.push(await node(level[i], level[i + 1]));
    if (level.length % 2) parents.push(level[level.length - 1]);
    level = parents;
  }
  return toHex(level[0]);
}

async function verifyProof(item, proof, rootHex) {
  let current = await leaf(item);
  for (const { side, hash } of proof) {
    current = side === "left" ? await node(fromHex(hash), current) : await node(current, fromHex(hash));
  }
  return toHex(current) === rootHex;
}

// Proof of work

function meetsTarget(hashHex, bits) {
  return BigInt(`0x${hashHex}`) >> BigInt(256 - bits) === 0n;
}

// Format checks (same order as Transaction.check_shape and BlockHeader.check_shape)

const isInt = (value) => Number.isSafeInteger(value);
const isCount = (value) => isInt(value) && value >= 0;
const isHash = (value) => typeof value === "string" && HEX_64.test(value);
const isObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const hasFields = (value, fields) =>
  isObject(value) && Object.keys(value).sort().join() === [...fields].sort().join();

function checkTxShape(tx) {
  if (!hasFields(tx, TX_FIELDS)) reject("malformed");
  if (!isInt(tx.version)) reject("malformed");
  if (tx.version !== 1) reject("unsupported_version");
  if (typeof tx.chain_id !== "string" || !CHAIN_ID.test(tx.chain_id)) reject("malformed");
  if (typeof tx.kind !== "string") reject("malformed");
  if (!Object.hasOwn(BODY_FIELDS, tx.kind)) reject("unknown_kind");
  if (!isCount(tx.nonce) || !isCount(tx.fee)) reject("malformed");

  const { body } = tx;
  if (!hasFields(body, BODY_FIELDS[tx.kind])) reject("malformed");
  if (!isHash(body.to)) reject("malformed");
  if (!isInt(body.amount)) reject("malformed");
  if ("height" in body && !isCount(body.height)) reject("malformed");

  if (USER_KINDS.has(tx.kind)) {
    if (!isHash(tx.sender) || typeof tx.signature !== "string" || !HEX_128.test(tx.signature)) reject("malformed");
  } else if (tx.sender !== null || tx.signature !== null || tx.nonce !== 0 || tx.fee !== 0) {
    reject("malformed");
  }
}

function checkHeaderShape(header) {
  if (!hasFields(header, HEADER_FIELDS)) reject("malformed");
  if (!isInt(header.version)) reject("malformed");
  if (header.version !== 1) reject("unsupported_version");
  if (!isHash(header.prev_hash) || !isHash(header.merkle_root)) reject("malformed");
  if (![header.height, header.timestamp, header.nonce].every(isCount)) reject("malformed");
  if (!isInt(header.difficulty_bits) || header.difficulty_bits < 0 || header.difficulty_bits > 255) reject("malformed");
}

// Rules (same order as validation.py)

async function checkTransaction(tx, params) {
  if (tx.chain_id !== params.chain_id) reject("wrong_chain");
  if (LOW_ORDER_KEYS.has(tx.body.to)) reject("invalid_public_key");
  const isUser = USER_KINDS.has(tx.kind);
  if (isUser && LOW_ORDER_KEYS.has(tx.sender)) reject("invalid_public_key");
  if (tx.body.amount <= 0) reject("invalid_amount");
  if (isUser) {
    if (tx.sender === tx.body.to) reject("self_transfer");
    if (tx.fee < params.min_fee) reject("fee_too_low");
    if (!(await verifySignature(tx.sender, tx.signature, signingBytes(tx)))) reject("bad_signature");
  }
}

function applyTx(state, tx) {
  const { to, amount } = tx.body;
  if (tx.kind === "transfer") {
    const spend = amount + tx.fee;
    if (tx.nonce !== (state.nonces[tx.sender] ?? 0)) reject("bad_nonce");
    if (spend > (state.balances[tx.sender] ?? 0)) reject("insufficient_balance");
    state.balances[tx.sender] -= spend;
    state.balances[to] = (state.balances[to] ?? 0) + amount;
    state.nonces[tx.sender] = tx.nonce + 1;
    state.burned += tx.fee;
  } else {
    if (state.minted + amount > Number.MAX_SAFE_INTEGER) reject("supply_overflow");
    state.balances[to] = (state.balances[to] ?? 0) + amount;
    state.minted += amount;
  }
}

async function makeGenesis(params) {
  const txs = params.genesis_allocations.map(([to, amount]) => ({
    version: 1, chain_id: params.chain_id, kind: "genesis", sender: null, nonce: 0, fee: 0,
    body: { to, amount }, signature: null,
  }));
  const header = {
    version: 1, height: 0, prev_hash: "00".repeat(32), timestamp: params.genesis_timestamp,
    merkle_root: await merkleRoot(await Promise.all(txs.map(txId))),
    difficulty_bits: params.difficulty_bits, nonce: 0,
  };
  return { header, txs };
}

async function validateBlock(block, parent, state, params) {
  const { header, txs } = block;
  const parentHash = await headerHash(parent);
  if (header.height !== parent.height + 1) reject("bad_height");
  if (header.prev_hash !== parentHash) reject("bad_prev_hash");
  if (header.difficulty_bits !== params.difficulty_bits) reject("bad_difficulty");
  if (!meetsTarget(await headerHash(header), header.difficulty_bits)) reject("bad_pow");
  if (header.timestamp < parent.timestamp) reject("bad_timestamp");
  if (txs.length > params.max_block_txs) reject("too_many_txs");
  if (txs.length === 0) reject("bad_coinbase");

  const ids = await Promise.all(txs.map(txId));
  if ((await merkleRoot(ids)) !== header.merkle_root) reject("bad_merkle_root");

  const coinbase = txs[0];
  if (coinbase.kind !== "coinbase") reject("bad_coinbase");
  await checkTransaction(coinbase, params);
  if (coinbase.body.amount !== params.block_reward) reject("bad_coinbase");
  if (coinbase.body.height !== header.height) reject("bad_coinbase");

  if (new Set(ids.map(toHex)).size !== ids.length) reject("duplicate_tx");

  applyTx(state, coinbase);
  for (const tx of txs.slice(1)) {
    if (tx.kind === "coinbase") reject("bad_coinbase");
    if (!USER_KINDS.has(tx.kind)) reject("unexpected_kind");
    await checkTransaction(tx, params);
    applyTx(state, tx);
  }
}

async function validateChain(blocks, params) {
  // Parse everything first, as Block.from_dict does.
  for (const block of blocks) {
    if (!hasFields(block, ["header", "txs"]) || !Array.isArray(block.txs)) reject("malformed");
    checkHeaderShape(block.header);
    block.txs.forEach(checkTxShape);
  }

  if (blocks.length === 0 || canonical(blocks[0]) !== canonical(await makeGenesis(params))) reject("bad_genesis");

  const state = { balances: {}, nonces: {}, minted: 0, burned: 0 };
  blocks[0].txs.forEach((tx) => applyTx(state, tx));
  for (let i = 1; i < blocks.length; i += 1) {
    await validateBlock(blocks[i], blocks[i - 1].header, state, params);
  }
  return state;
}

// Running the vectors

const path = process.argv[2];
if (!path) {
  console.error("Usage: node scripts/verify_vectors.mjs <vectors file>");
  process.exit(2);
}

const vectors = JSON.parse(readFileSync(path, "utf8"));
let passed = 0;
const failures = [];

function check(name, ok) {
  if (ok) passed += 1;
  else failures.push(name);
}

const rejectionOf = (run) => {
  try {
    run();
    return null;
  } catch (error) {
    return error.code ?? String(error);
  }
};

for (const { input, canonical: expected, sha256: hash } of vectors.canonical.valid) {
  const encoded = canonical(input);
  check(`canonical encoding of ${expected}`, encoded === expected);
  check(`canonical hash of ${expected}`, toHex(await sha256(utf8.encode(encoded))) === hash);
}
for (const { name, input } of vectors.canonical.reject) {
  check(`canonical must reject: ${name}`, rejectionOf(() => canonical(input)) === "non_canonical");
}

for (const { tag, data, hash } of vectors.tagged_hash) {
  check(`tagged hash ${tag}`, toHex(await taggedHash(tag, fromHex(data))) === hash);
}

for (const [caseIndex, { items, root, proofs }] of vectors.merkle.entries()) {
  check(`Merkle root, ${items.length} items`, (await merkleRoot(items.map(fromHex))) === root);
  for (const [i, proof] of proofs.entries()) {
    check(`Merkle proof, case ${caseIndex} item ${i}`, await verifyProof(fromHex(items[i]), proof, root));
    check(`Merkle proof must fail for another root, case ${caseIndex} item ${i}`,
      !(await verifyProof(fromHex(items[i]), proof, "00".repeat(32))));
  }
}

for (const { public_key: key, message, signature } of vectors.ed25519.valid) {
  check(`Ed25519 valid: ${key.slice(0, 8)}`, await verifySignature(key, signature, fromHex(message)));
}
for (const { name, public_key: key, message, signature } of vectors.ed25519.reject) {
  check(`Ed25519 must reject: ${name}`, !(await verifySignature(key, signature, fromHex(message))));
}
for (const key of vectors.ed25519.low_order_public_keys) {
  check(`low-order key refused: ${key.slice(0, 8)}`, LOW_ORDER_KEYS.has(key));
}
check("low-order key lists are the same size", vectors.ed25519.low_order_public_keys.length === LOW_ORDER_KEYS.size);

for (const { hash, bits, meets, work } of vectors.proof_of_work) {
  check(`proof of work ${hash.slice(0, 8)} at ${bits} bits`, meetsTarget(hash, bits) === meets);
  check(`work at ${bits} bits`, (2n ** BigInt(bits)).toString() === work);
}

for (const { tx, signing_bytes: bytes, id } of vectors.transactions) {
  check(`transaction shape ${id.slice(0, 8)}`, rejectionOf(() => checkTxShape(tx)) === null);
  check(`transaction ID ${id.slice(0, 8)}`, toHex(await txId(tx)) === id);
  check(`signing bytes ${id.slice(0, 8)}`, toHex(signingBytes(tx)) === bytes);
}

for (const { header, hash } of vectors.blocks) {
  check(`block hash ${hash.slice(0, 8)}`, (await headerHash(header)) === hash);
}

const { params, blocks, final_state: finalState, tip } = vectors.chain;
try {
  const state = await validateChain(blocks, params);
  check("sample chain final state", canonical(state) === canonical(finalState));
  check("sample chain tip", (await headerHash(blocks[blocks.length - 1].header)) === tip);
  check("balances add up to minted minus burned",
    Object.values(state.balances).reduce((a, b) => a + b, 0) === state.minted - state.burned);
} catch (error) {
  check(`sample chain must be valid, but was rejected: ${error.code ?? error}`, false);
}

for (const { name, code, blocks: tampered } of vectors.tampered_chains) {
  let actual = null;
  try {
    await validateChain(tampered, params);
  } catch (error) {
    actual = error.code ?? String(error);
  }
  check(`tampered chain "${name}": expected ${code}, got ${actual}`, actual === code);
}

if (failures.length) {
  console.error(`Vector check FAILED: ${failures.length} failed, ${passed} passed`);
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}
console.log(`Vector check passed: ${passed} checks`);
