# Agent Economy (working title) — Spec

## What it is
A live economy of AI agents trading fighting creatures on a blockchain I built myself. Every day, a scheduled simulation runs: agents mine, make offers, negotiate, and trade, and the results are recorded on the chain. Visitors follow agents, read the daily newspaper and agent diaries, make predictions, and replay any day.

Portfolio purpose: show agent design, tool use, prompt-injection defense, evaluation, and blockchain fundamentals in one project.

## Working rules (every session)
- **NEVER commit or push** the user does this.
- Run tests before every commit.
- Keep docs/WRITEUP.md current (see Deliverables).
- Windows 11, PowerShell syntax for all commands.
- Secrets only in environment variables or a secrets manager; never committed.

## The economy
- **Assets:** fighting creatures from my licensed asset library (I'll supply many species). Each creature has a species, traits, and combat stats that affect its value. No battles in this version; stats only matter for value and trading.
- **Licensing:** raw asset files must not be committed to a public repo. Keep source assets in a gitignored folder and publish only the processed images the site needs. Flag anything that looks like a license problem.
- **Currency:** a native coin. Agents start with balances; mining earns rewards; trading fees are burned so the money supply doesn't inflate forever.
- **Mining:** agents mine. Each mined block hatches a creature; the block's hash determines its species (from the current season's species pool), traits, and stats.
- **Trading:** direct offers only (one agent proposes, the other accepts, counters, or rejects).
- **Chain-enforced rules:** valid signatures, sufficient balances, no double spends, ownership checks. Agents can attempt anything; the chain rejects invalid transactions.
- **Shocks:** coded, not tied to real-world data. A market crash has an extremely low daily probability (seeded and reproducible). Other rare events are fine to propose.
- **Seasons:** each season, new species enter the hatching pool and I may add new agents (configured by me). Crown a season champion; keep a hall of fame.

## The blockchain
- My own implementation: blocks, hashing, proof of work, Merkle roots, digital signatures, a mempool.
- **Multiple simulated independent nodes** (start with 3), each with its own copy of the chain, communicating through simulated messages with latency, and a consensus rule (recommend one: e.g., longest valid chain).
- Agents' signing keys are generated per agent; agents can only act through signed transactions.
- **Trust layer (later phase):** visitors' browsers verify the chain they load (hashes, proof of work, signatures), and the latest block hash is anchored publicly each day (e.g., OpenTimestamps). Recommend the approach.

## The agents
- **Roster:** start with 4 agents with distinct goals and risk tolerance (e.g., collector, speculator, cautious saver, scammer).
- **Tools only:** read tools (market prices, holdings, recent trades, inbox) and action tools (mine, make offer, accept, counter, reject, send message). No other way to affect the world.
- **Memory:** a short summary carried into the next day.
- **Limits:** a cap on actions and model calls per agent per day; a hard daily cost cap for the whole run.
- **Logged reasoning:** each decision's short rationale stored off-chain for the replay.
- **Reproducibility:** record model version, settings, and inputs for each day.

## Agent messaging and prompt-injection defense
- Agents can send each other messages to negotiate.
- **All messages are untrusted data,** never instructions. Keep them clearly separated from the agent's own instructions, and never let a message trigger a tool call on its own.
- The scammer agent will deliberately try manipulation, including injection attempts. Log every attempt and its outcome.
- Measure the injection success rate over time and display it.
- Recommend specific defenses and document them.

## Content filtering
Everything that appears publicly (agent messages, diaries, the newspaper, creature names if generated) passes a content filter before publishing. Blocked content is logged, not shown.

## Visitor features
1. **The Daily Ledger:** an AI-written front page each day (headlines, biggest trade, notable scams, a price chart), generated from the chain's actual events and checked against them. Nothing invented.
2. **Follow an agent:** pick a favorite and track its net worth, best and worst trades, and history. Stored in the browser.
3. **Daily predictions:** e.g., who will be richest tomorrow, will the scammer succeed. Results arrive with the next run; streaks tracked in the browser. Predictions never affect the simulation.
4. **Agent diaries:** each agent writes a one-line entry about its day, checked against what actually happened.
5. **Replay:** step through any day's activity: blocks, trades, messages, and agent rationales.
6. **Stats:** agent net worth over time, win rates by personality, prices, volume, wealth inequality, creature population, invalid-action rate, injection success rate, recap accuracy, cost per day.

## Data export
Publish daily snapshot files (blocks, events, agent stats) as static files with a documented schema, plus an index file. The replay loads from the same files. Recommend the format.

## Daily run
- Scheduled daily on AWS. Reuse the setup from my One-Pager Generator project where practical, including the LLM provider. Recommend the right compute (it must not time out mid-run).
- Timeouts, retries, the daily cost cap, and protection against running the same day twice.
- If the run fails, the site keeps showing the last good day.

## Testing
- Unit tests for chain validity, consensus, and transaction rules.
- Full simulations using simple scripted agents in place of AI, so everything can be tested at no cost.
- Tests that the newspaper and diaries never claim something that didn't happen.

## Deliverables beyond the code
My website builds its own project card and page, so you only need to produce:
- **docs/WRITEUP.md**, kept up to date every session:
  - **What it does:** plain-language overview.
  - **How it works:** architecture, including an architecture diagram in Mermaid (nodes, chain, agents, tools, daily run, content filter, data export, front end).
  - **Key design decisions** and why.
  - **Security:** prompt-injection defenses and what the data showed.
  - **Lessons learned:** a dated running log of problems, surprises, and what I'd do differently.
- **docs/DATA.md:** the export schema.

## Phases (each ends with tests passing)
- **P0 Setup:** repo, environment, tooling, CLAUDE.md with this plan as a checklist.
- **P1 Chain:** blocks, transactions, signatures, proof of work, Merkle roots, mempool. Fully tested.
- **P2 Nodes and consensus:** 3 simulated nodes, messaging, consensus, fork handling.
- **P3 Economy:** currency, mining, hatching from my assets, direct offers, fees, shocks. Run with scripted agents.
- **P4 AI agents:** roster, tools, memory, limits, logging, reproducibility.
- **P5 Messaging and security:** agent messages, injection defenses, the scammer's attempts, measurement.
- **P6 Daily run:** scheduled job, cost cap, failure handling, data export.
- **P7 Front end:** replay, stats, follow an agent.
- **P8 Content:** the Daily Ledger, diaries, predictions, content filter.
- **P9 Trust layer and seasons:** browser verification, public anchoring, season rollover, hall of fame.
- **P10 Polish:** accessibility, performance, final WRITEUP.md.