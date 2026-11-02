"""Simple scripted agents, so full simulations cost nothing. P4's AI agents use the same interface.

An agent gets a read-only View and a random stream, and returns one Action or None. It holds no state between turns, so
restarting a run from saved data can't change what it does.

Agents trade because they disagree: each has its own lasting opinion of each creature, around the public appraisal.
"""

import hashlib
import random

from ..chain.state import Offer
from .tools import Accept, Action, Counter, Listing, MakeOffer, Mine, Reject, View


class ScriptedAgent:
    personality = "scripted"
    mine_chance = 0.4
    max_open_offers = 6
    minimum_gain = 5

    def __init__(self, name: str):
        self.name = name

    def decide(self, view: View, rng: random.Random) -> Action | None:
        if rng.random() < self.mine_chance:
            return Mine()
        if len(view.my_offers) >= self.max_open_offers:
            return Reject(min(view.my_offers, key=lambda offer: (offer.expires_at, offer.id)).id)  # withdraw the oldest
        return self.trade(view, rng)

    def trade(self, view: View, rng: random.Random) -> Action | None:
        return None

    def opinion(self, listing: Listing) -> int:
        """What this agent thinks a creature is worth: 70% to 130% of the appraisal, and it never changes its mind."""
        digest = hashlib.sha256(f"{self.name}|{listing.creature.id}".encode()).digest()
        return listing.value * (70 + digest[0] % 61) // 100

    def _opinion_of(self, view: View, creature_id: str) -> int:
        for listing in view.listings:
            if listing.creature.id == creature_id:
                return self.opinion(listing)
        return 0

    def offer_gain(self, view: View, offer: Offer) -> int:
        """How much better off, by this agent's own opinion, accepting an offer leaves it."""
        received = offer.give_coins + sum(self._opinion_of(view, creature) for creature in offer.give_creatures)
        paid = offer.want_coins + sum(self._opinion_of(view, creature) for creature in offer.want_creatures)
        return received - paid

    def best_offer(self, view: View) -> Offer | None:
        """The best offer that could actually work, if it clears this agent's bar. Ties go by ID, to reproduce."""
        mine = {listing.creature.id for listing in view.mine()}
        usable = [offer for offer in view.offers_to_me
                  if offer.want_coins < view.coins and set(offer.want_creatures) <= mine
                  and offer.give_coins <= view.balances.get(offer.proposer, 0)]
        usable.sort(key=lambda offer: (-self.offer_gain(view, offer), offer.id))
        if usable and self.offer_gain(view, usable[0]) >= self.minimum_gain:
            return usable[0]
        return None

    def bid_on(self, view: View, rng: random.Random, margin_percent: int) -> Action | None:
        """Offer coins for a creature this agent likes more than its appraisal suggests."""
        candidates = [listing for listing in view.theirs()
                      if self.opinion(listing) * margin_percent // 100 < view.coins]
        if not candidates:
            return None
        ranked = sorted(candidates, key=lambda listing: (listing.value - self.opinion(listing), listing.creature.id))
        target = rng.choice(ranked[:5])
        return MakeOffer(to=target.owner, give_coins=max(1, self.opinion(target) * margin_percent // 100),
                         want_creatures=(target.creature.id,))

    def list_one(self, view: View, rng: random.Random, margin_percent: int) -> Action | None:
        mine = view.mine()
        if not mine or not view.rivals:
            return None
        listing = rng.choice(mine)
        return MakeOffer(to=rng.choice(view.rivals), give_creatures=(listing.creature.id,),
                         want_coins=max(1, listing.value * margin_percent // 100))


class Collector(ScriptedAgent):
    """Wants rare creatures and will overpay for them without checking the price. The scammer's best customer."""

    personality = "collector"

    def opinion(self, listing: Listing) -> int:
        worth = super().opinion(listing)
        return worth if listing.creature.rarity == "common" else worth * 2

    def trade(self, view, rng):
        mine = {listing.creature.id for listing in view.mine()}
        for offer in view.offers_to_me:
            wants_it = any(listing.creature.id in offer.give_creatures and listing.creature.rarity != "common"
                           for listing in view.listings)
            if wants_it and offer.want_coins < view.coins and set(offer.want_creatures) <= mine:
                return Accept(offer.id)
        if (offer := self.best_offer(view)) is not None:
            return Accept(offer.id)
        return self.bid_on(view, rng, 90)


class Speculator(ScriptedAgent):
    """Buys low, sells high and haggles. It sometimes takes a too-good offer without checking the proposer can pay."""

    personality = "speculator"
    gullible_chance = 0.25

    def trade(self, view, rng):
        if (offer := self.best_offer(view)) is not None:
            return Accept(offer.id)
        if view.offers_to_me:
            offer = rng.choice(sorted(view.offers_to_me, key=lambda offer: offer.id))
            if rng.random() < self.gullible_chance and self.offer_gain(view, offer) > 0 \
                    and offer.want_coins < view.coins:
                return Accept(offer.id)
            if rng.random() < 0.5:
                return self._respond(view, offer)
        if rng.random() < 0.5:
            return self.list_one(view, rng, 115)
        return self.bid_on(view, rng, 85)

    def _respond(self, view, offer):
        if offer.give_coins and offer.want_creatures:
            return Counter(offer.id, give_creatures=offer.want_creatures, want_coins=offer.give_coins * 5 // 4)
        return Reject(offer.id)


class Saver(ScriptedAgent):
    """Mines steadily, rarely trades, and only for a clear win. It sells more often than it buys."""

    personality = "saver"
    mine_chance = 0.5
    minimum_gain = 30

    def trade(self, view, rng):
        if (offer := self.best_offer(view)) is not None:
            return Accept(offer.id)
        unwanted = [offer for offer in view.offers_to_me if self.offer_gain(view, offer) < 0]
        if unwanted and rng.random() < 0.3:
            return Reject(min(offer.id for offer in unwanted))
        if rng.random() < 0.2:
            return self.list_one(view, rng, 100)
        return None


class Scammer(ScriptedAgent):
    """Bait with coins it doesn't have, and creatures at inflated prices. No messages until P5."""

    personality = "scammer"

    def trade(self, view, rng):
        if (offer := self.best_offer(view)) is not None:
            return Accept(offer.id)
        if rng.random() < 0.5:
            return self._bait(view, rng)
        return self.list_one(view, rng, 300)

    def _bait(self, view, rng):
        """Promise far more than it holds. The offer is valid until someone accepts it, then fails at accept."""
        theirs = view.theirs()
        if not theirs:
            return None
        target: Listing = rng.choice(theirs)
        return MakeOffer(to=target.owner, give_coins=view.coins + target.value * 2,
                         want_creatures=(target.creature.id,))


ROSTER = (("collector", Collector), ("speculator", Speculator), ("saver", Saver), ("scammer", Scammer))
