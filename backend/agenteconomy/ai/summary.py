"""What the model reads: a fixed-size digest of a View, however many creatures exist.

Long hashes and addresses become short handles and names, which saves tokens and stops mistyped IDs. Handles are
resolved against the view in code, so an unknown one gets an error, never a guess.
"""

from collections import Counter
from typing import Any

from ..chain.state import Offer
from ..economy.market import FULL, Trade
from ..economy.tools import Listing, View

TOP_HOLDINGS = 5
TOP_OFFERS = 6
NOTABLE = 6
PRICE_ROWS = 8
TODAY_ROWS = 8
SHORT_ID = 8


class UnknownHandle(ValueError):
    pass


def creature_handle(creature_id: str) -> str:
    return f"c:{creature_id[:SHORT_ID]}"


def offer_handle(offer_id: str) -> str:
    return f"o:{offer_id[:SHORT_ID]}"


def resolve_creature(view: View, handle: Any) -> str:
    return _resolve(handle, "c:", [listing.creature.id for listing in view.listings])


def resolve_offer(view: View, handle: Any, offers: tuple[Offer, ...]) -> Offer:
    offer_id = _resolve(handle, "o:", [offer.id for offer in offers])
    return next(offer for offer in offers if offer.id == offer_id)


def _resolve(handle: Any, prefix: str, ids: list[str]) -> str:
    if not isinstance(handle, str) or not handle.startswith(prefix):
        raise UnknownHandle(f"{handle!r} isn't a {prefix} handle")
    matches = [item for item in ids if item.startswith(handle[len(prefix):])] if len(handle) > len(prefix) else []
    if len(matches) != 1:
        raise UnknownHandle(f"{handle} doesn't match exactly one item you can see")
    return matches[0]


def name_of(view: View, address: str) -> str:
    return view.names.get(address, address[:SHORT_ID])


def address_of(view: View, name: Any) -> str:
    for address, known in view.names.items():
        if known == name and address != view.me:
            return address
    raise UnknownHandle(f"{name!r} isn't another agent")


def card(view: View, listing: Listing) -> dict[str, Any]:
    creature = listing.creature
    return {"id": creature_handle(creature.id), "species": creature.species, "rarity": creature.rarity,
            "stats": creature.stat_total, "value": listing.value, "owner": name_of(view, listing.owner)}


def _ranked(listings: list[Listing]) -> list[Listing]:
    return sorted(listings, key=lambda listing: (-listing.value, listing.creature.id))


def _side(view: View, coins: int, creatures: tuple[str, ...]) -> dict[str, Any]:
    values = {listing.creature.id: listing for listing in view.listings}
    return {"coins": coins, "creatures": [
        {"id": creature_handle(key), "value": values[key].value if key in values else 0,
         "rarity": values[key].creature.rarity if key in values else "unknown"} for key in creatures]}


def describe_offer(view: View, offer: Offer) -> dict[str, Any]:
    return {"id": offer_handle(offer.id), "from": name_of(view, offer.proposer), "to": name_of(view, offer.to),
            "they_give": _side(view, offer.give_coins, offer.give_creatures),
            "they_want": _side(view, offer.want_coins, offer.want_creatures),
            "expires_in_blocks": offer.expires_at - view.height}


def _offer_worth(view: View, offer: Offer) -> int:
    return offer.give_coins + sum(view.value_of(creature) for creature in offer.give_creatures)


def describe_trade(view: View, trade: Trade) -> dict[str, Any]:
    return {"proposer": name_of(view, trade.proposer), "acceptor": name_of(view, trade.acceptor),
            "proposer_gave": _side(view, trade.give_coins, trade.give_creatures),
            "proposer_got": _side(view, trade.want_coins, trade.want_creatures)}


def digest(view: View, *, today: list[str], memory: dict[str, Any]) -> dict[str, Any]:
    mine = _ranked(view.mine())
    rivals = []
    for address in view.rivals:
        theirs = [listing for listing in view.listings if listing.owner == address]
        rivals.append({"name": name_of(view, address), "coins": view.balances.get(address, 0),
                       "creatures": len(theirs),
                       "net_worth": view.balances.get(address, 0) + sum(listing.value for listing in theirs)})
    offers = sorted(view.offers_to_me, key=lambda offer: (-_offer_worth(view, offer), offer.id))
    return {
        "day": view.day,
        "height": view.height,
        "you": {
            "name": name_of(view, view.me),
            "coins": view.coins,
            "creatures": len(mine),
            "net_worth": view.coins + sum(listing.value for listing in mine),
            "by_rarity": dict(sorted(Counter(listing.creature.rarity for listing in mine).items())),
            "most_valuable": [card(view, listing) for listing in mine[:TOP_HOLDINGS]],
        },
        "offers_to_you": [describe_offer(view, offer) for offer in offers[:TOP_OFFERS]],
        "more_offers_to_you": max(0, len(offers) - TOP_OFFERS),
        "your_open_offers": [describe_offer(view, offer) for offer in view.my_offers[:TOP_OFFERS]],
        "more_of_your_offers": max(0, len(view.my_offers) - TOP_OFFERS),
        "rivals": rivals,
        "notable_creatures_of_rivals": [card(view, listing) for listing in _ranked(view.theirs())[:NOTABLE]],
        "recent_average_prices": dict(list(view.prices.items())[:PRICE_ROWS]),
        "market": {"shocks_today": list(view.shocks), "appraisals_percent": view.conditions.value_bps * 100 // FULL,
                   "mining_budget_percent": view.conditions.hash_budget_bps * 100 // FULL},
        "mines_left_today": view.mines_left,
        # Who wrote, and how many, but never what they said: message text comes only from read_inbox, as data.
        "unread_messages": view.unread_messages,
        "unread_from": sorted({name_of(view, message.sender) for message in view.inbox}),
        "your_transactions_dropped_yesterday": view.dropped_yesterday,
        "today_so_far": today[-TODAY_ROWS:],
        "memory": memory,
    }
