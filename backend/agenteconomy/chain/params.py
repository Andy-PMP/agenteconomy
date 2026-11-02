"""The settings every node on a chain must share. P3 chooses the real amounts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChainParams:
    chain_id: str
    difficulty_bits: int
    block_reward: int
    genesis_allocations: tuple[tuple[str, int], ...]  # (address, starting balance)
    genesis_timestamp: int = 0
    max_block_txs: int = 100  # including the coinbase
    min_fee: int = 0

    def __post_init__(self):
        addresses = [address for address, _ in self.genesis_allocations]
        if not addresses or len(set(addresses)) != len(addresses):
            raise ValueError("genesis needs at least one allocation and no repeated addresses")
        if not 0 <= self.difficulty_bits <= 255:
            raise ValueError("difficulty_bits must be 0 to 255")
        if self.max_block_txs < 1:
            raise ValueError("max_block_txs must leave room for the coinbase")
