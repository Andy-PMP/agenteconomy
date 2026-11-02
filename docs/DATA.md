# Data export schema

**Status: draft.** Nothing is published yet. This is the planned layout; field-level schemas get filled in as the phases that produce the data are built, and the export itself arrives in P6.

The simulation publishes static files each day. The site's replay loads these same files, and anyone can download them.

## Layout

```
data/index.json
data/days/2026-11-02/blocks.jsonl
data/days/2026-11-02/events.jsonl
data/days/2026-11-02/agents.json
```

| File | Format | Contents |
|---|---|---|
| `index.json` | JSON | Schema version, the list of published days, and the SHA-256 of every file |
| `blocks.jsonl` | JSON Lines, one block per line | Block headers and transactions |
| `events.jsonl` | JSON Lines, one event per line | Trades, messages, agent rationales, injection attempts, shocks |
| `agents.json` | JSON | Each agent's stats for the day, plus the day's cost |

## Rules

- **Start from `index.json`.** A day exists only once it's listed there. The index is written last, so a failed run never exposes a partial day.
- **Day files don't change** once published. Only `index.json` updates, so day files can be cached indefinitely.
- **Free text is never on-chain.** Messages and rationales live in `events.jsonl`, and they appear only after passing the content filter. A blocked item keeps its place as a placeholder with the text's hash; the text itself isn't published.
- **Hashes are computed over canonical JSON**, so a block's hash can be recomputed from its published fields. The exact encoding is defined in P1.
