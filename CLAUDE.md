# Agent Economy (working title)

A live economy of AI agents trading creatures on a hand-built blockchain, run once a day. Portfolio piece for amandapatricks.com. Full brief: [spec.md](spec.md).

## Working rules
- **NEVER commit or push. The user does that.** Finish with tests passing and hand over the commands.
- Run `scripts/check.ps1` before saying a step is ready to commit. The pre-commit hook runs the same script.
- Keep `docs/WRITEUP.md` current every session, including the dated lessons log. Keep `docs/DATA.md` in step with the export.
- Windows 11, PowerShell syntax for all commands.
- Secrets only in environment variables or a secrets manager; never committed.
- Do one phase per session unless the user says otherwise.

## Phases
- [x] **P0 Setup:** repo, environment, tooling, this checklist.
- [x] **P1 Chain:** blocks, transactions, signatures, proof of work, Merkle roots, mempool. Fully tested.
- [x] **P2 Nodes and consensus:** 3 simulated nodes, messaging, consensus, fork handling.
- [ ] **P3 Economy:** currency, mining, hatching from the assets, direct offers, fees, shocks. Run with scripted agents.
- [ ] **P4 AI agents:** roster, tools, memory, limits, logging, reproducibility.
- [ ] **P5 Messaging and security:** agent messages, injection defenses, the scammer's attempts, measurement.
- [ ] **P6 Daily run:** scheduled job, cost cap, failure handling, data export.
- [ ] **P7 Front end:** replay, stats, follow an agent.
- [ ] **P8 Content:** the Daily Ledger, diaries, predictions, content filter.
- [ ] **P9 Trust layer and seasons:** browser verification, public anchoring, season rollover, hall of fame.
- [ ] **P10 Polish:** accessibility, performance, final WRITEUP.md.

Each phase ends with tests passing. Testing requirements are in spec.md: scripted agents must be able to run full simulations at no cost.

## Decisions (2026-11-01)
Reasons are in `docs/WRITEUP.md`.
- **Consensus:** most cumulative valid work; on a tie, keep the chain seen first. Nodes run in a seeded simulation with simulated time and latency. Reorged transactions return to the mempool. Each day ends with a settle step: all nodes must agree on one tip before publishing.
- **Crypto:** Ed25519 from `cryptography`, SHA-256 from `hashlib`. The chain logic is hand-built; the primitives are not. Hash canonical JSON, with test vectors shared between Python and the browser (P9).
- **Free text never goes on-chain.** The content filter must be able to hide text without breaking a hash.
- **Compute:** ECS Fargate started by EventBridge Scheduler, us-east-1, deployed with SAM. Not Lambda (15-minute limit). ECS has no task timeout, so run under a `timeout` wrapper (about 60 minutes). DynamoDB conditional write per date stops double runs. Docker isn't installed locally: GitHub Actions builds the image and pushes to ECR.
- **Model:** Claude Haiku 4.5 on Bedrock, `us.anthropic.claude-haiku-4-5-20251001-v1:0`, set by config. The One-Pager notes say Haiku 5.5 is blocked for this account; don't retry unless the user says access changed.
- **Cost cap:** hard $0.50/day for the whole run, including retries. Estimated normal spend is about $0.30/day at list price; check Bedrock pricing at P4.
- **Export:** JSON Lines per day plus `index.json`, written last so a failed run leaves the last good day live. See `docs/DATA.md`.
- **Injection defense:** messages can't wake an agent; `accept(offer_id)` uses the signed offer's terms; the tool layer holds keys and limits; messages are marked as data; memory is built mostly from structured facts; the newspaper writer sees chain events, not raw messages. Count scams and injections separately.
- **Art:** Kenney Monster Builder Pack (CC0 per its `License.txt`), assembled from parts. Chosen instead of the store-bought packs because GameDev Market's terms bar NFT use, and creatures traded on a chain look too close to that. Check the license before adding any other pack.
- **Species (P3):** 6 body shapes x 6 colours = 36 possible species, opened by season. The chain stores IDs only, never art. The pack has no attachment points, so P3 needs a hand-tuned offset table per body shape.
- **Hosting (P6-P7):** Astro on GitHub Pages at a subdomain, like the user's other projects; daily data in S3 behind CloudFront.

## Chain (P1)
`backend/agenteconomy/chain/`. The exact format is specified in `docs/DATA.md`.
- **Model:** accounts with a per-sender nonce, not UTXOs. The nonce is what stops replays and double spends. Address = hex Ed25519 public key. Integers only, up to 2^53-1, so JavaScript stays exact.
- **Adding a transaction kind (P3):** add its exact fields to `_BODY_FIELDS` and to `USER_KINDS` in `transaction.py`, its rules to `check_transaction` and `apply_tx`, then mirror it in `scripts/verify_vectors.mjs`. Strings must be hex or known values, never free text. Adding a kind keeps existing hashes the same.
- **Check order is part of the format.** Python and `scripts/verify_vectors.mjs` must report the same rejection code, so change them together.
- **Vectors:** `testdata/chain_vectors_v1.json` is generated by `scripts/generate_vectors.py`; a test fails if regenerating changes a byte. Regenerating to pass a test means the format changed, so only do it deliberately. Take published known answers from the source, never from memory.
- **Low-order public keys are refused.** OpenSSL (through `cryptography`) accepted a forged signature for one; Node's WebCrypto refused it. See `keys.py`. Browsers may differ on other Ed25519 edge cases, so P9 should test the real browsers it supports.
- **Mining:** `mine()` takes `max_attempts`, for P3's equal budgets. Measured about 1.25M hashes/s on the dev machine, so 20 bits is about 0.8s per block and 24 bits about 13s. Single-threaded, and Fargate will differ.
- **For P2:** `validate_chain`, `validate_block`, `cumulative_work`, `Mempool.on_new_tip`, `Mempool.readd_after_reorg`. `apply_block` and `validate_block` return a new state, so keeping state per branch is safe. Timestamps only have to be no earlier than the parent; P2 decides any limit on future timestamps.
- **For P3:** `min_fee` and `block_reward` are placeholders in `ChainParams`. Reward is flat, so P3 decides how supply stays under control. Seasons may add agents after genesis, and there's currently no way to fund one except by transfer or mining.

## Nodes (P2)
`backend/agenteconomy/network/`. Start with `Cluster` in `settle.py`.
- **Determinism:** no wall clock, threads or iteration over sets. Time is integer simulated milliseconds. Get randomness only from `sim.rng(purpose)`, one stream per purpose. `tests/network/test_real_forks.py` compares runs across processes with different `PYTHONHASHSEED`.
- **Settle tie-break:** lowest block hash, in `Node.resolve_tie`, settle only. During the day, first seen stays.
- **Checkpoints:** a settled tip can't be forked below (`below_checkpoint`), so published days stay true. `finalize()` drops side branches and old saved states, so memory holds only the tip's state.
- **Validation order in `Node._on_block` matters.** Cheap checks (difficulty, proof of work, Merkle root) are never remembered. Only a block whose transactions match its header may enter `_invalid`; otherwise a swapped-transaction attack gets an honest block refused forever.
- **Penalties:** only for things no honest node would do. Stale or unaffordable transactions aren't penalized, since nodes see different states in normal operation. Node-policy codes in `errors.py` aren't chain rules, so don't add them to `verify_vectors.mjs`.
- **Measured orphan rates** (3 miners, one per node, 60 simulated seconds, 5 seeds; latency is uniform from 20ms to the max):

| Per-miner block rate | Max 50ms | Max 200ms | Max 800ms | Max 3000ms |
|---|---|---|---|---|
| 1 per 3s | 0.8% | 8.1% | 16.0% | 39.0% |
| 1 per 1s | 6.1% | 13.7% | 31.6% | 52.7% |
| 4 per 1s | 18.8% | 37.2% | 53.0% | 62.4% |

- **For P3:** agents act through `node.submit_tx` and `node.mine(miner, max_attempts)`, which continues its nonce search between calls. Schedule agent turns with `sim.schedule`. `Node.mine` takes no simulated time by itself, so the orchestrator must advance the clock. Choosing latency and hash budget sets how many forks the newspaper gets. Add creatures to `State` carefully: `State.copy` runs per block and states are kept per unsettled block. `SettleResult.pending` holds unmined transactions; P3 or P6 decides whether they carry over. `EventLog` entries are what P6 exports.

## Assets
Raw files go in `assets/source/` (gitignored); see `assets/source/README.md`. Only processed images the site needs get published. `tests/test_repo_hygiene.py` fails if raw files are tracked.

## AWS
- us-east-1; local CLI uses the `automation` IAM user. Keep the account ID out of this public repo. Its deploy policy only covers `onepager*` names, so P6 needs a new `agenteconomy-deploy` policy, created by the user as admin.
- The account budget alert was $5/month per the One-Pager docs. The user should raise it to about $20 before P6.
- Reuse from `../onepager`: `template.yaml` and `docs/deploy.md` for SAM patterns, `backend/onepager/bedrock.py` for the Bedrock wrapper design. `sam deploy` prompts for confirmation, which the agent shell can't answer: use `--no-execute-changeset`, review, then execute the change set.

## Environment
```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
git config core.hooksPath .githooks

# Lint, tests and the independent JS vector check (the same checks the pre-commit hook runs)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1
```
Node 24 is needed for the vector check.
