"""Run the economy with the real model. THIS SPENDS REAL MONEY, so it refuses to start without --spend-real-money.

Keys are public test seeds, and results go to the gitignored data/ folder, so nothing here is for publishing.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from agenteconomy.ai.budget import DAILY_CAP_NANOS, NANOS_PER_DOLLAR, FileLedger, ModelCaller  # noqa: E402
from agenteconomy.ai.model import BedrockModel  # noqa: E402
from agenteconomy.ai.roster import PERSONALITIES, build_members  # noqa: E402
from agenteconomy.chain.vectors import sample_keys  # noqa: E402
from agenteconomy.economy.market import net_worth  # noqa: E402
from agenteconomy.economy.world import World, WorldConfig, economy_params  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--days", type=int, default=2)
parser.add_argument("--limit-dollars", type=float, default=0.30, help="Hard limit for this whole run")
parser.add_argument("--turns", type=int, default=6)
parser.add_argument("--out", type=Path, default=ROOT / "data" / "real-run")
parser.add_argument("--spend-real-money", action="store_true", help="Required: this calls Bedrock")
args = parser.parse_args()
if not args.spend_real_money:
    parser.error("this calls Bedrock and costs money; pass --spend-real-money to confirm")

args.out.mkdir(parents=True, exist_ok=True)
limit_nanos = int(args.limit_dollars * NANOS_PER_DOLLAR)
keys = {name: sample_keys(name) for name in PERSONALITIES}
caller = ModelCaller(BedrockModel(), FileLedger(args.out / "spend.json"))
members, run = build_members(keys, caller)
params = economy_params([(key.public_hex, 1000) for key in keys.values()], chain_id="real-test")
world = World(WorldConfig(params, seed=f"real-{args.out.name}", turns_per_day=args.turns, action_limit=8,
                                mine_limit=2, closing_blocks=True), members, ai_run=run)

total = 0
for _ in range(args.days):
    caller.cap_nanos = min(DAILY_CAP_NANOS, limit_nanos - total)
    report = world.run_day()
    total += report.model_cost_nanos
    (args.out / f"day-{report.day:04}.json").write_text(json.dumps(world.last_day_record, indent=1), encoding="utf-8")
    (args.out / f"day-{report.day:04}-log.json").write_text(json.dumps(world.last_log, indent=1), encoding="utf-8")
    print(f"day {report.day}: {report.model_calls} calls, ${report.model_cost_nanos / NANOS_PER_DOLLAR:.4f}, "
          f"{report.new_blocks} blocks, {report.trades} trades, {report.actions} actions, "
          f"{report.invalid_actions} invalid {report.invalid_by_code}, {report.model_errors} model errors, "
          f"{report.refused_calls} refused, {report.reorgs} reorgs")

world.save(args.out / "world.json")
print(f"total: ${total / NANOS_PER_DOLLAR:.4f} of ${args.limit_dollars:.2f} limit")
for member in world.members:
    address = member.keys.public_hex
    print(f"{member.agent.name}: coins {world.state.balance(address)}, "
          f"creatures {len(world.state.creatures_of(address))}, net worth {net_worth(world.state, address)}, "
          f"note: {member.agent.memory.note!r}")
