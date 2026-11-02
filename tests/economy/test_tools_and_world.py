import dataclasses
import json
import os
import random
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from economy_helpers import ALICE, BOB, PARAMS, assert_sound

from agenteconomy.chain.errors import ChainError
from agenteconomy.chain.keys import KeyPair
from agenteconomy.chain.validation import validate_chain
from agenteconomy.chain.vectors import sample_keys
from agenteconomy.economy.agents import ROSTER, Collector, Saver, Scammer, ScriptedAgent, Speculator
from agenteconomy.economy.market import NORMAL, Conditions
from agenteconomy.economy.shocks import MARKET_CRASH, Shock
from agenteconomy.economy.tools import Accept, ActionResult, AgentTools, Counter, MakeOffer, Mine, Reject, View
from agenteconomy.economy.world import Member, World, WorldConfig, economy_params
from agenteconomy.network.settle import Cluster, SettleError

BITS = 6
RUN_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from agenteconomy.chain.vectors import sample_keys
from agenteconomy.economy.agents import ROSTER
from agenteconomy.economy.world import Member, World, WorldConfig, economy_params
members = [Member(cls(name), sample_keys(name)) for name, cls in ROSTER]
params = economy_params([(m.keys.public_hex, 1000) for m in members], difficulty_bits=6)
world = World(WorldConfig(params, seed="cross", hash_budget=64), members)
for _ in range(5):
    world.run_day()
print(world.blocks[-1].header.hash_hex, [r.log_digest for r in world.reports])
"""


def make_tools(node, keys=ALICE, *, cluster, **overrides):
    settings = {"hash_budget": 1 << 12, "fee": 1, "action_limit": 12, "offer_ttl": 10, "day": 0, "conditions": NORMAL,
                "rivals": (BOB.public_hex,)}
    return AgentTools("agent", keys, node, cluster.log, **{**settings, **overrides})


@pytest.fixture
def cluster():
    return Cluster(PARAMS, seed=1)


@pytest.fixture
def tools(cluster):
    return make_tools(cluster.nodes[0], cluster=cluster)


def mine_until_block(tools):
    while tools.execute(Mine()).block is None:
        tools.actions_used = 0
    tools.actions_used = 0


# The tool layer


def test_mining_uses_the_hash_budget_and_hatches_for_the_agent(tools, cluster):
    mine_until_block(tools)
    node = cluster.nodes[0]
    assert node.height == 1
    assert list(node.state.owners.values()) == [ALICE.public_hex]


def test_hash_budgets_are_equal_and_follow_conditions(cluster):
    calls = []
    node = cluster.nodes[0]
    real_mine = node.mine
    node.mine = lambda miner, budget: calls.append(budget) or real_mine(miner, budget)
    for keys, conditions in ((ALICE, NORMAL), (BOB, NORMAL), (ALICE, Conditions(hash_budget_bps=20_000)),
                             (BOB, Conditions(hash_budget_bps=5_000))):
        make_tools(node, keys, cluster=cluster, conditions=conditions, hash_budget=4096).execute(Mine())
    assert calls == [4096, 4096, 8192, 2048]


def test_view_gives_copies_so_changing_it_changes_nothing(tools, cluster):
    view = tools.view()
    view.balances[ALICE.public_hex] = 10**9
    assert cluster.nodes[0].state.balance(ALICE.public_hex) == 1000
    assert tools.view().balances[ALICE.public_hex] == 1000
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.coins = 5


def test_view_holds_no_key_or_node(tools):
    """A manipulated agent can only ask for what the actions allow, because it holds nothing else."""
    view = tools.view()
    for field in dataclasses.fields(View):
        value = getattr(view, field.name)
        assert not isinstance(value, KeyPair | AgentTools)
        assert "node" not in field.name and "key" not in field.name


def test_every_attempt_is_logged_with_its_outcome(tools, cluster):
    result = tools.execute(Accept("ab" * 32))
    assert result == ActionResult(False, "offer_not_open")
    entry = cluster.log.of_kind("agent_action")[-1]
    assert entry["node"] == "agent" and entry["action"] == "Accept"
    assert entry["ok"] is False and entry["code"] == "offer_not_open"


def test_action_limit_is_enforced_and_logged(cluster):
    tools = make_tools(cluster.nodes[0], cluster=cluster, action_limit=2)
    tools.execute(Accept("ab" * 32))
    tools.execute(Accept("ab" * 32))
    assert tools.execute(Accept("ab" * 32)) == ActionResult(False, "action_limit")
    assert tools.execute(Mine()) == ActionResult(False, "action_limit")
    assert cluster.nodes[0].height == 0
    assert tools.actions_used == 2


def test_failed_actions_still_use_up_the_limit(cluster):
    tools = make_tools(cluster.nodes[0], cluster=cluster, action_limit=1)
    tools.execute(Accept("ab" * 32))
    assert tools.execute(MakeOffer(to=BOB.public_hex, give_coins=1)).code == "action_limit"


def test_make_offer_accept_and_mine_complete_a_trade(cluster):
    node = cluster.nodes[0]
    alice, bob = make_tools(node, ALICE, cluster=cluster), make_tools(node, BOB, cluster=cluster)
    mine_until_block(alice)
    creature = next(iter(node.state.creatures))

    offer = alice.execute(MakeOffer(to=BOB.public_hex, give_creatures=(creature,), want_coins=100))
    assert offer.ok
    mine_until_block(alice)
    assert bob.view().offers_to_me[0].id == offer.tx_id

    assert bob.execute(Accept(offer.tx_id)).ok
    mine_until_block(bob)
    assert node.state.owners[creature] == BOB.public_hex


def test_nonces_run_ahead_of_what_is_mined(tools, cluster):
    node = cluster.nodes[0]
    for coins in (1, 2, 3):
        assert tools.execute(MakeOffer(to=BOB.public_hex, give_coins=coins)).ok
    assert sorted(tx.nonce for tx in node.mempool.waiting()) == [0, 1, 2]


def test_creature_lists_are_put_in_order_for_the_agent(cluster):
    node = cluster.nodes[0]
    tools = make_tools(node, cluster=cluster)
    mine_until_block(tools)
    mine_until_block(tools)
    ids = sorted(node.state.creatures)
    result = tools.execute(MakeOffer(to=BOB.public_hex, give_creatures=(ids[1], ids[0]), want_coins=1))
    assert result.ok


def test_counter_goes_back_to_the_proposer_and_closes_the_original(cluster):
    node = cluster.nodes[0]
    alice, bob = make_tools(node, ALICE, cluster=cluster), make_tools(node, BOB, cluster=cluster)
    first = alice.execute(MakeOffer(to=BOB.public_hex, give_coins=10))
    mine_until_block(alice)

    counter = bob.execute(Counter(first.tx_id, give_coins=0, want_coins=20))
    assert counter.ok
    mine_until_block(alice)
    assert list(node.state.offers) == [counter.tx_id]
    assert node.state.offers[counter.tx_id].to == ALICE.public_hex


def test_counter_of_an_unknown_offer_fails_cleanly(tools):
    assert tools.execute(Counter("ab" * 32, want_coins=1)) == ActionResult(False, "offer_not_open")


def test_reject_works_through_the_tools(cluster):
    node = cluster.nodes[0]
    alice, bob = make_tools(node, ALICE, cluster=cluster), make_tools(node, BOB, cluster=cluster)
    offer = alice.execute(MakeOffer(to=BOB.public_hex, give_coins=10))
    mine_until_block(alice)
    assert bob.execute(Reject(offer.tx_id)).ok
    mine_until_block(alice)
    assert node.state.offers == {}


def test_an_offer_already_answered_is_not_shown_as_open(cluster):
    """Otherwise an agent answers twice, because its first answer hasn't been mined yet."""
    node = cluster.nodes[0]
    alice, bob = make_tools(node, ALICE, cluster=cluster), make_tools(node, BOB, cluster=cluster)
    to_accept = alice.execute(MakeOffer(to=BOB.public_hex, give_coins=1)).tx_id
    to_reject = alice.execute(MakeOffer(to=BOB.public_hex, give_coins=2)).tx_id
    to_counter = alice.execute(MakeOffer(to=BOB.public_hex, give_coins=3)).tx_id
    withdrawn = alice.execute(MakeOffer(to=BOB.public_hex, give_coins=4)).tx_id
    mine_until_block(alice)
    assert len(bob.view().offers_to_me) == 4

    bob.execute(Accept(to_accept))
    bob.execute(Reject(to_reject))
    bob.execute(Counter(to_counter, want_coins=5))
    assert [offer.id for offer in bob.view().offers_to_me] == [withdrawn]

    alice.execute(Reject(withdrawn))
    assert withdrawn not in [offer.id for offer in alice.view().my_offers]
    assert [offer.id for offer in bob.view().offers_to_me] == [withdrawn]  # alice's withdrawal isn't mined yet


def test_view_describes_the_world(cluster):
    node = cluster.nodes[0]
    tools = make_tools(node, cluster=cluster, day=7, shocks=(MARKET_CRASH,), dropped_yesterday=2)
    mine_until_block(tools)
    view = tools.view()
    assert view.day == 7 and view.height == 1 and view.me == ALICE.public_hex
    assert view.shocks == (MARKET_CRASH,) and view.dropped_yesterday == 2
    assert len(view.mine()) == 1 and view.theirs() == []
    assert view.value_of(view.mine()[0].creature.id) == view.mine()[0].value
    assert view.value_of("ab" * 32) == 0


def test_unknown_action_is_a_programming_error(tools):
    with pytest.raises(TypeError):
        tools.execute("mine")


# Scripted agents


def world_view(cluster, keys, offers=False):
    return make_tools(cluster.nodes[0], keys, cluster=cluster).view()


@pytest.mark.parametrize("agent_class", [Collector, Speculator, Saver, Scammer])
def test_agents_return_only_actions_on_an_empty_world(cluster, agent_class):
    agent = agent_class("x")
    view = world_view(cluster, ALICE)
    for seed in range(50):
        action = agent.decide(view, random.Random(seed))
        assert action is None or isinstance(action, Mine | MakeOffer | Counter | Accept | Reject)


def test_opinions_differ_between_agents_and_never_change(cluster):
    tools = make_tools(cluster.nodes[0], cluster=cluster)
    mine_until_block(tools)
    listing = tools.view().listings[0]
    first = ScriptedAgent("a")
    assert first.opinion(listing) == first.opinion(listing)
    opinions = {ScriptedAgent(f"agent{i}").opinion(listing) for i in range(20)}
    assert len(opinions) > 3
    for opinion in opinions:
        assert listing.value * 69 // 100 <= opinion <= listing.value * 131 // 100


def test_cautious_agent_ignores_an_offer_it_can_see_is_bait(cluster):
    node = cluster.nodes[0]
    scammer, saver = make_tools(node, ALICE, cluster=cluster), make_tools(node, BOB, cluster=cluster)
    scammer.execute(MakeOffer(to=BOB.public_hex, give_coins=10**6))
    mine_until_block(scammer)
    assert Saver("saver").best_offer(saver.view()) is None


def test_agent_takes_a_good_offer_it_can_pay_for(cluster):
    node = cluster.nodes[0]
    alice, bob = make_tools(node, ALICE, cluster=cluster), make_tools(node, BOB, cluster=cluster)
    offer = alice.execute(MakeOffer(to=BOB.public_hex, give_coins=500))
    mine_until_block(alice)
    assert Saver("saver").best_offer(bob.view()).id == offer.tx_id


def test_agent_passes_on_an_offer_below_its_bar(cluster):
    node = cluster.nodes[0]
    alice, bob = make_tools(node, ALICE, cluster=cluster), make_tools(node, BOB, cluster=cluster)
    alice.execute(MakeOffer(to=BOB.public_hex, give_coins=1))
    mine_until_block(alice)
    assert Saver("saver").best_offer(bob.view()) is None


def test_agents_withdraw_old_offers_before_reaching_the_cap(cluster):
    node = cluster.nodes[0]
    tools = make_tools(node, cluster=cluster, action_limit=100)
    for coins in range(1, 4):
        tools.execute(MakeOffer(to=BOB.public_hex, give_coins=coins))
    mine_until_block(tools)
    agent = ScriptedAgent("x")
    agent.mine_chance = 0
    agent.max_open_offers = 3
    view = tools.view()
    oldest = min(view.my_offers, key=lambda offer: (offer.expires_at, offer.id))
    assert agent.decide(view, random.Random(1)) == Reject(oldest.id)
    agent.max_open_offers = 4
    assert agent.decide(view, random.Random(1)) is None


def test_mine_chance_controls_mining():
    agent = ScriptedAgent("x")
    agent.mine_chance = 1
    assert isinstance(agent.decide(None, random.Random(1)), Mine)


# Whole worlds


def members():
    return [Member(agent(name), sample_keys(name)) for name, agent in ROSTER]


def config(seed="world", **overrides):
    settings = {"hash_budget": 1 << BITS}
    roster = members()
    allocations = [(member.keys.public_hex, 1000) for member in roster]
    allocations.append((sample_keys("treasury").public_hex, 5000))
    params = economy_params(allocations, chain_id="test-world", difficulty_bits=BITS)
    return WorldConfig(params, seed=seed, **{**settings, **overrides})


def assert_world_sound(world):
    state = world.state
    assert_sound(state, world.config.params)
    assert validate_chain(world.blocks, world.config.params) == state
    assert len(state.creatures) == world.blocks[-1].header.height  # one per mined block, none left from forks
    assert set(state.creatures) == {block.header.hash_hex for block in world.blocks[1:]}


def test_multi_day_run_settles_every_day():
    world = World(config(), members())
    for _ in range(10):
        report = world.run_day()
        assert report.settle_rounds >= 1
    assert_world_sound(world)
    assert world.day == 10
    assert world.blocks[-1].header.height == sum(r.new_blocks for r in world.reports)


def test_agents_really_trade_and_fail_and_fork():
    # Guards the simulation itself: it means little if nothing happens in it.
    world = World(config("busy"), members())
    for _ in range(25):
        world.run_day()
    reports = world.reports
    assert sum(r.trades for r in reports) > 0
    assert sum(r.invalid_actions for r in reports) > 0
    assert sum(r.reorgs for r in reports) > 0
    assert sum(r.orphaned for r in reports) > 0
    assert sum(r.new_blocks for r in reports) > 50
    assert world.state.burned > 0
    assert_world_sound(world)


def test_day_reports_add_up():
    world = World(config(), members())
    for _ in range(5):
        report = world.run_day()
        assert report.height == world.blocks[-1].header.height
        assert report.tip == world.blocks[-1].header.hash_hex
        assert report.supply == world.state.supply
        assert report.creatures == len(world.state.creatures)
        assert report.open_offers == len(world.state.offers)
        assert report.invalid_actions == sum(report.invalid_by_code.values())
        assert report.invalid_actions <= report.actions
    assert sum(r.trades for r in world.reports) == len(world.trades)


def test_same_seed_gives_the_same_world():
    first, second = World(config(), members()), World(config(), members())
    for _ in range(4):
        assert first.run_day().log_digest == second.run_day().log_digest
    assert first.state == second.state


def test_different_seeds_give_different_worlds():
    first, second = World(config("a"), members()), World(config("b"), members())
    assert first.run_day().log_digest != second.run_day().log_digest


def test_identical_across_processes_with_different_hash_seeds():
    backend = str(Path(__file__).resolve().parents[2] / "backend")
    outputs = set()
    for hash_seed in ("1", "2", "3"):
        run = subprocess.run([sys.executable, "-c", RUN_SCRIPT, backend], capture_output=True, text=True, check=True,
                             env={**os.environ, "PYTHONHASHSEED": hash_seed})
        outputs.add(run.stdout)
    assert len(outputs) == 1, outputs


def test_restarting_from_a_save_matches_running_straight_through(tmp_path):
    straight = World(config(), members())
    for _ in range(6):
        straight.run_day()

    world = World(config(), members())
    for _ in range(3):
        world.run_day()
    path = tmp_path / "world.json"
    world.save(path)
    restarted = World.load(path, config(), members())
    assert restarted.day == 3
    for _ in range(3):
        restarted.run_day()

    assert restarted.state == straight.state
    assert [r.log_digest for r in restarted.reports] == [r.log_digest for r in straight.reports]
    assert restarted.shocks == straight.shocks


def test_restart_every_day_matches_too(tmp_path):
    straight = World(config(), members())
    for _ in range(4):
        straight.run_day()

    path = tmp_path / "world.json"
    World(config(), members()).save(path)
    for _ in range(4):
        world = World.load(path, config(), members())
        world.run_day()
        world.save(path)
    assert World.load(path, config(), members()).state == straight.state


def test_saved_file_contains_no_keys(tmp_path):
    world = World(config(), members())
    world.run_day()
    path = tmp_path / "world.json"
    world.save(path)
    text = path.read_text(encoding="utf-8")
    assert "public test seed" not in text
    assert json.loads(text).keys() == {"blocks", "reports", "shocks"}


def test_load_rebuilds_state_from_the_blocks(tmp_path):
    world = World(config(), members())
    world.run_day()
    path = tmp_path / "world.json"
    world.save(path)
    assert World.load(path, config(), members()).state == world.state


def test_a_damaged_save_is_noticed_on_load(tmp_path):
    world = World(config(), members())
    world.run_day()
    world.run_day()
    path = tmp_path / "world.json"
    world.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["blocks"][1]["txs"][0]["body"]["amount"] += 1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ChainError):
        World.load(path, config(), members())


def test_unmined_transactions_are_dropped_and_reported():
    world = World(config("drops", turn_jitter_ms=0), members())
    for _ in range(12):
        world.run_day()
    dropped = [tx for report in world.reports for tx in report.dropped_pending]
    assert dropped, "expected something left unmined at the end of some day"


class MinesOnly(ScriptedAgent):
    mine_chance = 1.0


def test_a_dropped_transaction_is_not_mined_the_next_day():
    # Agents here stop submitting anything new. An agent could sign the identical transaction again, which is a new
    # decision, so this has to rule that out to show that yesterday's queue itself is gone.
    world = World(config("drops"), members())
    while not (world.reports and world.reports[-1].dropped_pending):
        world.run_day()
    dropped = set(world.reports[-1].dropped_pending)
    height = world.blocks[-1].header.height

    world.members[:] = [Member(MinesOnly(member.agent.name), member.keys) for member in world.members]
    report = world.run_day()
    assert report.new_blocks > 0
    mined = {tx.id.hex() for block in world.blocks[height + 1:] for tx in block.txs}
    assert not mined & dropped
    assert report.dropped_pending == []


def test_agents_are_told_how_many_of_their_transactions_were_dropped():
    world = World(config("drops"), members())
    seen = []
    real = Collector.decide

    def spy(self, view, rng):
        seen.append(view.dropped_yesterday)
        return real(self, view, rng)

    Collector.decide = spy
    try:
        for _ in range(8):
            world.run_day()
    finally:
        Collector.decide = real
    assert seen[0] == 0
    assert any(seen)


def test_treasury_funds_a_new_agent_with_an_ordinary_visible_transfer():
    treasury = sample_keys("treasury")
    world = World(config(), members(), treasury)
    world.run_day()
    newcomer = sample_keys("newcomer")
    assert world.state.balance(newcomer.public_hex) == 0

    world.add_member(Saver("newcomer"), newcomer, 700)
    grants = 0
    for _ in range(3):
        world.run_day()
    for block in world.blocks:
        grants += sum(tx.kind == "transfer" and tx.sender == treasury.public_hex for tx in block.txs)
    assert grants == 1
    assert world.state.balance(newcomer.public_hex) > 0
    assert any(entry["node"] == "newcomer" for entry in world.last_log)
    assert_world_sound(world)


def test_new_agents_need_the_treasury_key():
    world = World(config(), members())
    with pytest.raises(ValueError):
        world.add_member(Saver("newcomer"), sample_keys("newcomer"), 700)


def test_shocks_apply_on_their_day_and_are_recorded():
    # Find a seed with a shock in the first days, so this doesn't depend on a lucky constant.
    for attempt in range(200):
        world = World(config(f"shock-{attempt}"), members())
        for _ in range(6):
            world.run_day()
        if world.shocks:
            break
    assert world.shocks
    for shock in world.shocks:
        assert shock.to_dict() in world.reports[shock.day].shocks


def test_a_crash_changes_appraisals_but_no_balance_or_chain_rule():
    seen = []
    real = Saver.decide

    def spy(self, view, rng):
        seen.append((view.day, view.conditions.value_bps))
        return real(self, view, rng)

    world = World(config(), members())
    world.shocks.append(Shock(MARKET_CRASH, 1))
    Saver.decide = spy
    try:
        world.run_day()
        world.run_day()
    finally:
        Saver.decide = real
    assert {value for day, value in seen if day == 1} == {4_000}
    assert {value for day, value in seen if day == 0} == {10_000}
    assert_world_sound(world)  # the same rules still hold, and the chain replays with no knowledge of any shock


def test_settle_failure_leaves_the_world_on_the_last_good_day(monkeypatch):
    world = World(config(), members())
    world.run_day()
    state, blocks, reports = world.state, world.blocks, list(world.reports)

    def fail(self, **kwargs):
        raise SettleError("nodes disagree")

    monkeypatch.setattr(Cluster, "settle", fail)
    with pytest.raises(SettleError):
        world.run_day()
    assert world.state == state and world.blocks == blocks and world.reports == reports


def test_economy_params_defaults():
    params = economy_params([(ALICE.public_hex, 1000)])
    assert params.supply_cap == 100_000 and params.block_reward == 50 and params.trade_fee_bps == 200
    assert params.difficulty_bits == 16 and params.min_fee == 1
    assert len(params.species_pool(1)) == 8
    assert replace(params, chain_id="x").chain_id == "x"
