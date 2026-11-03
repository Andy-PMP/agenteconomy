"""The only way an agent affects the world. Agents see a read-only View and return Actions; this layer holds the keys.

An agent never receives a key, a node or this object, so a manipulated agent can only ask for what these actions
allow. P4 maps the model's tool calls onto the same Actions.
"""

from dataclasses import dataclass, field

from ..chain.creature import Creature
from ..chain.errors import ChainError
from ..chain.keys import KeyPair
from ..chain.state import Offer
from ..chain.transaction import sign_accept, sign_offer, sign_reject
from ..network.network import EventLog
from ..network.node import Node
from .market import FULL, Conditions, Trade, appraise
from .messages import Message, MessageBoard, clean_text


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


@dataclass(frozen=True)
class SendMessage:
    to: str
    text: str


Action = Mine | MakeOffer | Counter | Accept | Reject | SendMessage


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
    dropped_yesterday: int  # this agent's own
    names: dict[str, str] = field(default_factory=dict)  # address to agent name
    prices: dict[str, int] = field(default_factory=dict)  # recent average coin price per species
    recent_trades: tuple[Trade, ...] = ()
    mines_left: int | None = None  # None: no daily limit
    inbox: tuple[Message, ...] = ()  # untrusted, and newest unread only
    unread_messages: int = 0

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
class DayFacts:
    """What really happened to one agent in a day, computed from the settled chain and the action log."""

    day: int
    coins_start: int
    coins_end: int
    worth_start: int
    worth_end: int
    blocks_mined: int
    trades: tuple[dict, ...]
    failed_actions: dict[str, int]
    dropped_transactions: int
    shocks: tuple[str, ...]


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    code: str | None = None
    tx_id: str | None = None
    block: str | None = None
    message: int | None = None


class AgentTools:
    """One agent's tool layer: signs for it, holds it to its limits, and logs every attempt with its outcome."""

    def __init__(self, name: str, keys: KeyPair, node: Node, log: EventLog, *, hash_budget: int, fee: int,
                 action_limit: int, offer_ttl: int, day: int, conditions: Conditions, rivals: tuple[str, ...],
                 shocks: tuple[str, ...] = (), dropped_yesterday: int = 0, names: dict[str, str] | None = None,
                 prices: dict[str, int] | None = None, recent_trades: tuple[Trade, ...] = (),
                 mine_limit: int | None = None, board: MessageBoard | None = None, message_limit: int = 3,
                 max_spend_bps: int | None = None, shown_names: dict[str, str] | None = None):
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
        self._names = names or {}  # real names, for the log
        self._shown_names = shown_names or self._names  # what the agent sees
        self._prices = prices or {}
        self._recent_trades = recent_trades
        self._mine_limit = mine_limit
        self._mines_used = 0
        self._board = board
        self._message_limit = message_limit
        self._messages_sent = 0
        self._max_spend_bps = max_spend_bps
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
            names=dict(self._shown_names),
            prices=dict(self._prices),
            recent_trades=self._recent_trades,
            mines_left=None if self._mine_limit is None else max(0, self._mine_limit - self._mines_used),
            inbox=self._board.inbox(self.address) if self._board else (),
            unread_messages=len(self._board.unread_for(self.address)) if self._board else 0,
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
                         tx=result.tx_id, block=result.block, details=self._details(action, result))
        return result

    def _details(self, action: Action, result: ActionResult) -> dict:
        """What was asked for, so outcomes can be judged from this log alone. Message text stays on the board."""
        match action:
            case MakeOffer():
                return self._terms(self._name_of(action.to), action)
            case Counter():
                original = self._node.state.offers.get(action.offer_id)
                to = self._name_of(original.proposer) if original else None
                return {"offer_id": action.offer_id, **self._terms(to, action)}
            case Accept() | Reject():
                original = self._node.state.offers.get(action.offer_id)
                return {"offer_id": action.offer_id, "proposer": self._name_of(original.proposer) if original else None}
            case SendMessage():
                return {"to": self._name_of(action.to), "message": result.message}
        return {}

    def _terms(self, to: str | None, terms: MakeOffer | Counter) -> dict:
        return {"to": to, "give_coins": terms.give_coins, "give_creatures": len(terms.give_creatures),
                "want_coins": terms.want_coins, "want_creatures": len(terms.want_creatures)}

    def _name_of(self, address: str) -> str:
        return self._names.get(address, address[:8])

    def _over_spend_limit(self, coins: int) -> bool:
        """A fooled agent can only lose so much at once. Exactly the limit is allowed."""
        if self._max_spend_bps is None:
            return False
        return coins * FULL > self._node.state.balance(self.address) * self._max_spend_bps

    def _dispatch(self, action: Action) -> ActionResult:
        match action:
            case Mine():
                # Without a limit, mining is free money, so a rational agent never does anything else.
                if self._mine_limit is not None and self._mines_used >= self._mine_limit:
                    return ActionResult(False, "mine_limit")
                self._mines_used += 1
                block = self._node.mine(self.address, self._hash_budget)
                return ActionResult(True, block=block.header.hash_hex if block else None)
            case MakeOffer():
                if self._over_spend_limit(action.give_coins):
                    return ActionResult(False, "spend_limit")
                return self._make_offer(action.to, action, None)
            case Counter():
                if self._over_spend_limit(action.give_coins):
                    return ActionResult(False, "spend_limit")
                return self._counter(action)
            case SendMessage():
                return self._send_message(action)
            case Accept():
                original = self._node.state.offers.get(action.offer_id)
                if original is not None and self._over_spend_limit(original.want_coins):
                    return ActionResult(False, "spend_limit")
                return self._submit(sign_accept(self._keys, self._node.params.chain_id, offer_id=action.offer_id,
                                                nonce=self._next_nonce(), fee=self._fee))
            case Reject():
                return self._submit(sign_reject(self._keys, self._node.params.chain_id, offer_id=action.offer_id,
                                                nonce=self._next_nonce(), fee=self._fee))
        raise TypeError(f"not an action: {action!r}")

    def _send_message(self, action: SendMessage) -> ActionResult:
        if self._board is None:
            return ActionResult(False, "messaging_off")
        if self._messages_sent >= self._message_limit:
            return ActionResult(False, "message_limit")
        if action.to == self.address or action.to not in self._names:
            return ActionResult(False, "bad_recipient")
        if not clean_text(action.text, 1):
            return ActionResult(False, "empty_message")
        self._messages_sent += 1
        message = self._board.post(day=self._day, time=self._node.now, sender=self.address, to=action.to,
                                   text=action.text)
        return ActionResult(True, message=message.id)

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
