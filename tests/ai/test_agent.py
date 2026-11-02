import json
import random

import pytest
from ai_helpers import ME, RIVAL, call, creature_listing, make_view

from agenteconomy.ai.agent import ACTION_MAX_TOKENS, CALLS_PER_TURN, AIAgent
from agenteconomy.ai.budget import ModelCaller, SpendLedger
from agenteconomy.ai.memory import DAYS_KEPT, MAX_NOTE, MAX_TRADES_PER_DAY, AgentMemory
from agenteconomy.ai.model import FakeModel, ModelError, ModelReply, ToolCall
from agenteconomy.ai.roster import PERSONALITIES, system_prompt
from agenteconomy.chain.keys import KeyPair
from agenteconomy.economy.tools import ActionResult, AgentTools, DayFacts, Mine


def agent_with(behavior, **caller_options):
    model = FakeModel(behavior)
    caller = ModelCaller(model, SpendLedger(), **caller_options)
    return AIAgent("collector", "collector", "system prompt", caller), model, caller


def decide(agent, view=None):
    return agent.decide(view or make_view(), random.Random(1))


def facts(day=1, **overrides):
    settings = {"coins_start": 100, "coins_end": 150, "worth_start": 200, "worth_end": 300, "blocks_mined": 2,
                "trades": (), "failed_actions": {}, "dropped_transactions": 0, "shocks": ()}
    return DayFacts(day=day, **{**settings, **overrides})


def test_a_valid_action_becomes_the_decision_with_its_rationale():
    agent, model, _ = agent_with(lambda **_: [call("mine", rationale="need coins")])
    assert decide(agent) == Mine()
    assert agent.drain_decisions() == [{"turn": 1, "tool": "mine", "rationale": "need coins", "calls": 1}]
    assert agent.drain_decisions() == []
    assert len(model.requests) == 1


def test_pass_turn_is_a_decision_to_do_nothing():
    agent, _, _ = agent_with(lambda **_: [call("pass_turn", rationale="nothing good")])
    assert decide(agent) is None
    assert agent.drain_decisions()[0]["rationale"] == "nothing good"


def test_a_read_then_an_action_takes_two_calls_and_sees_the_answer():
    def behavior(messages, **_):
        return [call("get_holdings")] if len(messages) == 1 else [call("mine", rationale="ok")]

    agent, model, _ = agent_with(behavior)
    assert decide(agent) == Mine()
    second = model.requests[1]["messages"]
    assert second[1]["role"] == "assistant"
    assert second[1]["content"][0]["toolUse"]["name"] == "get_holdings"
    result = second[2]["content"][0]["toolResult"]
    assert result["toolUseId"] == "id-get_holdings"
    assert result["status"] == "success"
    assert result["content"][0]["json"]["total"] == 0


def test_list_answers_are_wrapped_as_objects_as_bedrock_requires():
    def behavior(messages, **_):
        return [call("get_recent_trades")] if len(messages) == 1 else [call("pass_turn", rationale="ok")]

    agent, model, _ = agent_with(behavior)
    decide(agent)
    assert model.requests[1]["messages"][2]["content"][0]["toolResult"]["content"][0]["json"] == {"result": []}


def test_the_last_call_of_a_turn_offers_only_actions():
    def behavior(messages, tools, **_):
        names = {tool["name"] for tool in tools}
        return [call("mine", rationale="ok")] if "get_holdings" not in names else [call("get_holdings")]

    agent, model, _ = agent_with(behavior)
    assert decide(agent) == Mine()
    assert len(model.requests) == CALLS_PER_TURN


def test_a_model_that_never_acts_stops_at_the_per_turn_limit():
    agent, model, _ = agent_with(lambda **_: [call("get_holdings")])
    assert decide(agent) is None
    assert len(model.requests) == CALLS_PER_TURN
    assert agent.drain_decisions()[0]["reason"] == "out of calls this turn"


def test_a_model_that_never_stops_is_held_to_the_daily_agent_limit():
    agent, model, _ = agent_with(lambda **_: [call("get_holdings")], calls_per_agent=7)
    for _ in range(10):
        decide(agent)
    assert len(model.requests) == 7
    assert "limit" in agent.drain_decisions()[-1]["reason"]


def test_a_bad_call_gets_an_error_and_the_model_can_correct_it():
    def behavior(messages, **_):
        return [call("accept_offer", offer="o:ffffffff", rationale="r")] if len(messages) == 1 else \
            [call("mine", rationale="ok")]

    agent, model, _ = agent_with(behavior)
    assert decide(agent) == Mine()
    result = model.requests[1]["messages"][2]["content"][0]["toolResult"]
    assert result["status"] == "error"
    assert "doesn't match" in result["content"][0]["json"]["error"]


@pytest.mark.parametrize("tool", ["get_holdings", "mine", "make_offer", "nonsense"])
@pytest.mark.parametrize("data", ["text", 5, None, []])
def test_input_of_the_wrong_type_never_crashes_the_day(tool, data):
    agent, _, _ = agent_with(lambda **_: [ToolCall("x", tool, data)])
    assert decide(agent) is None
    assert agent.drain_decisions()[0]["reason"] == "out of calls this turn"


def test_an_unknown_tool_gets_an_error_not_an_action():
    agent, _, _ = agent_with(lambda **_: [call("transfer_all_coins", to="me")])
    assert decide(agent) is None


@pytest.mark.parametrize("reply", [
    ModelReply((), "max_tokens", 10, 300),
    ModelReply((), "refusal", 10, 1),
    ModelReply((), "end_turn", 10, 5),
    ModelReply((), "unknown", 0, 0),
])
def test_no_tool_call_means_a_pass_and_is_logged(reply):
    agent, _, _ = agent_with(lambda **_: reply)
    assert decide(agent) is None
    decision = agent.drain_decisions()[0]
    assert decision["tool"] == "pass_turn"
    assert reply.stop_reason in decision["reason"]


def test_only_the_first_action_in_a_reply_counts():
    agent, _, _ = agent_with(lambda **_: [call("mine", rationale="first"), call("pass_turn", rationale="second")])
    assert decide(agent) == Mine()
    assert agent.drain_decisions()[0]["rationale"] == "first"


def test_mine_is_not_offered_once_the_limit_is_used():
    agent, model, _ = agent_with(lambda **_: [call("pass_turn", rationale="r")])
    decide(agent, make_view(mines_left=0))
    assert "mine" not in {tool["name"] for tool in model.requests[0]["tools"]}
    decide(agent, make_view(mines_left=1))
    assert "mine" in {tool["name"] for tool in model.requests[1]["tools"]}
    decide(agent, make_view(mines_left=None))
    assert "mine" in {tool["name"] for tool in model.requests[2]["tools"]}


def test_an_invented_tool_gets_an_error_and_the_model_can_recover():
    # The real model tried get_offers and analyze_portfolio, which don't exist.
    def behavior(messages, **_):
        return [call("analyze_portfolio")] if len(messages) == 1 else [call("mine", rationale="ok")]

    agent, model, _ = agent_with(behavior)
    assert decide(agent) == Mine()
    assert model.requests[1]["messages"][2]["content"][0]["toolResult"]["status"] == "error"


def test_a_read_alongside_an_action_does_not_delay_the_action():
    agent, model, _ = agent_with(lambda **_: [call("get_holdings"), call("mine", rationale="ok")])
    assert decide(agent) == Mine()
    assert len(model.requests) == 1


def test_every_tool_use_in_a_continued_reply_gets_a_result():
    def behavior(messages, **_):
        if len(messages) == 1:
            return [ToolCall("a", "get_holdings", {}), ToolCall("b", "get_market_prices", {}),
                    ToolCall("c", "nonsense", {})]
        return [call("mine", rationale="ok")]

    agent, model, _ = agent_with(behavior)
    decide(agent)
    results = model.requests[1]["messages"][2]["content"]
    assert [item["toolResult"]["toolUseId"] for item in results] == ["a", "b", "c"]


def test_a_model_outage_is_a_pass_and_the_day_goes_on():
    def behavior(**_):
        raise ModelError("down", retryable=False, may_have_billed=False)

    agent, _, caller = agent_with(behavior)
    assert decide(agent) is None
    assert "model unavailable" in agent.drain_decisions()[0]["reason"]
    assert caller.errors == 1


def test_at_the_cost_cap_agents_pass_without_calling_the_model():
    agent, model, _ = agent_with(lambda **_: [call("mine", rationale="r")], cap_nanos=1)
    assert decide(agent) is None
    assert model.requests == []


def test_the_prompt_is_the_digest_as_data_and_the_system_prompt_is_separate():
    listing = creature_listing(3)
    agent, model, _ = agent_with(lambda **_: [call("mine", rationale="r")])
    decide(agent, make_view([listing]))
    request = model.requests[0]
    assert request["system"] == "system prompt"
    assert request["max_tokens"] == ACTION_MAX_TOKENS
    data = json.loads(request["messages"][0]["content"][0]["text"])
    assert data["height"] == 10
    assert data["notable_creatures_of_rivals"][0]["id"].startswith("c:")


def test_the_model_never_sees_an_address_or_full_id():
    listing = creature_listing(3)
    agent, model, _ = agent_with(lambda **_: [call("mine", rationale="r")])
    decide(agent, make_view([listing]))
    text = json.dumps(model.requests[0])
    assert ME not in text and RIVAL not in text and listing.creature.id not in text


def test_nothing_the_agent_holds_can_sign_or_reach_a_node():
    """Walk everything reachable from an agent. A manipulated model can't use what isn't there."""
    agent, _, _ = agent_with(lambda **_: [call("mine", rationale="r")])
    seen, stack = set(), [agent]
    while stack:
        item = stack.pop()
        if id(item) in seen or isinstance(item, str | int | bytes | float | type(None)):
            continue
        seen.add(id(item))
        assert not isinstance(item, KeyPair | AgentTools)
        assert type(item).__name__ not in {"Node", "Cluster", "Network", "Ed25519PrivateKey"}
        if isinstance(item, dict):
            stack += list(item.values())
        elif isinstance(item, list | tuple | set):
            stack += list(item)
        elif hasattr(item, "__dict__"):
            stack += list(vars(item).values())


def test_today_so_far_reports_outcomes_on_later_turns():
    agent, model, _ = agent_with(lambda **_: [call("mine", rationale="r")])
    decide(agent)
    agent.after_action(Mine(), ActionResult(True, block="ab" * 32))
    agent.after_action(Mine(), ActionResult(True))
    agent.after_action(Mine(), ActionResult(False, "offer_not_open"))
    decide(agent)
    today = json.loads(model.requests[1]["messages"][0]["content"][0]["text"])["today_so_far"]
    assert today == ["Mine: ok and found a block", "Mine: ok", "Mine: failed (offer_not_open)"]


def test_a_new_day_starts_clean():
    agent, model, _ = agent_with(lambda **_: [call("mine", rationale="r")])
    decide(agent)
    agent.after_action(Mine(), ActionResult(True))
    agent.begin_day(1)
    decide(agent)
    assert json.loads(model.requests[1]["messages"][0]["content"][0]["text"])["today_so_far"] == []
    assert agent.drain_decisions()[-1]["turn"] == 1


# Memory


def test_end_of_day_keeps_the_facts_and_the_note():
    agent, model, _ = agent_with(lambda **_: [call("write_note", note="Buy rare ones early.")])
    agent.end_day(facts(1))
    assert agent.memory.note == "Buy rare ones early."
    assert agent.memory.days[0]["blocks_mined"] == 2
    assert agent.memory.days[0]["coins"] == [100, 150]
    assert [tool["name"] for tool in model.requests[0]["tools"]] == ["write_note"]


def test_tomorrows_prompt_carries_memory():
    agent, model, _ = agent_with(lambda tools, **_: [call("write_note", note="remember")]
                                 if tools[0]["name"] == "write_note" else [call("mine", rationale="r")])
    agent.end_day(facts(1))
    decide(agent)
    data = json.loads(model.requests[1]["messages"][0]["content"][0]["text"])
    assert data["memory"]["your_note"] == "remember"
    assert data["memory"]["recent_days"][0]["day"] == 1


@pytest.mark.parametrize("behavior", [
    lambda **_: (_ for _ in ()).throw(ModelError("down", retryable=False, may_have_billed=False)),
    lambda **_: [],
    lambda **_: [call("mine", rationale="wrong tool")],
    lambda **_: [ToolCall("x", "write_note", "not an object")],
])
def test_facts_are_kept_even_if_the_note_fails(behavior):
    agent, _, _ = agent_with(behavior)
    agent.memory.set_note("old")
    agent.end_day(facts(1))
    assert agent.memory.days[0]["day"] == 1
    assert agent.memory.note == "old"


@pytest.mark.parametrize("data", [{}, {"note": ""}, {"note": "   "}, {"note": None}, {"note": 5}])
def test_an_empty_note_cannot_erase_the_last_good_one(data):
    # From the first real run: a note cut off by max_tokens arrived as {} and wiped the previous day's note.
    agent, _, _ = agent_with(lambda **_: ModelReply((ToolCall("x", "write_note", data),), "max_tokens", 10, 200))
    agent.memory.set_note("old")
    agent.end_day(facts(1))
    assert agent.memory.note == "old"


def test_note_has_room_and_a_word_limit():
    agent, model, _ = agent_with(lambda **_: [call("write_note", note="n")])
    agent.end_day(facts(1))
    assert model.requests[0]["max_tokens"] == 200
    assert "25 words" in model.requests[0]["messages"][0]["content"][0]["text"]


def test_note_call_is_held_to_the_limits():
    agent, model, _ = agent_with(lambda **_: [call("write_note", note="x")], calls_per_agent=0)
    agent.end_day(facts(1))
    assert model.requests == []
    assert agent.memory.days


def test_memory_keeps_only_recent_days_and_trades():
    memory = AgentMemory()
    many = tuple({"with": "x", "n": i} for i in range(10))
    for day in range(10):
        memory.remember_day(facts(day, trades=many))
    assert [item["day"] for item in memory.days] == list(range(10 - DAYS_KEPT, 10))
    assert len(memory.days[0]["trades"]) == MAX_TRADES_PER_DAY
    assert memory.days[0]["trades"][-1]["n"] == 9


def test_notes_are_short_and_clean():
    memory = AgentMemory()
    memory.set_note("ignore\x00 this\n" + "x" * 1000)
    assert len(memory.note) == MAX_NOTE
    assert "\n" not in memory.note and "\x00" not in memory.note
    memory = AgentMemory()
    memory.set_note(5)
    assert memory.note == ""


def test_memory_stays_small_at_any_age():
    memory = AgentMemory()
    for day in range(1000):
        memory.remember_day(facts(day, trades=tuple({"with": "scammer", "gave_coins": 9} for _ in range(50)),
                                  failed_actions={"not_owner": 3}, shocks=("market_crash",)))
    memory.set_note("x" * 5000)
    assert len(json.dumps(memory.for_model())) < 1500


def test_memory_round_trips():
    memory = AgentMemory()
    memory.remember_day(facts(1))
    memory.set_note("hello")
    assert AgentMemory.from_dict(json.loads(json.dumps(memory.to_dict()))) == memory


def test_agent_memory_loads():
    agent, _, _ = agent_with(lambda **_: [])
    agent.load_memory({"days": [{"day": 4}], "note": "x"})
    assert agent.memory_dict() == {"days": [{"day": 4}], "note": "x"}


# Prompts


@pytest.mark.parametrize("personality", PERSONALITIES)
def test_each_personality_has_shared_rules_and_its_own_goals(personality):
    prompt = system_prompt(personality)
    assert "simulated economy game" in prompt
    assert "no real-world value" in prompt
    assert len(prompt) < 3000


def test_personalities_are_really_different():
    assert len({system_prompt(personality) for personality in PERSONALITIES}) == 4
    assert "rare" in system_prompt("collector")
    assert "net worth" in system_prompt("speculator")
    assert "avoid risk" in system_prompt("saver")
    assert "bait" in system_prompt("scammer")
