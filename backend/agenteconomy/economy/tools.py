"""The only way an agent affects the world. Agents see a read-only View and return Actions; this layer holds the keys.

An agent never receives a key, a node or this object, so a manipulated agent can only ask for what these actions
allow. P4 maps the model's tool calls onto the same Actions.
"""

from dataclasses import dataclass

from ..chain.creature import Creature
from ..chain.errors import ChainError
from ..chain.keys import KeyPair
from ..chain.state import Offer
from ..chain.transaction import sign_accept, sign_offer, sign_reject
from ..network.network import EventLog
from ..network.node import Node
from .market import FULL, Conditions, appraise


@dataclass(frozen=True)
class Mine:
    pass


@dataclass(frozen=True)
class MakeOffer:
    to: str
    give_coins: int = 0
    give_creatures: tuple[str, ...] = ()
    want_coins: int = 0
    want_creatures: tuple[str, ...] = ()


@dataclass(frozen=True)
class Counter:
    offer_id: str
    give_coins: int = 0
    give_creatures: tuple[str, ...] = ()
    want_coins: int = 0
    want_creatures: tuple[str, ...] = ()


@dataclass(frozen=True)
class Accept:
    offer_id: str


@dataclass(frozen=True)
class Reject:
    offer_id: str


Action = Mine | MakeOffer | Counter | Accept | Reject


@dataclass(frozen=True)
class Listing:
    creature: Creature
    owner: str
    value: int


@dataclass(frozen=True)
class View:
    """Everything an agent can read. All of it is copies, so changing it changes nothing."""

    day: int
    height: int
    me: str
    coins: int
    balances: dict[str, int]  # public on-chain, so an agent can see whether an offer's proposer can actually pay
    listings: tuple[Listing, ...]  # every creature, with its owner and today's appraisal
    offers_to_me: tuple[Offer, ...]
    my_offers: tuple[Offer, ...]
    rivals: tuple[str, ...]
    conditions: Conditions
    shocks: tuple[str, ...]
    dropped_yesterday: int

    def mine(self) -> list[Listing]:
        return [listing for listing in self.listings if listing.owner == self.me]

    def theirs(self) -> list[Listing]:
        return [listing for listing in self.listings if listing.owner != self.me]

    def value_of(self, creature_id: str) -> int:
        for listing in self.listings:
            if listing.creature.id == creature_id:
                return listing.value
        return 0


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    code: str | None = None
    tx_id: str | None = None
    block: str | None = None


class AgentTools:
    """One agent's tool layer: signs for it, holds it to its limits, and logs every attempt with its outcome."""

    def __init__(self, name: str, keys: KeyPair, node: Node, log: EventLog, *, hash_budget: int, fee: int,
                 action_limit: int, offer_ttl: int, day: int, conditions: Conditions, rivals: tuple[str, ...],
                 shocks: tuple[str, ...] = (), dropped_yesterday: int = 0):
        self.name = name
        self.address = keys.public_hex
        self._keys = keys
        self._node = node
        self._log = log
        self._hash_budget = hash_budget * conditions.hash_budget_bps // FULL
        self._fee = fee
        self._action_limit = action_limit
        self._offer_ttl = offer_ttl
        self._day = day
        self._conditions = conditions
        self._rivals = rivals
        self._shocks = shocks
        self._dropped_yesterday = dropped_yesterday
        self.actions_used = 0

    def view(self) -> View:
        """The agent's own waiting transactions count: an offer it has already answered isn't shown as still open."""
        state = self._node.state
        answered = {tx.body["offer_id"] for tx in self._node.mempool.waiting()
                    if tx.sender == self.address and "offer_id" in tx.body}
        answered |= {tx.body["counter_of"] for tx in self._node.mempool.waiting()
                     if tx.sender == self.address and tx.body.get("counter_of")}
        open_offers = [offer for offer in state.offers.values() if offer.id not in answered]
        listings = tuple(Listing(creature, state.owners[key], appraise(creature, self._conditions))
                         for key, creature in state.creatures.items())
        return View(
            day=self._day,
            height=state.height,
            me=self.address,
            coins=state.balance(self.address),
            balances=dict(state.balances),
            listings=listings,
            offers_to_me=tuple(offer for offer in open_offers if offer.to == self.address),
            my_offers=tuple(offer for offer in open_offers if offer.proposer == self.address),
            rivals=self._rivals,
            conditions=self._conditions,
            shocks=self._shocks,
            dropped_yesterday=self._dropped_yesterday,
        )

    def execute(self, action: Action) -> ActionResult:
        if self.actions_used >= self._action_limit:
            result = ActionResult(False, "action_limit")
        else:
            self.actions_used += 1
            try:
                result = self._dispatch(action)
            except ChainError as e:
                result = ActionResult(False, str(e.code))
        self._log.record(self.name, "agent_action", action=type(action).__name__, ok=result.ok, code=result.code,
                         tx=result.tx_id, block=result.block)
        return result

    def _dispatch(self, action: Action) -> ActionResult:
        match action:
            case Mine():
                block = self._node.mine(self.address, self._hash_budget)
                return ActionResult(True, block=block.header.hash_hex if block else None)
            case MakeOffer():
                return self._make_offer(action.to, action, None)
            case Counter():
                return self._counter(action)
            case Accept():
                return self._submit(sign_accept(self._keys, self._node.params.chain_id, offer_id=action.offer_id,
                                                nonce=self._next_nonce(), fee=self._fee))
            case Reject():
                return self._submit(sign_reject(self._keys, self._node.params.chain_id, offer_id=action.offer_id,
                                                nonce=self._next_nonce(), fee=self._fee))
        raise TypeError(f"not an action: {action!r}")

    def _counter(self, action: Counter) -> ActionResult:
        original = self._node.state.offers.get(action.offer_id)
        if original is None:
            return ActionResult(False, "offer_not_open")
        return self._make_offer(original.proposer, action, action.offer_id)

    def _make_offer(self, to: str, terms: MakeOffer | Counter, counter_of: str | None) -> ActionResult:
        tx = sign_offer(self._keys, self._node.params.chain_id, to=to, give_coins=terms.give_coins,
                        give_creatures=tuple(sorted(terms.give_creatures)), want_coins=terms.want_coins,
                        want_creatures=tuple(sorted(terms.want_creatures)),
                        expires_at=self._node.state.height + self._offer_ttl, counter_of=counter_of,
                        nonce=self._next_nonce(), fee=self._fee)
        return self._submit(tx)

    def _submit(self, tx) -> ActionResult:
        self._node.submit_tx(tx)
        return ActionResult(True, tx_id=tx.id.hex())

    def _next_nonce(self) -> int:
        waiting = {tx.nonce for tx in self._node.mempool.waiting() if tx.sender == self.address}
        nonce = self._node.state.nonce(self.address)
        while nonce in waiting:
            nonce += 1
        return nonce
