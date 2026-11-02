"""What creatures are worth. Off-chain: appraisals guide agents, but only signed offers ever move anything."""

from dataclasses import dataclass, field

from ..chain.block import Block
from ..chain.creature import Creature
from ..chain.state import State

RARITY_PERCENT = {"common": 100, "uncommon": 150, "rare": 300, "legendary": 800}
FULL = 10_000  # basis points: 10,000 means 100%


@dataclass(frozen=True)
class Conditions:
    """Today's market, set by shocks."""

    value_bps: int = FULL
    species_bps: dict[str, int] = field(default_factory=dict)
    hash_budget_bps: int = FULL


@dataclass(frozen=True)
class Trade:
    height: int
    offer_id: str
    proposer: str
    acceptor: str
    give_coins: int
    give_creatures: tuple[str, ...]
    want_coins: int
    want_creatures: tuple[str, ...]


NORMAL = Conditions()


def appraise(creature: Creature, conditions: Conditions = NORMAL) -> int:
    """Half the stat total, scaled by rarity: an average common is worth about two blocks' reward. Never below 1."""
    value = creature.stat_total * RARITY_PERCENT[creature.rarity] // 200
    value = value * conditions.value_bps // FULL
    value = value * conditions.species_bps.get(creature.species, FULL) // FULL
    return max(1, value)


def net_worth(state: State, address: str, conditions: Conditions = NORMAL) -> int:
    return state.balance(address) + sum(appraise(creature, conditions) for creature in state.creatures_of(address))


def trades_in_block(block: Block, state_before: State) -> list[Trade]:
    """The accepted offers in a block. An offer made and accepted in the same block is found in the block itself."""
    offers = dict(state_before.offers)
    trades = []
    for tx in block.txs:
        if tx.kind == "offer":
            body = tx.body
            offers[tx.id.hex()] = Trade(block.header.height, tx.id.hex(), tx.sender, body["to"], body["give_coins"],
                                        tuple(body["give_creatures"]), body["want_coins"],
                                        tuple(body["want_creatures"]))
        elif tx.kind == "accept":
            offer = offers[tx.body["offer_id"]]
            trades.append(Trade(block.header.height, tx.body["offer_id"], offer.proposer, tx.sender, offer.give_coins,
                                tuple(offer.give_creatures), offer.want_coins, tuple(offer.want_creatures)))
    return trades


def average_prices(trades: list[Trade], state: State) -> dict[str, int]:
    """Average coins paid per species, from trades of one creature for coins. Other trades have no clear price."""
    paid: dict[str, list[int]] = {}
    for trade in trades:
        if len(trade.give_creatures) == 1 and not trade.want_creatures and trade.want_coins:
            paid.setdefault(state.creatures[trade.give_creatures[0]].species, []).append(trade.want_coins)
        elif len(trade.want_creatures) == 1 and not trade.give_creatures and trade.give_coins:
            paid.setdefault(state.creatures[trade.want_creatures[0]].species, []).append(trade.give_coins)
    return {species: sum(prices) // len(prices) for species, prices in sorted(paid.items())}
