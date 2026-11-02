import hashlib
from collections import Counter
from dataclasses import replace

import pytest
from economy_helpers import ALICE, BOB, CAROL, PARAMS, POOL, Chain, assert_sound, finished

from agenteconomy.chain.block import block_work
from agenteconomy.chain.creature import (
    ARMS,
    COLOURS,
    DETAILS,
    EYE_COUNTS,
    EYES,
    LEGS,
    MAX_STAT,
    MOUTHS,
    NOSES,
    RARITIES,
    SHAPES,
    SPECIES,
    hatch,
)
from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.params import ChainParams, Season
from agenteconomy.chain.state import State, apply_block
from agenteconomy.chain.transaction import make_coinbase
from agenteconomy.chain.validation import make_genesis, validate_block, validate_chain


def hashes(count: int, salt: str = "x") -> list[str]:
    return [hashlib.sha256(f"{salt}{i}".encode()).hexdigest() for i in range(count)]


CREATURES = [hatch(value, SPECIES) for value in hashes(3000)]


def test_there_are_36_species():
    assert len(SPECIES) == 36 == len(set(SPECIES)) == len(SHAPES) * len(COLOURS)


def test_hatching_is_deterministic():
    value = hashes(1)[0]
    assert hatch(value, POOL) == hatch(value, POOL)


def test_different_hashes_give_different_creatures():
    assert len({(c.species, c.arms, c.legs, c.eyes, c.attack, c.speed) for c in CREATURES}) > 2500


def test_species_only_come_from_the_pool():
    assert {hatch(value, POOL).species for value in hashes(200)} == set(POOL)


def test_pool_order_matters_only_through_its_contents():
    # Same hash, one-species pool: the species is forced, and nothing else about the creature changes.
    value = hashes(1)[0]
    assert replace(hatch(value, ("redC",)), species="blueA") == hatch(value, ("blueA",))


@pytest.mark.parametrize("attribute, options", [
    ("arms", ARMS), ("legs", LEGS), ("eyes", EYES), ("mouth", MOUTHS), ("detail", DETAILS), ("species", SPECIES),
    ("nose", NOSES), ("eye_count", EYE_COUNTS),
])
def test_every_option_appears_about_as_often_as_expected(attribute, options):
    counts = Counter(getattr(creature, attribute) for creature in CREATURES)
    assert set(counts) == set(options)
    for option, weight in Counter(options).items():
        expected = len(CREATURES) * weight / len(options)
        assert expected * 0.6 < counts[option] < expected * 1.4, (option, counts[option], expected)


def test_rarities_match_their_odds():
    counts = Counter(creature.rarity for creature in CREATURES)
    assert list(name for name, _, _ in RARITIES) == ["legendary", "rare", "uncommon", "common"]
    assert 0 < counts["legendary"] < 40
    assert 80 < counts["rare"] < 200
    assert 500 < counts["uncommon"] < 800
    assert counts["common"] > 2000


def test_stats_are_in_range_and_mostly_middling():
    for creature in CREATURES:
        for stat in (creature.attack, creature.defense, creature.speed, creature.health):
            assert 3 <= stat <= MAX_STAT
    commons = [creature.attack for creature in CREATURES if creature.rarity == "common"]
    assert 45 < sum(commons) / len(commons) < 57
    assert max(commons) <= 99


def test_rarer_creatures_have_better_stats():
    def average(rarity):
        totals = [creature.stat_total for creature in CREATURES if creature.rarity == rarity]
        return sum(totals) / len(totals)

    assert average("common") < average("uncommon") < average("rare")


def test_a_mined_hash_with_leading_zeros_still_hatches_fairly():
    # Why every roll re-hashes: the top bits of a mined hash are always zero.
    mined = ["0000" + value[4:] for value in hashes(1000)]
    assert len({hatch(value, SPECIES).species for value in mined}) == 36


def test_colour_and_shape_come_from_the_species():
    creature = hatch(hashes(1)[0], ("yellowD",))
    assert creature.colour == "yellow" and creature.shape == "D"


def test_every_mined_block_hatches_one_creature_for_its_miner():
    chain = Chain()
    block = chain.add((), ALICE)
    key = block.header.hash_hex
    assert chain.state.owners[key] == ALICE.public_hex
    assert chain.state.creatures[key] == hatch(key, POOL)
    chain.add((), BOB)
    assert len(chain.state.creatures) == 2


def test_genesis_hatches_nothing():
    assert Chain().state.creatures == {}


def test_a_chain_with_no_seasons_hatches_nothing():
    params = replace(PARAMS, seasons=())
    chain = Chain(params)
    chain.add()
    assert chain.state.creatures == {}


def test_before_the_first_season_blocks_hatch_nothing():
    params = replace(PARAMS, seasons=(Season(3, POOL),))
    chain = Chain(params)
    chain.add()
    chain.add()
    assert chain.state.creatures == {}
    chain.add()
    assert len(chain.state.creatures) == 1


def test_seasons_change_the_pool_at_their_start_height():
    params = replace(PARAMS, seasons=(Season(1, ("blueA",)), Season(3, ("redC",))))
    chain = Chain(params)
    species = []
    for _ in range(4):
        key = chain.add().header.hash_hex
        species.append(chain.state.creatures[key].species)
    assert species == ["blueA", "blueA", "redC", "redC"]
    assert params.species_pool(2) == ("blueA",) and params.species_pool(3) == ("redC",)
    assert params.species_pool(0) is None


@pytest.mark.parametrize("seasons", [
    (Season(0, POOL),),
    (Season(5, POOL), Season(3, POOL)),
    (Season(5, POOL), Season(5, POOL)),
    (Season(1, ()),),
    (Season(1, ("notASpecies",)),),
])
def test_bad_seasons_are_refused(seasons):
    with pytest.raises(ValueError):
        replace(PARAMS, seasons=seasons)


# The soft cap


@pytest.mark.parametrize("cap, base, supply, reward", [
    (100_000, 50, 0, 50),
    (100_000, 50, 50_000, 25),
    (100_000, 50, 90_000, 5),
    (100_000, 50, 99_000, 1),  # the floor keeps mining paying until the cap really is reached
    (100_000, 50, 99_999, 1),
    (100_000, 50, 100_000, 0),
    (100_000, 50, 150_000, 0),
    (1_000, 50, 999, 1),
    (1_000, 500, 900, 50),
    (10, 50, 5, 5),  # the reward never overshoots the room left
])
def test_reward_curve(cap, base, supply, reward):
    params = ChainParams("c", 1, base, ((ALICE.public_hex, 1),), supply_cap=cap)
    assert params.reward_at(supply) == reward


def test_flat_reward_without_a_cap():
    params = ChainParams("c", 1, 50, ((ALICE.public_hex, 1),))
    assert params.reward_at(10**12) == 50


def test_reward_never_rises_as_supply_grows():
    rewards = [PARAMS.reward_at(supply) for supply in range(0, 100_001, 37)]
    assert rewards == sorted(rewards, reverse=True)


@pytest.mark.parametrize("overrides", [{"supply_cap": 0}, {"supply_cap": 2**53}, {"supply_cap": 100},
                                       {"trade_fee_bps": 10_001}, {"trade_fee_bps": -1}, {"difficulty_bits": 256},
                                       {"difficulty_bits": -1}, {"max_block_txs": 0}])
def test_bad_economy_settings_are_refused(overrides):
    with pytest.raises(ValueError):
        replace(PARAMS, **overrides)


def capped_chain() -> Chain:
    return Chain(replace(PARAMS, supply_cap=1_510))


def test_supply_reaches_the_cap_exactly_and_stops():
    chain = capped_chain()
    while chain.params.reward_at(chain.state.supply) > 0:
        chain.add((), BOB)
    assert chain.state.supply == 1_510
    chain.add((), BOB)
    assert chain.state.supply == 1_510
    assert chain.blocks[-1].txs[0].body["amount"] == 0


def test_burned_fees_make_mining_pay_again():
    chain = capped_chain()
    while chain.params.reward_at(chain.state.supply) > 0:
        chain.add((), BOB)
    chain.add([chain.pay(ALICE, CAROL, 1, fee=100)], BOB)
    assert chain.state.supply == 1_410
    block = chain.add((), BOB)
    assert block.txs[0].body["amount"] > 0
    assert_sound(chain.state, chain.params)


def test_a_block_is_judged_by_the_reward_before_it():
    # The reward comes from supply after the parent block, so a burn in this block can't raise this block's reward.
    chain = capped_chain()
    while chain.params.reward_at(chain.state.supply) > 0:
        chain.add((), BOB)
    block = chain.add([chain.pay(ALICE, CAROL, 1, fee=100)], BOB)
    assert block.txs[0].body["amount"] == 0


@pytest.mark.parametrize("change", [1, -1])
def test_a_coinbase_off_by_one_is_rejected(change):
    chain = Chain()
    parent = chain.blocks[-1].header
    block = chain.unmined_next()
    amount = block.txs[0].body["amount"] + change
    bad = finished(replace(block, txs=(make_coinbase(PARAMS, to=CAROL.public_hex, height=1, amount=amount),)))
    with pytest.raises(ChainError) as e:
        validate_block(bad, parent, chain.state, PARAMS)
    assert e.value.code == Code.BAD_COINBASE


def test_zero_coinbase_is_rejected_while_a_reward_is_due():
    chain = Chain()
    block = chain.unmined_next()
    bad = finished(replace(block, txs=(make_coinbase(PARAMS, to=CAROL.public_hex, height=1, amount=0),)))
    with pytest.raises(ChainError) as e:
        validate_block(bad, chain.blocks[-1].header, chain.state, PARAMS)
    assert e.value.code == Code.BAD_COINBASE


def test_negative_coinbase_is_rejected():
    chain = Chain()
    block = chain.unmined_next()
    bad = finished(replace(block, txs=(make_coinbase(PARAMS, to=CAROL.public_hex, height=1, amount=-5),)))
    with pytest.raises(ChainError) as e:
        validate_block(bad, chain.blocks[-1].header, chain.state, PARAMS)
    assert e.value.code == Code.INVALID_AMOUNT


def test_only_a_coinbase_may_be_zero():
    chain = Chain()
    assert chain.code_of(chain.pay(ALICE, BOB, 0)) == Code.INVALID_AMOUNT


def test_zero_fee_chain_handles_a_sender_with_no_balance_entry():
    # A regression: deducting a fee of 0 from an account that had never held coins crashed instead of working.
    chain = Chain(replace(PARAMS, min_fee=0))
    creature = chain.hatch_for(ALICE)
    chain.add([chain.offer(CAROL, ALICE, want_creatures=(creature,), fee=0)], ALICE)
    assert chain.state.balance(CAROL.public_hex) == 0
    assert chain.state.nonce(CAROL.public_hex) == 1


def test_a_whole_chain_replays_to_the_same_economy():
    chain = Chain()
    creature = chain.hatch_for(ALICE)
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([offer])
    chain.add([chain.accept(BOB, offer)])
    assert validate_chain(chain.blocks, PARAMS) == chain.state


def test_apply_block_matches_validate_block():
    chain = Chain()
    state = apply_block(State(), make_genesis(PARAMS), PARAMS)
    for _ in range(3):
        block = chain.add()
        state = apply_block(state, block, PARAMS)
    assert state == chain.state


def test_work_is_unchanged_by_the_economy():
    assert block_work(PARAMS.difficulty_bits) == 16
