"""Account state: what the transactions in a chain add up to. Nothing here is stored on-chain; verifiers replay."""

from dataclasses import dataclass, field

from .block import Block
from .canonical import MAX_SAFE_INT
from .errors import ChainError, Code
from .transaction import TRANSFER, Transaction


@dataclass
class State:
    balances: dict[str, int] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    minted: int = 0
    burned: int = 0

    def copy(self) -> State:
        return State(dict(self.balances), dict(self.nonces), self.minted, self.burned)

    def balance(self, address: str) -> int:
        return self.balances.get(address, 0)

    def nonce(self, address: str) -> int:
        return self.nonces.get(address, 0)

    @property
    def supply(self) -> int:
        return self.minted - self.burned


def apply_tx(state: State, tx: Transaction) -> None:
    """Apply one structurally valid transaction in place. Checks first, so a refusal changes nothing."""
    if tx.kind == TRANSFER:
        _apply_transfer(state, tx)
    else:
        _apply_mint(state, tx)


def _apply_transfer(state: State, tx: Transaction) -> None:
    # The nonce must match exactly. This is what stops replays and double spends: only one transaction can ever
    # use a given sender and nonce on a chain.
    if tx.nonce != state.nonce(tx.sender):
        raise ChainError(Code.BAD_NONCE, f"expected {state.nonce(tx.sender)}, got {tx.nonce}")
    if tx.spend > state.balance(tx.sender):
        raise ChainError(Code.INSUFFICIENT_BALANCE, f"needs {tx.spend}, has {state.balance(tx.sender)}")

    to, amount = tx.body["to"], tx.body["amount"]
    state.balances[tx.sender] -= tx.spend
    state.balances[to] = state.balance(to) + amount
    state.nonces[tx.sender] = tx.nonce + 1
    state.burned += tx.fee


def _apply_mint(state: State, tx: Transaction) -> None:
    to, amount = tx.body["to"], tx.body["amount"]
    if state.minted + amount > MAX_SAFE_INT:
        raise ChainError(Code.SUPPLY_OVERFLOW)
    state.balances[to] = state.balance(to) + amount
    state.minted += amount


def apply_block(state: State, block: Block) -> State:
    """Returns the new state and leaves `state` alone, so a failed block has no effect."""
    new_state = state.copy()
    for tx in block.txs:
        apply_tx(new_state, tx)
    return new_state
