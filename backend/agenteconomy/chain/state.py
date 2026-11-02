"""State: what the transactions in a chain add up to. Nothing here is stored on-chain; verifiers replay."""

from dataclasses import dataclass, field

from .block import Block
from .canonical import MAX_SAFE_INT
from .creature import Creature, hatch
from .errors import ChainError, Code
from .params import ChainParams
from .transaction import ACCEPT, OFFER, REJECT, TRANSFER, Transaction


@dataclass(frozen=True)
class Offer:
    """An open offer: the terms exactly as signed. Accepting one can only ever execute these."""

    id: str
    proposer: str
    to: str
    give_coins: int
    give_creatures: tuple[str, ...]
    want_coins: int
    want_creatures: tuple[str, ...]
    expires_at: int


@dataclass
class State:
    balances: dict[str, int] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    minted: int = 0
    burned: int = 0
    height: int = 0
    creatures: dict[str, Creature] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    offers: dict[str, Offer] = field(default_factory=dict)

    def copy(self) -> State:
        return State(dict(self.balances), dict(self.nonces), self.minted, self.burned, self.height,
                     dict(self.creatures), dict(self.owners), dict(self.offers))

    def balance(self, address: str) -> int:
        return self.balances.get(address, 0)

    def nonce(self, address: str) -> int:
        return self.nonces.get(address, 0)

    def creatures_of(self, address: str) -> list[Creature]:
        return [self.creatures[key] for key, owner in self.owners.items() if owner == address]

    @property
    def supply(self) -> int:
        return self.minted - self.burned


def start_block(state: State, height: int) -> None:
    """Move to a new block's height. Offers past their expiry close here."""
    state.height = height
    state.offers = {key: offer for key, offer in state.offers.items() if offer.expires_at > height}


def finish_block(state: State, block: Block, params: ChainParams) -> None:
    """Hatch the block's creature. It goes to the miner and can be traded from the next block on."""
    pool = params.species_pool(block.header.height)
    if pool is not None:
        key = block.header.hash_hex
        state.creatures[key] = hatch(key, pool)
        state.owners[key] = block.txs[0].body["to"]


def apply_tx(state: State, tx: Transaction, params: ChainParams) -> None:
    """Apply one structurally valid transaction in place. Checks first, so a refusal changes nothing."""
    if tx.kind not in (TRANSFER, OFFER, ACCEPT, REJECT):
        _apply_mint(state, tx)
        return

    # The nonce must match exactly. This is what stops replays and double spends: only one transaction can ever
    # use a given sender and nonce on a chain.
    if tx.nonce != state.nonce(tx.sender):
        raise ChainError(Code.BAD_NONCE, f"expected {state.nonce(tx.sender)}, got {tx.nonce}")
    if tx.spend > state.balance(tx.sender):
        raise ChainError(Code.INSUFFICIENT_BALANCE, f"needs {tx.spend}, has {state.balance(tx.sender)}")

    match tx.kind:
        case "transfer":
            _apply_transfer(state, tx)
        case "offer":
            _apply_offer(state, tx, params)
        case "accept":
            _apply_accept(state, tx, params)
        case "reject":
            _apply_reject(state, tx)

    state.balances[tx.sender] = state.balance(tx.sender) - tx.fee  # may have no entry yet on a zero-fee chain
    state.burned += tx.fee
    state.nonces[tx.sender] = tx.nonce + 1


def _apply_transfer(state: State, tx: Transaction) -> None:
    to, amount = tx.body["to"], tx.body["amount"]
    state.balances[tx.sender] -= amount
    state.balances[to] = state.balance(to) + amount


def _apply_offer(state: State, tx: Transaction, params: ChainParams) -> None:
    body = tx.body
    if not state.height < body["expires_at"] <= state.height + params.max_offer_ttl:
        raise ChainError(Code.INVALID_OFFER, f"expires_at must be within {params.max_offer_ttl} blocks")
    if sum(offer.proposer == tx.sender for offer in state.offers.values()) >= params.max_open_offers:
        raise ChainError(Code.TOO_MANY_OFFERS)
    _require_owner(state, tx.sender, body["give_creatures"])

    counter_of = body["counter_of"]
    if counter_of is not None:
        original = _open_offer(state, counter_of)
        if original.to != tx.sender or original.proposer != body["to"]:
            raise ChainError(Code.NOT_OFFER_PARTY, "a counter goes back to whoever made the offer")
        del state.offers[counter_of]

    key = tx.id.hex()
    state.offers[key] = Offer(key, tx.sender, body["to"], body["give_coins"], tuple(body["give_creatures"]),
                              body["want_coins"], tuple(body["want_creatures"]), body["expires_at"])


def _apply_accept(state: State, tx: Transaction, params: ChainParams) -> None:
    """All or nothing, and only ever the signed terms. Nothing was escrowed, so everything is checked here."""
    offer = _open_offer(state, tx.body["offer_id"])
    if offer.to != tx.sender:
        raise ChainError(Code.NOT_OFFER_PARTY, "only the recipient can accept")
    _require_owner(state, offer.proposer, offer.give_creatures)
    _require_owner(state, tx.sender, offer.want_creatures)
    if offer.give_coins > state.balance(offer.proposer):
        raise ChainError(Code.INSUFFICIENT_BALANCE, "the proposer can no longer pay")
    if offer.want_coins + tx.fee > state.balance(tx.sender):
        raise ChainError(Code.INSUFFICIENT_BALANCE, f"needs {offer.want_coins + tx.fee}")

    _pay(state, offer.proposer, tx.sender, offer.give_coins, params)
    _pay(state, tx.sender, offer.proposer, offer.want_coins, params)
    for creature in offer.give_creatures:
        state.owners[creature] = tx.sender
    for creature in offer.want_creatures:
        state.owners[creature] = offer.proposer
    del state.offers[offer.id]


def _pay(state: State, payer: str, receiver: str, coins: int, params: ChainParams) -> None:
    """Move coins for a trade, burning the trade fee out of what the receiver gets."""
    if coins:
        burn = coins * params.trade_fee_bps // 10_000
        state.balances[payer] -= coins
        state.balances[receiver] = state.balance(receiver) + coins - burn
        state.burned += burn


def _apply_reject(state: State, tx: Transaction) -> None:
    """The recipient declines, or the proposer withdraws."""
    offer = _open_offer(state, tx.body["offer_id"])
    if tx.sender not in (offer.to, offer.proposer):
        raise ChainError(Code.NOT_OFFER_PARTY)
    del state.offers[offer.id]


def _open_offer(state: State, offer_id: str) -> Offer:
    offer = state.offers.get(offer_id)
    if offer is None:
        raise ChainError(Code.OFFER_NOT_OPEN, offer_id)
    return offer


def _require_owner(state: State, address: str, creatures) -> None:
    for creature in creatures:
        if state.owners.get(creature) != address:
            raise ChainError(Code.NOT_OWNER, creature)


def _apply_mint(state: State, tx: Transaction) -> None:
    to, amount = tx.body["to"], tx.body["amount"]
    if state.minted + amount > MAX_SAFE_INT:
        raise ChainError(Code.SUPPLY_OVERFLOW)
    state.balances[to] = state.balance(to) + amount
    state.minted += amount


def apply_block(state: State, block: Block, params: ChainParams) -> State:
    """Returns the new state and leaves `state` alone, so a failed block has no effect."""
    new_state = state.copy()
    start_block(new_state, block.header.height)
    for tx in block.txs:
        apply_tx(new_state, tx, params)
    finish_block(new_state, block, params)
    return new_state
