import json
from dataclasses import replace

import pytest
from ai_helpers import ME, RIVAL, creature_listing, make_view

from agenteconomy.ai import summary as s
from agenteconomy.ai import tool_schemas as t
from agenteconomy.chain.state import Offer
from agenteconomy.economy.market import Conditions, Trade
from agenteconomy.economy.tools import Accept, Counter, MakeOffer, Mine, Reject


def offer(index=1, to=ME, proposer=RIVAL, give_coins=10, give=(), want_coins=0, want=(), expires_at=30):
    return Offer(f"{index:02x}" * 32, proposer, to, give_coins, tuple(give), want_coins, tuple(want), expires_at)


def digest_of(view, today=(), memory=None):
    return s.digest(view, today=list(today), memory=memory or {})


# Digest


def test_digest_stays_small_however_many_creatures_exist():
    listings = [creature_listing(i % 250, owner=ME if i % 2 else RIVAL, value=i) for i in range(5000)]
    offers = [offer(i) for i in range(48)]
    view = make_view(listings, offers, offers[:16], prices={f"s{i}": i for i in range(36)})
    text = json.dumps(digest_of(view, today=[f"action {i}" for i in range(50)]))
    assert len(text) < 9000
    data = json.loads(text)
    assert len(data["you"]["most_valuable"]) == s.TOP_HOLDINGS
    assert len(data["notable_creatures_of_rivals"]) == s.NOTABLE
    assert len(data["offers_to_you"]) == s.TOP_OFFERS
    assert data["more_offers_to_you"] == 42
    assert data["more_of_your_offers"] == 10
    assert len(data["recent_average_prices"]) == s.PRICE_ROWS
    assert len(data["today_so_far"]) == s.TODAY_ROWS


def test_digest_size_does_not_grow_with_the_creature_count():
    def size(count):
        listings = [creature_listing(i % 250, owner=ME if i % 2 else RIVAL) for i in range(count)]
        return len(json.dumps(digest_of(make_view(listings))))

    assert size(5000) - size(100) < 50


def test_digest_tells_the_truth():
    mine = creature_listing(1, owner=ME, value=300, rarity="rare")
    theirs = creature_listing(2, owner=RIVAL, value=200)
    view = make_view([mine, theirs], [offer(give_coins=7)], coins=500, shocks=("market_crash",),
                     conditions=Conditions(value_bps=4_000, hash_budget_bps=5_000), dropped_yesterday=2)
    data = digest_of(view, today=["Mine: ok"], memory={"note": "hi"})
    assert data["you"]["coins"] == 500
    assert data["you"]["creatures"] == 1
    assert data["you"]["net_worth"] == 800
    assert data["you"]["by_rarity"] == {"rare": 1}
    assert data["you"]["most_valuable"][0]["id"] == s.creature_handle(mine.creature.id)
    assert data["rivals"] == [{"name": "scammer", "coins": 40, "creatures": 1, "net_worth": 240}]
    assert data["notable_creatures_of_rivals"][0]["owner"] == "scammer"
    assert data["offers_to_you"][0]["from"] == "scammer"
    assert data["offers_to_you"][0]["they_give"]["coins"] == 7
    assert data["offers_to_you"][0]["expires_in_blocks"] == 20
    assert data["market"] == {"shocks_today": ["market_crash"], "appraisals_percent": 40, "mining_budget_percent": 50}
    assert data["your_transactions_dropped_yesterday"] == 2
    assert data["mines_left_today"] is None
    assert data["today_so_far"] == ["Mine: ok"]
    assert data["memory"] == {"note": "hi"}


def test_holdings_and_notables_are_the_most_valuable():
    listings = [creature_listing(i, owner=ME, value=i) for i in range(1, 9)]
    values = [card["value"] for card in digest_of(make_view(listings))["you"]["most_valuable"]]
    assert values == [8, 7, 6, 5, 4]


def test_best_offers_come_first():
    offers = [offer(1, give_coins=5), offer(2, give_coins=50), offer(3, give_coins=9)]
    ids = [item["they_give"]["coins"] for item in digest_of(make_view((), offers))["offers_to_you"]]
    assert ids == [50, 9, 5]


def test_digest_does_not_tell_the_agent_whether_an_offer_is_bait():
    # Rivals' coins are listed, so a careful agent can work it out. Whether it bothers is the test of its judgment.
    text = json.dumps(digest_of(make_view((), [offer(give_coins=10**6)])))
    assert "can_pay" not in text
    assert '"coins": 40' in text


def test_digest_contains_no_full_ids_or_addresses():
    listing = creature_listing(7, owner=RIVAL)
    text = json.dumps(digest_of(make_view([listing], [offer(9, give=(listing.creature.id,))])))
    assert ME not in text and RIVAL not in text and listing.creature.id not in text


def test_a_creature_offered_but_not_visible_is_marked_unknown():
    side = s._side(make_view(), 0, ("ab" * 32,))
    assert side["creatures"][0] == {"id": "c:abababab", "value": 0, "rarity": "unknown"}


def test_trades_are_described_with_names():
    trade = Trade(5, "aa" * 32, RIVAL, ME, 10, (), 0, ())
    described = s.describe_trade(make_view(), trade)
    assert described["proposer"] == "scammer" and described["acceptor"] == "collector"
    assert described["proposer_gave"]["coins"] == 10


# Handles


def test_handles_resolve_exactly():
    listing = creature_listing(3)
    view = make_view([listing])
    assert s.resolve_creature(view, s.creature_handle(listing.creature.id)) == listing.creature.id
    assert s.resolve_offer(view, "o:01010101", (offer(1),)).id == offer(1).id


@pytest.mark.parametrize("handle", ["c:ffffffff", "o:03030303", "c:", "", None, 5, "03030303", ["c:03030303"]])
def test_unknown_handles_are_errors_not_guesses(handle):
    with pytest.raises(s.UnknownHandle):
        s.resolve_creature(make_view([creature_listing(3)]), handle)


def test_ambiguous_handle_is_an_error():
    first, second = creature_listing(3), creature_listing(4)
    view = make_view([replace(first, creature=replace(first.creature, id="ab" + "0" * 62)),
                      replace(second, creature=replace(second.creature, id="ab" + "1" * 62))])
    with pytest.raises(s.UnknownHandle):
        s.resolve_creature(view, "c:ab")
    assert s.resolve_creature(view, "c:ab0")


def test_names_resolve_to_other_agents_only():
    view = make_view()
    assert s.address_of(view, "scammer") == RIVAL
    assert s.name_of(view, RIVAL) == "scammer"
    assert s.name_of(view, "ab" * 32) == "abababab"
    with pytest.raises(s.UnknownHandle):
        s.address_of(view, "collector")  # yourself
    for name in ("nobody", RIVAL, None, 5):
        with pytest.raises(s.UnknownHandle):
            s.address_of(view, name)


# Tool calls become Actions


def test_mine_and_pass():
    assert t.parse_action("mine", {"rationale": "why"}, make_view()) == (Mine(), "why")
    assert t.parse_action("pass_turn", {"rationale": "why"}, make_view()) == (None, "why")


def test_make_offer_resolves_names_and_handles():
    listing = creature_listing(3, owner=RIVAL)
    view = make_view([listing])
    action, rationale = t.parse_action("make_offer", {
        "to": "scammer", "give_coins": 5, "want_creatures": [s.creature_handle(listing.creature.id)], "rationale": "r",
    }, view)
    assert action == MakeOffer(to=RIVAL, give_coins=5, want_creatures=(listing.creature.id,))
    assert rationale == "r"


def test_accept_takes_only_an_offer_id_never_terms():
    view = make_view((), [offer(1, give_coins=5)])
    action, _ = t.parse_action("accept_offer", {"offer": "o:01010101", "rationale": "r"}, view)
    assert action == Accept(offer(1).id)
    with pytest.raises(t.ToolInputError):
        t.parse_action("accept_offer", {"offer": "o:01010101", "give_coins": 1, "rationale": "r"}, view)


def test_counter_and_reject():
    view = make_view((), [offer(1)], [offer(2, to=RIVAL, proposer=ME)])
    action, _ = t.parse_action("counter_offer", {"offer": "o:01010101", "want_coins": 9, "rationale": "r"}, view)
    assert action == Counter(offer(1).id, want_coins=9)
    assert t.parse_action("reject_offer", {"offer": "o:01010101", "rationale": "r"}, view)[0] == Reject(offer(1).id)
    assert t.parse_action("reject_offer", {"offer": "o:02020202", "rationale": "r"}, view)[0] == Reject(offer(2).id)


def test_cannot_accept_or_counter_your_own_offer():
    view = make_view((), (), [offer(2, to=RIVAL, proposer=ME)])
    for tool in ("accept_offer", "counter_offer"):
        with pytest.raises(t.ToolInputError):
            t.parse_action(tool, {"offer": "o:02020202", "rationale": "r"}, view)


@pytest.mark.parametrize("name, data", [
    ("mine", {}),
    ("mine", {"rationale": ""}),
    ("mine", {"rationale": "   "}),
    ("mine", {"rationale": 5}),
    ("mine", {"rationale": "r", "extra": 1}),
    ("mine", "not an object"),
    ("mine", 5),
    ("mine", None),
    ("mine", ["rationale"]),
    ("transfer_all_coins", {"rationale": "r"}),
    ("get_holdings", {"rationale": "r"}),
    ("make_offer", {"to": "nobody", "rationale": "r"}),
    ("make_offer", {"to": "scammer", "give_coins": -1, "rationale": "r"}),
    ("make_offer", {"to": "scammer", "give_coins": 1.5, "rationale": "r"}),
    ("make_offer", {"to": "scammer", "give_coins": True, "rationale": "r"}),
    ("make_offer", {"to": "scammer", "give_coins": "5", "rationale": "r"}),
    ("make_offer", {"to": "scammer", "give_creatures": "c:03030303", "rationale": "r"}),
    ("make_offer", {"to": "scammer", "give_creatures": "", "rationale": "r"}),
    ("make_offer", {"to": "scammer", "give_creatures": 5, "rationale": "r"}),
    ("make_offer", {"to": "scammer", "want_creatures": None, "rationale": "r"}),
    ("make_offer", {"to": "scammer", "give_creatures": ["c:ffffffff"], "rationale": "r"}),
    ("make_offer", {"to": "scammer", "memo": "free text", "rationale": "r"}),
    ("accept_offer", {"offer": "o:ffffffff", "rationale": "r"}),
    ("accept_offer", {"rationale": "r"}),
    ("counter_offer", {"offer": "o:ffffffff", "rationale": "r"}),
    ("reject_offer", {"offer": "o:ffffffff", "rationale": "r"}),
])
def test_bad_calls_get_an_error_never_an_action(name, data):
    with pytest.raises(t.ToolInputError):
        t.parse_action(name, data, make_view())


def test_every_action_tool_requires_a_rationale():
    for tool in t.ACTION_TOOLS:
        assert "rationale" in tool["input_schema"]["required"], tool["name"]


def test_tool_list_is_exactly_the_allowed_surface():
    assert {tool["name"] for tool in t.ACTION_TOOLS} == {"mine", "make_offer", "accept_offer", "counter_offer",
                                                        "reject_offer", "pass_turn"}
    assert {tool["name"] for tool in t.READ_TOOLS} == {"get_holdings", "find_creatures", "get_recent_trades",
                                                       "get_market_prices", "get_open_offers"}
    assert t.is_action("mine") and not t.is_action("get_holdings")


def test_tool_hash_changes_with_the_tools(monkeypatch):
    before = t.tools_hash()
    monkeypatch.setattr(t, "NOTE_TOOL", {"name": "other"})
    assert t.tools_hash() != before


def test_rationales_are_cleaned_and_capped():
    _, rationale = t.parse_action("mine", {"rationale": "a\nb\t\x00c​d  " + "x" * 500}, make_view())
    assert rationale.startswith("a b c d x")
    assert len(rationale) == t.MAX_RATIONALE


@pytest.mark.parametrize("value, expected", [
    ("plain", "plain"), ("  spaced   out ", "spaced out"), ("line\nbreak", "line break"), (None, ""), (5, ""),
    ("hid‮den﻿", "hid den"), ("x" * 10, "xxxx"),
])
def test_clean_text(value, expected):
    assert t.clean_text(value, 4 if value == "x" * 10 else 100) == expected


# Read tools


def test_read_tools_answer_from_the_view():
    mine = creature_listing(1, owner=ME, value=5)
    theirs = creature_listing(2, owner=RIVAL, value=9, rarity="rare", species="redC")
    view = make_view([mine, theirs], [offer(1)], [offer(2, to=RIVAL, proposer=ME)], prices={"blueA": 7},
                     recent_trades=(Trade(5, "aa" * 32, RIVAL, ME, 10, (), 0, ()),))
    assert t.answer_read("get_holdings", {}, view)["total"] == 1
    found = t.answer_read("find_creatures", {"owner": "scammer"}, view)
    assert found["creatures"][0]["value"] == 9
    assert [card["value"] for card in t.answer_read("find_creatures", {"owner": "collector"}, view)["creatures"]] == [5]
    assert t.answer_read("find_creatures", {"rarity": "rare"}, view)["total"] == 1
    assert t.answer_read("find_creatures", {"rarity": "legendary"}, view)["total"] == 0
    assert t.answer_read("find_creatures", {"species": "redC"}, view)["total"] == 1
    assert t.answer_read("find_creatures", {}, view)["total"] == 2
    assert t.answer_read("get_market_prices", {}, view) == {"blueA": 7}
    assert len(t.answer_read("get_recent_trades", {}, view)) == 1
    offers = t.answer_read("get_open_offers", {}, view)
    assert len(offers["to_you"]) == 1 and len(offers["from_you"]) == 1


def test_results_are_ranked_and_limited():
    view = make_view([creature_listing(i, owner=ME, value=i) for i in range(1, 40)])
    result = t.answer_read("get_holdings", {"limit": 3}, view)
    assert result["total"] == 39
    assert [card["value"] for card in result["creatures"]] == [39, 38, 37]
    assert len(t.answer_read("get_holdings", {"limit": 500}, view)["creatures"]) == t.MAX_RESULTS
    for bad in (0, -1, "3", True):
        assert len(t.answer_read("get_holdings", {"limit": bad}, view)["creatures"]) == t.MAX_RESULTS


def test_trades_are_limited_to_the_most_recent():
    trades = tuple(Trade(i, "aa" * 32, RIVAL, ME, i, (), 0, ()) for i in range(1, 6))
    result = t.answer_read("get_recent_trades", {"limit": 2}, make_view(recent_trades=trades))
    assert [item["proposer_gave"]["coins"] for item in result] == [4, 5]


@pytest.mark.parametrize("data", ["text", 5, None, ["limit"]])
@pytest.mark.parametrize("tool", [tool["name"] for tool in t.READ_TOOLS])
def test_a_read_with_input_of_the_wrong_type_is_an_error_not_a_crash(tool, data):
    with pytest.raises(t.ToolInputError):
        t.answer_read(tool, data, make_view())


def test_an_action_is_not_a_read():
    with pytest.raises(t.ToolInputError):
        t.answer_read("mine", {}, make_view())
