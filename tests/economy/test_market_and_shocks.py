import hashlib
from dataclasses import replace

from economy_helpers import ALICE, BOB, CAROL, Chain

from agenteconomy.chain.creature import hatch
from agenteconomy.economy import shocks as s
from agenteconomy.economy.market import (
    FULL,
    NORMAL,
    Conditions,
    appraise,
    average_prices,
    net_worth,
    trades_in_block,
)

CREATURE = hatch("ab" * 32, ("blueA",))
POOL = ("blueA", "redC")


# Appraisal


def test_appraisal_rises_with_stats_and_rarity():
    common = replace(CREATURE, rarity="common", attack=50, defense=50, speed=50, health=50)
    assert appraise(common) == 100
    assert appraise(replace(common, attack=90)) == 120
    assert appraise(replace(common, rarity="uncommon")) == 150
    assert appraise(replace(common, rarity="rare")) == 300
    assert appraise(replace(common, rarity="legendary")) == 800


def test_conditions_scale_appraisals():
    common = replace(CREATURE, rarity="common", attack=50, defense=50, speed=50, health=50)
    assert appraise(common, Conditions(value_bps=4_000)) == 40
    assert appraise(common, Conditions(species_bps={"blueA": 20_000})) == 200
    assert appraise(common, Conditions(species_bps={"redC": 20_000})) == 100


def test_nothing_appraises_as_free():
    weak = replace(CREATURE, rarity="common", attack=1, defense=0, speed=0, health=0)
    assert appraise(weak, Conditions(value_bps=1)) == 1


def test_net_worth_counts_coins_and_creatures():
    chain = Chain()
    creature = chain.hatch_for(ALICE)
    expected = chain.state.balance(ALICE.public_hex) + appraise(chain.state.creatures[creature])
    assert net_worth(chain.state, ALICE.public_hex) == expected
    assert net_worth(chain.state, BOB.public_hex) == chain.state.balance(BOB.public_hex)


def test_a_crash_lowers_net_worth_but_never_touches_coins():
    chain = Chain()
    chain.hatch_for(ALICE)
    before = chain.state.balance(ALICE.public_hex)
    assert net_worth(chain.state, ALICE.public_hex, Conditions(value_bps=4_000)) < \
        net_worth(chain.state, ALICE.public_hex)
    assert chain.state.balance(ALICE.public_hex) == before


# Trades


def trade_chain():
    chain = Chain()
    creature = chain.hatch_for(ALICE)
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([offer])
    state_before = chain.state.copy()
    block = chain.add([chain.accept(BOB, offer)])
    return chain, creature, offer, block, state_before


def test_accepted_offers_are_trades():
    _, creature, offer, block, state_before = trade_chain()
    [trade] = trades_in_block(block, state_before)
    assert trade.offer_id == offer.id.hex()
    assert trade.proposer == ALICE.public_hex and trade.acceptor == BOB.public_hex
    assert trade.give_creatures == (creature,) and trade.want_coins == 100
    assert trade.height == block.header.height


def test_offers_alone_are_not_trades():
    chain = Chain()
    creature = chain.hatch_for(ALICE)
    state_before = chain.state.copy()
    block = chain.add([chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)])
    assert trades_in_block(block, state_before) == []


def test_offer_accepted_in_its_own_block_is_a_trade():
    chain = Chain()
    creature = chain.hatch_for(ALICE)
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    state_before = chain.state.copy()
    block = chain.add([offer, chain.accept(BOB, offer)])
    assert len(trades_in_block(block, state_before)) == 1


def test_average_prices_use_single_creature_cash_trades_either_way():
    chain, creature, _, block, state_before = trade_chain()
    species = chain.state.creatures[creature].species
    trades = trades_in_block(block, state_before)
    assert average_prices(trades, chain.state) == {species: 100}

    other = chain.hatch_for(BOB)
    species_other = chain.state.creatures[other].species
    buy = chain.offer(CAROL, BOB, give_coins=60, want_creatures=(other,))
    chain.add([buy])
    state_before = chain.state.copy()
    block = chain.add([chain.accept(BOB, buy)])
    more = trades_in_block(block, state_before)
    prices = average_prices(more, chain.state)
    assert prices == {species_other: 60}


def test_swaps_have_no_coin_price():
    chain = Chain()
    mine, yours = chain.hatch_for(ALICE), chain.hatch_for(BOB)
    swap = chain.offer(ALICE, BOB, give_creatures=(mine,), want_creatures=(yours,))
    chain.add([swap])
    state_before = chain.state.copy()
    block = chain.add([chain.accept(BOB, swap)])
    assert average_prices(trades_in_block(block, state_before), chain.state) == {}


# Shocks


def tip(day: int) -> str:
    return hashlib.sha256(f"tip{day}".encode()).hexdigest()


def history(days: int, seed: str = "seed"):
    shocks = []
    for day in range(days):
        shocks += s.roll_shocks(seed, day, tip(day), POOL)
    return shocks


def test_shocks_are_reproducible():
    assert history(500) == history(500)


def test_shocks_depend_on_the_seed():
    assert history(500, "a") != history(500, "b")


def test_shocks_cannot_be_computed_without_the_previous_tip():
    """Tomorrow's shocks depend on today's tip, so nobody can look them up while today is still running."""
    with_tips = [s.roll_shocks("seed", 5, tip(n), POOL) for n in range(2000)]
    assert any(with_tips) and not all(with_tips)


def test_crash_is_rare_and_close_to_its_odds():
    days = 36_500
    crashes = [shock for shock in history(days) if shock.kind == s.MARKET_CRASH]
    assert 70 < len(crashes) < 130  # about 100 expected, one a year
    assert s.ODDS[s.MARKET_CRASH] == 365


def test_other_shocks_are_close_to_their_odds():
    days = 18_000
    shocks = history(days)
    for kind, odds in s.ODDS.items():
        count = sum(shock.kind == kind for shock in shocks)
        assert days / odds * 0.6 < count < days / odds * 1.4, kind


def test_craze_picks_a_species_from_the_pool_and_others_pick_none():
    shocks = history(5000)
    assert {shock.species for shock in shocks if shock.kind == s.COLLECTOR_CRAZE} == set(POOL)
    assert {shock.species for shock in shocks if shock.kind != s.COLLECTOR_CRAZE} == {None}


def test_a_normal_day_is_normal():
    assert s.conditions_for(10, []) == NORMAL


def test_crash_drops_values_then_recovers_over_a_week():
    crash = s.Shock(s.MARKET_CRASH, 10)
    values = [s.conditions_for(day, [crash]).value_bps for day in range(10, 19)]
    assert values[0] == s.CRASH_VALUE_BPS
    assert values == sorted(values)
    assert values[6] < FULL
    assert values[7] == FULL
    assert values[8] == FULL


def test_shocks_do_nothing_before_they_happen():
    assert s.conditions_for(9, [s.Shock(s.MARKET_CRASH, 10)]) == NORMAL


def test_overlapping_crashes_take_the_worse_value():
    shocks = [s.Shock(s.MARKET_CRASH, 10), s.Shock(s.MARKET_CRASH, 12)]
    assert s.conditions_for(12, shocks).value_bps == s.CRASH_VALUE_BPS


def test_mining_shocks_change_budgets_for_one_day_only():
    boom = s.Shock(s.MINING_BOOM, 3)
    drought = s.Shock(s.MINING_DROUGHT, 5)
    assert s.conditions_for(3, [boom]).hash_budget_bps == s.BOOM_BPS
    assert s.conditions_for(4, [boom]).hash_budget_bps == FULL
    assert s.conditions_for(5, [drought]).hash_budget_bps == s.DROUGHT_BPS
    assert s.conditions_for(6, [drought]).hash_budget_bps == FULL


def test_boom_and_drought_on_one_day_cancel_out():
    shocks = [s.Shock(s.MINING_BOOM, 3), s.Shock(s.MINING_DROUGHT, 3)]
    assert s.conditions_for(3, shocks).hash_budget_bps == FULL


def test_craze_doubles_one_species_for_one_day():
    craze = s.Shock(s.COLLECTOR_CRAZE, 3, "blueA")
    assert s.conditions_for(3, [craze]).species_bps == {"blueA": s.CRAZE_BPS}
    assert s.conditions_for(4, [craze]).species_bps == {}


def test_shock_round_trips_through_a_dict():
    shock = s.Shock(s.COLLECTOR_CRAZE, 3, "blueA")
    assert s.Shock(**shock.to_dict()) == shock
