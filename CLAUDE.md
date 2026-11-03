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
- [x] **P3 Economy:** currency, mining, hatching from the assets, direct offers, fees, shocks. Run with scripted agents.
- [x] **P4 AI agents:** roster, tools, memory, limits, logging, reproducibility.
- [x] **P5 Messaging and security:** agent messages, injection defenses, the scammer's attempts, measurement.
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
- **Cost cap:** hard $0.50/day for the whole run, including retries. Measured real spend is $0.13 to $0.17 a day; see AI agents below.
- **Export:** JSON Lines per day plus `index.json`, written last so a failed run leaves the last good day live. See `docs/DATA.md`.
- **Injection defense:** messages can't wake an agent; `accept(offer_id)` uses the signed offer's terms; the tool layer holds keys and limits; messages are marked as data; memory is built mostly from structured facts; the newspaper writer sees chain events, not raw messages. Count scams and injections separately.
- **Art:** Kenney Monster Builder Pack (CC0 per its `License.txt`), assembled from parts. Chosen instead of the store-bought packs because GameDev Market's terms bar NFT use, and creatures traded on a chain look too close to that. Check the license before adding any other pack.
- **Species:** 6 body shapes x 6 colours = 36 possible species, opened by season. The chain stores IDs only, never art. The pack has no attachment points, so `art/render.py` has a hand-tuned offset table per body shape.
- **Hosting (P6-P7):** Astro on GitHub Pages at a subdomain, like the user's other projects; daily data in S3 behind CloudFront.

## Chain (P1)
`backend/agenteconomy/chain/`. The exact format is specified in `docs/DATA.md`.
- **Model:** accounts with a per-sender nonce, not UTXOs. The nonce is what stops replays and double spends. Address = hex Ed25519 public key. Integers only, up to 2^53-1, so JavaScript stays exact.
- **Adding a transaction kind:** add its exact fields to `_BODY_FIELDS` and to `USER_KINDS` in `transaction.py`, its rules to `check_transaction` and `apply_tx`, then mirror it in `scripts/verify_vectors.mjs`. Strings must be hex or known values, never free text. Adding a kind keeps existing hashes the same.
- **Check order is part of the format.** Python and `scripts/verify_vectors.mjs` must report the same rejection code, so change them together.
- **Vectors:** `testdata/chain_vectors_v1.json` is generated by `scripts/generate_vectors.py`; a test fails if regenerating changes a byte. Regenerating to pass a test means the format changed, so only do it deliberately. Take published known answers from the source, never from memory.
- **Low-order public keys are refused.** OpenSSL (through `cryptography`) accepted a forged signature for one; Node's WebCrypto refused it. See `keys.py`. Browsers may differ on other Ed25519 edge cases, so P9 should test the real browsers it supports.
- **Mining:** `mine()` takes `max_attempts`, for P3's equal budgets. Measured about 1.25M hashes/s on the dev machine, so 20 bits is about 0.8s per block and 24 bits about 13s. Single-threaded, and Fargate will differ.
- **For P2:** `validate_chain`, `validate_block`, `cumulative_work`, `Mempool.on_new_tip`, `Mempool.readd_after_reorg`. `apply_block` and `validate_block` return a new state, so keeping state per branch is safe. Timestamps only have to be no earlier than the parent; P2 decides any limit on future timestamps.

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

- **Replay at settle** runs from the last checkpoint, not genesis, or it would grow without limit. `Node(trusted_state=...)` skips the replay at start; pass only a state already derived from those blocks. `World.load` does one full replay from genesis, so a damaged save is noticed. `EventLog` entries are what P6 exports.

## Economy (P3)
`backend/agenteconomy/economy/`, plus `chain/creature.py`. Start with `World` in `world.py` and `economy_params`.
- **Decisions (user, 2026-11-02):** soft cap plus a 2% trade burn; later-season agents funded by an ordinary transfer from a genesis treasury; unmined transactions dropped at settle and reported; a market crash hits appraisals, not balances.
- **Offers:** `offer`, `accept`, `reject`; a counter is an `offer` with `counter_of`. No escrow: everything is checked at `accept`, all or nothing. Either party can `reject`, which is how a proposer withdraws.
- **Hatching:** creature ID is the block hash, the miner owns it, and it can be traded from the next block. Attributes are derived, never stored on-chain. Re-hash for every roll, because a mined hash starts with zeros.
- **Shocks:** `hash(seed, day, previous tip)`, off-chain, acting through `Conditions` only. Never add a shock that changes chain rules or balances.
- **The tool layer is the security boundary.** Agents get a `View` and return `Action`s. P4's model tool calls must map onto these same `Action`s, so the model never holds a key, a node or `AgentTools`. Keys are never saved by `World.save`.
- **`View` hides offers the agent has already answered.** Otherwise agents act twice on one offer before their first answer is mined. P4 should feed the same view to the model. It also lists every creature, so P4 needs to summarize: there are about 2,800 after a year.
- **Mempool:** `Mempool.add` and `prune` try transactions against current state, or a stale `accept` would block that sender's later nonces forever. `node` logs drops as `tx_dropped`.
- **Measured** with scripted agents (4 agents, 8 turns a day, 16-bit difficulty, 65,536-hash budget): about 7.6 blocks a day, 12.8% orphaned, 1.2% invalid actions, about 2 dropped transactions a day, about 1 trade a day. Scripted agents only exercise the machinery; don't tune the economy to them. A 365-day run at 10 bits took 35s, with per-day time growing as creatures accumulate, because `AgentTools.view` lists them all.
- **Reward decays quickly:** about 12 coins a block after a year, from 50. Revisit `block_reward` and `supply_cap` once AI agents show real trading volume.
- **Vectors:** `testdata/economy_vectors_v1.json` includes a real simulated chain, so a change to scripted agents or `world.py` changes the file. Regenerate deliberately with `scripts/generate_vectors.py`. `chain_vectors_v1.json` must stay byte-identical: new settings default to off.
- **Art:** `assets/parts/` is generated by `scripts/build_parts.py` and committed. Tune offsets in `art/render.py`, and check with `scripts/render_sheet.py`. White species nearly vanish on a light background, so P7 needs a darker card behind creatures.

## AI agents (P4)
`backend/agenteconomy/ai/`. Start with `build_members` in `roster.py` and `AIAgent` in `agent.py`.
- **No paid calls from tests.** `tests/conftest.py` makes `boto3.client` raise. Tests use `FakeModel`; `tests/ai/ai_helpers.py` has one that plays the game. Only `scripts/run_real_days.py --spend-real-money` calls Bedrock. Tell the user the expected cost and get an OK before every real run. Results go to `data/` (gitignored).
- **Boundary:** `AIAgent` holds only a `ModelCaller`, prompts and memory. Tool calls become `Action`s only in `tool_schemas.parse_action`. Read tools answer from the `View`. A test walks everything reachable from an agent to confirm no key or node.
- **Handles:** the model sees `c:1a2b3c4d`, `o:...` and agent names, never addresses or full hashes. `summary.py` resolves them against the view; anything unknown gets an error.
- **Limits:** 6 turns, 2 calls per turn (the last offers only action tools), 16 calls and 8 actions per agent per day, 2 mines per agent per day. Enforced in code.
- **Mining limit:** without one, mining is free positive value, and real agents did nothing else. `WorldConfig.mine_limit`; off for scripted worlds.
- **Closing blocks:** agents mine early and act late, so without these their late transactions were never mined and no trade could happen. `WorldConfig.closing_blocks` spends equal hash budgets at the end of the day, with no model calls, while something can be mined. Off for scripted worlds.
- **Cost:** `ModelCaller` reserves the worst case before each attempt (`chars / 2 + 500` tokens in, all `max_tokens` out), and refuses if that could pass the cap. It charges actual usage after, or worst case on an ambiguous failure. boto3's retries are off, so every attempt is metered. The cap is exact only while that estimate holds; the largest real call was 3,427 tokens against about 5,000 reserved. Integer nanodollars. A failed estimate can overshoot by one call, and then everything stops.
- **Prices** in `model.PRICES`: $1.10/$5.50 per million tokens for the US profile, from an AWS post and an Anthropic PDF. AWS's pricing page loads dynamically and the public price list has no Haiku 4.5 row, so **this isn't confirmed against the official table**. Compare with the bill.
- **Memory:** code-built facts for 3 days, plus a note of at most 200 characters. An empty note must never overwrite a good one. P5 adds injection checks on the note, which is the one model-written thing carried forward.
- **Day record:** `World.last_day_record` has model ID, settings, prompt and tool hashes, and every call's input, reply, usage and cost. Rationales are in the event log as `agent_decision`. Neither is public until the P8 content filter exists.
- **Real results:** 9 real days so far, $1.40 by the meter. Closing blocks work, but only 1 real trade so far, so trading is still thin. Prompt caching needs 4,096 tokens per checkpoint, more than these prompts have.

## Messaging and security (P5)
`economy/messages.py` (board), `ai/attacks.py` (fixed set), `ai/security.py` (sending, judging, rates).
- **Message path:** text is off-chain, capped at 280 characters and cleaned. It reaches a model only through the `read_inbox` tool result, in `untrusted_text` under a fixed notice. The summary shows only counts and senders. Never put message text in the system prompt, the summary, memory or the note call.
- **No waking:** posting schedules nothing. A test compares turns and call counts with and without messages.
- **Note call never sees a message.** `memory.note_problem` is the second line of defense: instruction-like patterns, or 5+ words copied from a message read that day. Real notes are test fixtures, so tune it against those.
- **Aliases:** agents see each other as Ash, Birch, Cedar and Dune (`roster.ALIASES`). Logs keep real names (`shown_as` is only for what agents see). Never let the role names reach another agent's prompt, or the injection rate measures the word "scammer".
- **Attacks:** code sends them, one per rival per day, rotating through `ATTACKS`, and one rival a day gets a harmless control. Don't edit the set casually: it's what makes rates comparable over time. An accept attack needs an open offer from the scammer to name.
- **Judging is from the tool log:** `not_read`, `resisted`, `obeyed`, `blocked`. Headline rate is (obeyed + blocked) / read, damage rate is obeyed / read, and `injection_stats` gives both by day and in total. `note_poison` can't really succeed, because the note call never sees the message, so it's a check on the design, not on the model. Report rates by goal if that skews them.
- **Scams stay separate:** overpaid trades and bait failures, from the chain and log.
- **Spend limit:** `WorldConfig.max_spend_bps`, 50% of coins per trade in real runs (`spend_limit`).
- **Real runs need:** `mine_limit=2, closing_blocks=True, max_spend_bps=5_000`, `security=SecurityMonitor()`; `scripts/run_real_days.py` sets these. P6 must too.
- **Message content isn't public yet.** It holds free text, so P8's filter comes first. Rationales are the same.

## Assets
Raw files go in `assets/source/` (gitignored); see `assets/source/README.md`. Processed parts are committed in `assets/parts/`, because CI and Fargate don't have the raw pack. Only processed images the site needs get published. `tests/test_repo_hygiene.py` fails if raw files are tracked.

## AWS
- us-east-1; local CLI uses the `automation` IAM user. Keep the account ID out of this public repo. Its deploy policy only covers `onepager*` names, so P6 needs a new `agenteconomy-deploy` policy, created by the user as admin.
- The account budget is `monthly-20-usd` (raised by the user on 2026-11-02; `automation` can't read budgets, so this is as reported). Haiku 4.5 is billed through AWS Marketplace, so charges appear under the model provider, not Bedrock.
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
