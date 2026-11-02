"""Whole days with AI agents and a fake model. The real thing differs only in who answers."""

import json
import os
import subprocess
import sys
from pathlib import Path

from ai_helpers import call, make_world

from agenteconomy.ai.budget import NANOS_PER_DOLLAR, FileLedger
from agenteconomy.ai.model import MODEL_ID, ModelError
from agenteconomy.ai.roster import PERSONALITIES
from agenteconomy.chain.validation import validate_chain
from agenteconomy.economy.market import net_worth
from agenteconomy.economy.world import MAX_CLOSING_ATTEMPTS
from agenteconomy.network.node import Node

ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = """
import sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from ai_helpers import make_world
world, _, _ = make_world("cross")
for _ in range(4):
    world.run_day()
print(world.blocks[-1].header.hash_hex, [r.log_digest for r in world.reports])
"""


def run(world, days=3):
    for _ in range(days):
        world.run_day()


def test_ai_agents_run_days_on_the_real_chain():
    world, _, _ = make_world()
    run(world, 5)
    assert world.blocks[-1].header.height > 5
    assert validate_chain(world.blocks, world.config.params) == world.state
    assert sum(report.actions for report in world.reports) > 20
    assert all(name in {member.agent.name for member in world.members} for name in PERSONALITIES)


def test_agents_really_mine_trade_and_remember():
    world, _, _ = make_world()
    run(world, 12)
    kinds = {tx.kind for block in world.blocks for tx in block.txs}
    assert {"coinbase", "offer", "accept"} <= kinds
    assert world.trades
    for member in world.members:
        assert member.agent.memory.note
        assert member.agent.memory.days


def test_every_decision_is_logged_with_its_rationale():
    world, _, _ = make_world()
    world.run_day()
    decisions = [entry for entry in world.last_log if entry["kind"] == "agent_decision"]
    assert len(decisions) == 4 * 6
    assert all(entry["rationale"] for entry in decisions)
    assert {entry["node"] for entry in decisions} == set(PERSONALITIES)


def test_decisions_come_before_their_outcomes_in_the_log():
    world, _, _ = make_world()
    world.run_day()
    kinds = [entry["kind"] for entry in world.last_log if entry["kind"] in ("agent_decision", "agent_action")]
    assert kinds[0] == "agent_decision"
    assert kinds[1] == "agent_action"


def test_rationales_never_reach_the_chain():
    def behavior(tools, **request):
        if tools[0]["name"] == "write_note":
            return [call("write_note", note="secret-note-marker")]
        return [call("mine", rationale="secret-rationale-marker")]

    world, _, _ = make_world(behavior=behavior)
    run(world, 2)
    chain = json.dumps([block.to_dict() for block in world.blocks])
    assert "secret-rationale-marker" not in chain and "secret-note-marker" not in chain
    assert any(entry.get("rationale") == "secret-rationale-marker" for entry in world.last_log)


def test_day_record_captures_model_settings_and_inputs():
    world, _, _ = make_world()
    world.run_day()
    record = world.last_day_record
    assert record["day"] == 0
    assert record["model_id"] == MODEL_ID
    assert record["settings"]["provider"] == "fake"
    assert record["settings"]["action_max_tokens"] == 300
    assert set(record["prompts"]) == set(PERSONALITIES)
    assert len(record["tools_hash"]) == 64
    assert len(record["calls"]) >= 4 * 6 + 4
    assert record["calls"][0]["messages"][0]["role"] == "user"
    json.dumps(record)


def test_day_report_shows_cost_and_calls():
    world, _, caller = make_world()
    report = world.run_day()
    assert report.model_calls == len(caller.records) > 0
    assert report.model_cost_nanos == caller.spent_today > 0
    assert report.model_errors == 0
    assert report.refused_calls == 0


def test_scripted_worlds_report_no_model_use():
    from agenteconomy.chain.vectors import sample_keys
    from agenteconomy.economy.agents import ROSTER
    from agenteconomy.economy.world import Member, World, WorldConfig, economy_params

    members = [Member(cls(name), sample_keys(name)) for name, cls in ROSTER]
    params = economy_params([(m.keys.public_hex, 1000) for m in members], difficulty_bits=6)
    world = World(WorldConfig(params, seed="s", hash_budget=64), members)
    report = world.run_day()
    assert report.model_calls == 0 and report.model_cost_nanos == 0
    assert world.last_day_record is None


def test_daily_cost_cap_holds_over_a_whole_day():
    # A cap too low for the day: agents stop calling, and the day still settles.
    world, _, caller = make_world(cap_nanos=NANOS_PER_DOLLAR // 100)
    report = world.run_day()
    assert 0 < caller.spent_today <= caller.cap_nanos
    assert report.refused_calls > 0
    assert validate_chain(world.blocks, world.config.params) == world.state
    reasons = [entry.get("reason", "") for entry in world.last_log if entry["kind"] == "agent_decision"]
    assert any("cost cap" in reason for reason in reasons)


def test_normal_days_stay_well_under_the_cap():
    world, _, caller = make_world()
    world.run_day()
    assert caller.spent_today < caller.cap_nanos / 2


def test_worst_case_model_stays_within_the_agent_limits():
    world, model, caller = make_world(behavior=lambda tools, **_: [call(tools[0]["name"])], calls_per_agent=16)
    world.run_day()
    assert all(count <= 16 for count in caller.calls.values())
    assert len(model.requests) <= 4 * 16


def test_action_limit_holds_for_ai_agents():
    world, _, _ = make_world(behavior=lambda tools, **_: [call("write_note", note="n")]
                             if tools[0]["name"] == "write_note" else [call("mine", rationale="r")],
                             turns=10, action_limit=3, mine_limit=None)
    world.run_day()
    outcomes = [entry for entry in world.last_log if entry["kind"] == "agent_action"]
    assert sum(entry["code"] == "action_limit" for entry in outcomes) == 4 * 7


def mines_first_then_offers(tools, messages, **_):
    """What real agents did: use up their mines early, then make offers no later block picks up."""
    if tools[0]["name"] == "write_note":
        return [call("write_note", note="n")]
    digest = json.loads(messages[0]["content"][0]["text"])
    if digest["mines_left_today"]:
        return [call("mine", rationale="Mine first.")]
    return [call("make_offer", to="saver" if digest["you"]["name"] != "saver" else "scammer", give_coins=1,
                 rationale="Then offer.")]


def test_without_closing_blocks_late_offers_are_lost():
    # The real-run failure this fixes, kept as a test so it's clear why closing blocks exist.
    world, _, _ = make_world(behavior=mines_first_then_offers, mine_limit=1, closing_blocks=False)
    report = world.run_day()
    assert report.dropped_pending
    assert report.open_offers == 0


def test_closing_blocks_mine_what_is_left_at_the_end_of_the_day():
    world, _, _ = make_world(behavior=mines_first_then_offers, mine_limit=1)
    report = world.run_day()
    assert report.dropped_pending == []
    assert report.open_offers > 0
    assert any(entry["kind"] == "closing_block" for entry in world.last_log)
    assert validate_chain(world.blocks, world.config.params) == world.state


def test_closing_blocks_cost_no_model_calls():
    world, model, _ = make_world(behavior=mines_first_then_offers, mine_limit=1)
    world.run_day()
    closing = [index for index, entry in enumerate(world.last_log) if entry["kind"] == "closing_block"]
    decisions = [index for index, entry in enumerate(world.last_log) if entry["kind"] == "agent_decision"]
    assert min(closing) > max(decisions)
    assert len(model.requests) <= 4 * 6 + 4


def test_no_closing_block_when_nothing_is_waiting():
    def only_passes(tools, **_):
        return [call("write_note", note="n")] if tools[0]["name"] == "write_note" else \
            [call("pass_turn", rationale="Nothing to do.")]

    world, _, _ = make_world(behavior=only_passes)
    report = world.run_day()
    assert report.new_blocks == 0
    assert not any(entry["kind"] == "closing_block" for entry in world.last_log)


def test_offers_made_one_day_can_be_accepted_the_next():
    world, _, _ = make_world()
    for _ in range(8):
        world.run_day()
    assert world.trades
    offered_at = {tx.id.hex(): block.header.height for block in world.blocks for tx in block.txs if tx.kind == "offer"}
    assert any(trade.height > offered_at[trade.offer_id] for trade in world.trades)


def test_closing_attempts_are_bounded(monkeypatch):
    # If no block can be found, the day must still end, with what's left reported rather than hanging.
    attempts = []
    monkeypatch.setattr(Node, "mine", lambda self, miner, budget: attempts.append(1))
    world, _, _ = make_world(behavior=mines_first_then_offers, mine_limit=0)
    report = world.run_day()
    assert len(attempts) == MAX_CLOSING_ATTEMPTS
    assert report.dropped_pending
    assert report.new_blocks == 0


def test_mining_limit_makes_agents_choose_what_else_to_do():
    # From the first real run: with free unlimited mining, agents did nothing else.
    def always_mine(tools, **_):
        if tools[0]["name"] == "write_note":
            return [call("write_note", note="n")]
        return [call("mine", rationale="r")]

    world, model, _ = make_world(behavior=always_mine, mine_limit=2)
    world.run_day()
    outcomes = [entry for entry in world.last_log if entry["kind"] == "agent_action"]
    assert sum(entry["ok"] for entry in outcomes) == 4 * 2
    assert sum(entry["code"] == "mine_limit" for entry in outcomes) == 4 * 4
    late = [request for request in model.requests if request["tools"][0]["name"] != "write_note"][-1]
    assert "mine" not in {tool["name"] for tool in late["tools"]}


def test_mines_left_counts_down_in_the_digest():
    world, model, _ = make_world(mine_limit=2)
    world.run_day()
    left = [json.loads(request["messages"][0]["content"][0]["text"])["mines_left_today"]
            for request in model.requests if request["tools"][0]["name"] != "write_note"]
    assert set(left) <= {0, 1, 2} and 2 in left and 0 in left


def test_a_model_outage_does_not_stop_the_day():
    def behavior(**_):
        raise ModelError("down", retryable=False, may_have_billed=False)

    world, _, _ = make_world(behavior=behavior)
    report = world.run_day()
    assert report.model_errors > 0
    assert report.new_blocks == 0
    assert report.height == 0
    assert report.tip == world.blocks[-1].header.hash_hex


def test_refusing_to_play_is_a_pass_not_a_failure():
    def behavior(system, tools, **_):
        if "trickster" in system and tools[0]["name"] != "write_note":
            return []
        if tools[0]["name"] == "write_note":
            return [call("write_note", note="n")]
        return [call("mine", rationale="r")]

    world, _, _ = make_world(behavior=behavior)
    world.run_day()
    passes = [entry for entry in world.last_log if entry["kind"] == "agent_decision" and entry["node"] == "scammer"]
    assert passes and all(entry["tool"] == "pass_turn" for entry in passes)


def test_a_retried_day_shares_the_ledger(tmp_path):
    path = tmp_path / "spend.json"
    world, _, first = make_world(ledger=FileLedger(path))
    world.run_day()
    spent = first.ledger.spent(0)

    # The day failed after spending and is run again from the top.
    retry, _, second = make_world(ledger=FileLedger(path))
    retry.run_day()
    assert second.ledger.spent(0) > spent
    assert second.ledger.spent(0) <= second.cap_nanos


def test_memory_facts_match_the_chain():
    world, _, _ = make_world()
    state_before = world.state
    height = world.blocks[-1].header.height
    world.run_day()
    mined = {member.keys.public_hex: 0 for member in world.members}
    for block in world.blocks[height + 1:]:
        mined[block.txs[0].body["to"]] += 1
    for member in world.members:
        address = member.keys.public_hex
        day = member.agent.memory.days[-1]
        assert day["blocks_mined"] == mined[address]
        assert day["coins"] == [state_before.balance(address), world.state.balance(address)]
        assert day["net_worth"][1] == net_worth(world.state, address)


def test_memory_records_each_agents_own_failures():
    def fails_for_the_scammer(tools, system, **_):
        if tools[0]["name"] == "write_note":
            return [call("write_note", note="n")]
        if "trickster" in system:
            return [call("make_offer", to="saver", give_creatures=[], give_coins=0, rationale="Offers nothing.")]
        return [call("pass_turn", rationale="Nothing to do.")]

    world, _, _ = make_world(behavior=fails_for_the_scammer)
    world.run_day()
    days = {member.agent.name: member.agent.memory.days[-1] for member in world.members}
    assert days["scammer"]["failed_actions"] == {"invalid_offer": 6}
    assert days["saver"]["failed_actions"] == {}


def test_memory_trades_match_the_chain():
    world, _, _ = make_world()
    names = {member.keys.public_hex: member.agent.name for member in world.members}
    while True:
        known = len(world.trades)
        world.run_day()
        if len(world.trades) > known:
            break
    new = world.trades[known:]
    for member in world.members:
        address = member.keys.public_hex
        mine = [trade for trade in new if address in (trade.proposer, trade.acceptor)]
        remembered = member.agent.memory.days[-1]["trades"]
        assert len(remembered) == min(len(mine), 3)
        for trade, entry in zip(mine[-3:], remembered, strict=True):
            proposing = trade.proposer == address
            other = trade.acceptor if proposing else trade.proposer
            assert entry["with"] == names[other]
            assert entry["gave_coins"] == (trade.give_coins if proposing else trade.want_coins)
            assert entry["got_coins"] == (trade.want_coins if proposing else trade.give_coins)
            assert entry["gave_creatures"] == len(trade.give_creatures if proposing else trade.want_creatures)
            assert entry["got_creatures"] == len(trade.want_creatures if proposing else trade.give_creatures)


def test_each_agent_sees_only_its_own_dropped_transactions():
    world, model, _ = make_world(closing_blocks=False)
    while not (world.reports and world.reports[-1].dropped_by_sender):
        world.run_day()
    report = world.reports[-1]
    names = {member.keys.public_hex: member.agent.name for member in world.members}
    expected = {names[address]: count for address, count in report.dropped_by_sender.items()}
    assert sum(report.dropped_by_sender.values()) == len(report.dropped_pending)

    model.requests.clear()
    world.run_day()
    for request in model.requests:
        if request["tools"][0]["name"] == "write_note":
            continue
        data = json.loads(request["messages"][0]["content"][0]["text"])
        assert data["your_transactions_dropped_yesterday"] == expected.get(data["you"]["name"], 0)


def test_same_seed_and_same_model_answers_give_the_same_world():
    first, _, _ = make_world()
    second, _, _ = make_world()
    run(first, 3)
    run(second, 3)
    assert [r.log_digest for r in first.reports] == [r.log_digest for r in second.reports]
    assert first.state == second.state


def test_identical_across_processes_with_different_hash_seeds():
    outputs = set()
    for hash_seed in ("1", "2", "3"):
        result = subprocess.run([sys.executable, "-c", RUN_SCRIPT, str(ROOT / "backend"), str(ROOT / "tests" / "ai")],
                                capture_output=True, text=True, check=True,
                                env={**os.environ, "PYTHONHASHSEED": hash_seed})
        outputs.add(result.stdout)
    assert len(outputs) == 1, outputs


def test_restart_from_a_save_matches_running_straight_through(tmp_path):
    straight, _, _ = make_world()
    run(straight, 6)

    world, _, _ = make_world()
    run(world, 3)
    path = tmp_path / "world.json"
    world.save(path)

    fresh, _, _ = make_world()
    restarted = type(fresh).load(path, fresh.config, fresh.members, ai_run=fresh.ai_run)
    assert restarted.trades == world.trades
    for before, after in zip(world.members, restarted.members, strict=True):
        assert before.agent.memory == after.agent.memory
    run(restarted, 3)

    assert [r.log_digest for r in restarted.reports] == [r.log_digest for r in straight.reports]
    assert restarted.state == straight.state
    assert [m.agent.memory for m in restarted.members] == [m.agent.memory for m in straight.members]


def test_saved_world_has_memories_but_no_keys_or_rationales(tmp_path):
    world, _, _ = make_world()
    world.run_day()
    path = tmp_path / "world.json"
    world.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["memories"]) == set(PERSONALITIES)
    assert "public test seed" not in path.read_text(encoding="utf-8")


def test_trades_rebuilt_from_blocks_are_the_trades_that_happened():
    world, _, _ = make_world()
    run(world, 10)
    assert world.trades
    assert world._trades_from_blocks() == world.trades


def test_recent_trades_and_prices_reach_the_model():
    world, model, _ = make_world()
    while not world.trades:
        world.run_day()
    model.requests.clear()
    world.run_day()
    turn = next(request for request in model.requests if request["tools"][0]["name"] != "write_note")
    data = json.loads(turn["messages"][0]["content"][0]["text"])
    assert "recent_average_prices" in data


def test_real_run_script_refuses_without_the_flag():
    env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_real_days.py"), "--days", "1"],
                            capture_output=True, text=True, env=env, cwd=ROOT)
    assert result.returncode != 0
    assert "--spend-real-money" in result.stderr + result.stdout
