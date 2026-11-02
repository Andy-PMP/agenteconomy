"""Shared setup for economy tests: a small chain where creatures hatch, plus what must always hold."""

from dataclasses import dataclass, field, replace

from agenteconomy.chain.block import Block, compute_merkle_root, mine
from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.params import ChainParams, Season
from agenteconomy.chain.state import State, apply_block
from agenteconomy.chain.transaction import Transaction, sign_accept, sign_offer, sign_reject, sign_transfer
from agenteconomy.chain.validation import build_block, make_genesis, validate_block
from agenteconomy.chain.vectors import sample_keys

ALICE, BOB, CAROL, DAVE = (sample_keys(name) for name in ("alice", "bob", "carol", "dave"))
POOL = ("blueA", "greenB", "redC")

PARAMS = ChainParams(
    chain_id="economy-test", difficulty_bits=4, block_reward=50, min_fee=1, supply_cap=100_000, trade_fee_bps=200,
    seasons=(Season(1, POOL),), max_offer_creatures=3, max_offer_ttl=50, max_open_offers=4, max_block_txs=20,
    genesis_allocations=((ALICE.public_hex, 1000), (BOB.public_hex, 500)),
)


def assert_sound(state: State, params: ChainParams = PARAMS) -> None:
    assert all(balance >= 0 for balance in state.balances.values())
    assert sum(state.balances.values()) == state.minted - state.burned
    assert set(state.owners) == set(state.creatures)
    if params.supply_cap is not None:
        assert state.supply <= params.supply_cap


@dataclass
class Chain:
    """A chain built by hand, with no nodes, checked by the real rules at every step."""

    params: ChainParams = PARAMS
    blocks: list[Block] = field(default_factory=list)
    state: State | None = None

    def __post_init__(self):
        genesis = make_genesis(self.params)
        self.blocks = [genesis]
        self.state = apply_block(State(), genesis, self.params)

    def unmined_next(self, txs=(), miner=CAROL) -> Block:
        parent = self.blocks[-1].header
        block = build_block(self.params, parent, miner=miner.public_hex, txs=list(txs), timestamp=parent.timestamp + 1,
                            supply=self.state.supply)
        return finished(block)

    def add(self, txs=(), miner=CAROL) -> Block:
        block = self.unmined_next(txs, miner)
        self.state = validate_block(block, self.blocks[-1].header, self.state, self.params)
        self.blocks.append(block)
        assert_sound(self.state, self.params)
        return block

    def hatch_for(self, keys) -> str:
        """Mine a block for `keys`, and return the creature that hatched."""
        return self.add((), keys).header.hash_hex

    def code_of(self, *txs, miner=CAROL) -> Code | None:
        """The rejection code a block with these transactions gets. The chain isn't changed."""
        before = self.state.copy()
        try:
            validate_block(self.unmined_next(txs, miner), self.blocks[-1].header, self.state, self.params)
        except ChainError as e:
            assert self.state == before
            return e.code
        assert self.state == before
        return None

    def nonce(self, keys) -> int:
        return self.state.nonce(keys.public_hex)

    def offer(self, keys, to, *, nonce=None, fee=1, ttl=10, **terms) -> Transaction:
        expires_at = terms.pop("expires_at", self.state.height + ttl)
        return sign_offer(keys, self.params.chain_id, to=to.public_hex, expires_at=expires_at,
                          nonce=self.nonce(keys) if nonce is None else nonce, fee=fee, **terms)

    def accept(self, keys, offer: Transaction | str, *, nonce=None, fee=1) -> Transaction:
        return sign_accept(keys, self.params.chain_id, offer_id=_offer_id(offer),
                           nonce=self.nonce(keys) if nonce is None else nonce, fee=fee)

    def reject(self, keys, offer: Transaction | str, *, nonce=None, fee=1) -> Transaction:
        return sign_reject(keys, self.params.chain_id, offer_id=_offer_id(offer),
                           nonce=self.nonce(keys) if nonce is None else nonce, fee=fee)

    def pay(self, keys, to, amount, *, nonce=None, fee=1) -> Transaction:
        return sign_transfer(keys, self.params.chain_id, to=to.public_hex, amount=amount,
                             nonce=self.nonce(keys) if nonce is None else nonce, fee=fee)


def _offer_id(offer: Transaction | str) -> str:
    return offer if isinstance(offer, str) else offer.id.hex()


def finished(block: Block) -> Block:
    header = mine(replace(block.header, merkle_root=compute_merkle_root(block.txs), nonce=0), max_attempts=1 << 20)
    assert header is not None
    return Block(header, block.txs)
