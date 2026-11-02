# Agent Economy (working title)

A live economy of AI agents trading creatures on a hand-built blockchain, run once a day. Portfolio piece for amandapatricks.com. Full brief: [spec.md](spec.md).

## Working rules
- **NEVER commit or push. The user does that.** This includes creating the GitHub repo. Where spec.md's Phases section says a phase ends with "commit, and push", that step is the user's: finish with tests passing and hand over the commands.
- Run `scripts/check.ps1` before saying a step is ready to commit. The pre-commit hook runs the same script.
- Keep `docs/WRITEUP.md` current every session, including the dated lessons log. Keep `docs/DATA.md` in step with the export.
- Windows 11, PowerShell syntax for all commands.
- Secrets only in environment variables or a secrets manager; never committed.
- Do one phase per session unless the user says otherwise.

## Phases
- [x] **P0 Setup:** repo, environment, tooling, this checklist.
- [ ] **P1 Chain:** blocks, transactions, signatures, proof of work, Merkle roots, mempool. Fully tested.
- [ ] **P2 Nodes and consensus:** 3 simulated nodes, messaging, consensus, fork handling.
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

# Lint and tests (the same checks the pre-commit hook runs)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1
```
