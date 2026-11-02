"""A fake model that plays the game from the same prompts and tools the real one gets, at no cost."""

import json
from dataclasses import replace

from agenteconomy.ai.budget import ModelCaller, SpendLedger
from agenteconomy.ai.model import FakeModel, ToolCall
from agenteconomy.ai.roster import PERSONALITIES, build_members
from agenteconomy.chain.creature import hatch
from agenteconomy.chain.vectors import sample_keys
from agenteconomy.economy.market import NORMAL
from agenteconomy.economy.tools import Listing, View
from agenteconomy.economy.world import World, WorldConfig, economy_params

BITS = 6
ME = sample_keys("collector").public_hex
RIVAL = sample_keys("scammer").public_hex
NAMES = {ME: "collector", RIVAL: "scammer"}


def call(name, **data):
    return ToolCall(f"id-{name}", name, data)


def pure_digest(messages):
    return json.loads(messages[0]["content"][0]["text"])


def plays_the_game(system, messages, tools, max_tokens):
    """Reads the digest and acts, deterministically. Reads something first on some turns, as a real model might."""
    names = {tool["name"] for tool in tools}
    if names == {"write_note"}:
        return [call("write_note", note="Keep mining, and look for offers early.")]
    digest = pure_digest(messages)
    number = digest["height"] + digest["day"] + len(digest["today_so_far"])
    if "get_holdings" in names and number % 5 == 0 and len(messages) == 1:
        return [call("get_holdings", limit=3)]
    if digest["mines_left_today"] == 0 and "mine" not in names:
        return [call("pass_turn", rationale="No mines left, nothing else to do.")]
    if digest["offers_to_you"] and number % 2 == 0:
        return [call("accept_offer", offer=digest["offers_to_you"][0]["id"], rationale="This offer looks useful.")]
    notable = digest["notable_creatures_of_rivals"]
    if notable and number % 3 == 0:
        target = notable[0]
        return [call("make_offer", to=target["owner"], give_coins=max(1, target["value"] // 2),
                     want_creatures=[target["id"]], rationale="Trying to buy this cheaply.")]
    return [call("mine", rationale="Mining earns coins and a creature.")]


def make_world(seed="ai", *, behavior=plays_the_game, ledger=None, calls_per_agent=16, cap_nanos=None, turns=6,
               action_limit=8, mine_limit=3, closing_blocks=True):
    keys = {name: sample_keys(name) for name in PERSONALITIES}
    model = FakeModel(behavior)
    caller = ModelCaller(model, ledger if ledger is not None else SpendLedger(), calls_per_agent=calls_per_agent)
    if cap_nanos is not None:
        caller.cap_nanos = cap_nanos
    members, run = build_members(keys, caller)
    params = economy_params([(key.public_hex, 1000) for key in keys.values()], chain_id="ai-test", difficulty_bits=BITS)
    config = WorldConfig(params, seed=seed, hash_budget=1 << BITS, turns_per_day=turns, action_limit=action_limit,
                         mine_limit=mine_limit, closing_blocks=closing_blocks)
    return World(config, members, ai_run=run), model, caller


def creature_listing(index, owner=RIVAL, value=100, rarity="common", species="blueA"):
    creature_id = f"{index:02x}" * 32
    return Listing(replace(hatch(creature_id, (species,)), rarity=rarity), owner, value)


def make_view(listings=(), offers_to_me=(), my_offers=(), coins=500, **overrides):
    settings = {
        "day": 3, "height": 10, "me": ME, "coins": coins, "balances": {ME: coins, RIVAL: 40},
        "listings": tuple(listings), "offers_to_me": tuple(offers_to_me), "my_offers": tuple(my_offers),
        "rivals": (RIVAL,), "conditions": NORMAL, "shocks": (), "dropped_yesterday": 0, "names": NAMES,
    }
    return View(**{**settings, **overrides})
