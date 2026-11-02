"""The settings every node on a chain must share. The defaults leave the economy off, as a plain chain."""

from dataclasses import dataclass

from .canonical import MAX_SAFE_INT
from .creature import SPECIES


@dataclass(frozen=True)
class Season:
    """From `start_height`, mined blocks hatch one of these species, until the next season starts."""

    start_height: int
    species: tuple[str, ...]


@dataclass(frozen=True)
class ChainParams:
    chain_id: str
    difficulty_bits: int
    block_reward: int
    genesis_allocations: tuple[tuple[str, int], ...]  # (address, starting balance)
    genesis_timestamp: int = 0
    max_block_txs: int = 100  # including the coinbase
    min_fee: int = 0
    supply_cap: int | None = None  # None: block_reward is flat
    trade_fee_bps: int = 0  # share of the coins in an accepted offer that gets burned, in 1/100ths of a percent
    seasons: tuple[Season, ...] = ()  # empty: blocks hatch nothing
    max_offer_creatures: int = 8
    max_offer_ttl: int = 500  # blocks
    max_open_offers: int = 16  # per sender

    def __post_init__(self):
        addresses = [address for address, _ in self.genesis_allocations]
        if not addresses or len(set(addresses)) != len(addresses):
            raise ValueError("genesis needs at least one allocation and no repeated addresses")
        if not 0 <= self.difficulty_bits <= 255:
            raise ValueError("difficulty_bits must be 0 to 255")
        if self.max_block_txs < 1:
            raise ValueError("max_block_txs must leave room for the coinbase")
        if not 0 <= self.trade_fee_bps <= 10_000:
            raise ValueError("trade_fee_bps must be 0 to 10000")
        if self.supply_cap is not None:
            if not 0 < self.supply_cap <= MAX_SAFE_INT:
                raise ValueError("supply_cap must be positive and within the safe integer range")
            if sum(amount for _, amount in self.genesis_allocations) > self.supply_cap:
                raise ValueError("genesis allocations exceed the supply cap")
        self._check_seasons()

    def _check_seasons(self) -> None:
        starts = [season.start_height for season in self.seasons]
        if starts != sorted(set(starts)) or (starts and starts[0] < 1):
            raise ValueError("seasons must start at height 1 or later, in increasing order")
        for season in self.seasons:
            if not season.species or any(species not in SPECIES for species in season.species):
                raise ValueError(f"season at height {season.start_height} needs known species")

    def reward_at(self, supply: int) -> int:
        """The soft cap: the reward shrinks as supply nears the cap and is 0 at it, so supply can never pass it.

        Burned fees lower supply, which makes mining pay again.
        """
        if self.supply_cap is None:
            return self.block_reward
        room = self.supply_cap - supply
        if room <= 0:
            return 0
        return min(room, max(1, self.block_reward * room // self.supply_cap))

    def species_pool(self, height: int) -> tuple[str, ...] | None:
        pool = None
        for season in self.seasons:
            if season.start_height <= height:
                pool = season.species
        return pool
