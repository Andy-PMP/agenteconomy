"""Transactions waiting for a block. One node's view; P2 gives each node its own.

Selection depends only on the contents, never on arrival order, so simulations are reproducible.
"""

import heapq

from .block import Block
from .errors import ChainError, Code
from .params import ChainParams
from .state import State, apply_tx, start_block
from .transaction import USER_KINDS, Transaction, check_transaction


class Mempool:
    def __init__(self, params: ChainParams, *, max_size: int = 1000, max_per_sender: int = 32):
        self._params = params
        self._max_size = max_size
        self._max_per_sender = max_per_sender
        self._by_sender: dict[str, dict[int, Transaction]] = {}
        self._ids: set[bytes] = set()

    def __len__(self) -> int:
        return len(self._ids)

    def __contains__(self, tx: Transaction) -> bool:
        return tx.id in self._ids

    def add(self, tx: Transaction, state: State) -> None:
        """Admit a transaction or raise ChainError. `state` is this node's current tip."""
        if tx.kind not in USER_KINDS:
            raise ChainError(Code.UNEXPECTED_KIND, tx.kind)
        check_transaction(tx, self._params)
        if tx.id in self._ids:
            raise ChainError(Code.DUPLICATE_TX)
        if tx.nonce < state.nonce(tx.sender):
            raise ChainError(Code.NONCE_TOO_LOW, f"next nonce is {state.nonce(tx.sender)}")

        waiting = self._by_sender.get(tx.sender, {})
        if tx.nonce in waiting:  # first seen wins; a double spend attempt never gets in
            raise ChainError(Code.NONCE_CONFLICT, f"nonce {tx.nonce} is already waiting")
        # Deliberately conservative: coins the sender might receive later don't count yet.
        if sum(pending.spend for pending in waiting.values()) + tx.spend > state.balance(tx.sender):
            raise ChainError(Code.INSUFFICIENT_BALANCE, "with this sender's other waiting transactions")
        if len(waiting) >= self._max_per_sender:
            raise ChainError(Code.SENDER_LIMIT)
        if len(self._ids) >= self._max_size:
            raise ChainError(Code.MEMPOOL_FULL)
        self._try_after_waiting(tx, waiting, state)

        self._by_sender.setdefault(tx.sender, {})[tx.nonce] = tx
        self._ids.add(tx.id)

    def _trial(self, state: State) -> State:
        """A throwaway copy of `state` as the next block would see it."""
        trial = state.copy()
        start_block(trial, state.height + 1)
        return trial

    def _try_after_waiting(self, tx: Transaction, waiting: dict[int, Transaction], state: State) -> None:
        """If `tx` is next in line for its sender, it must work now. A later nonce can't be judged yet."""
        trial = self._trial(state)
        nonce = trial.nonce(tx.sender)
        while nonce in waiting:
            apply_tx(trial, waiting[nonce], self._params)
            nonce += 1
        if tx.nonce == nonce:
            apply_tx(trial, tx, self._params)

    def select(self, state: State, max_txs: int) -> list[Transaction]:
        """Best transactions for a block: highest fee first, ties by ID, each sender's nonces in order."""
        trial = self._trial(state)
        heap = []
        for sender, waiting in self._by_sender.items():
            if (tx := waiting.get(trial.nonce(sender))) is not None:
                heapq.heappush(heap, _priority(tx))

        chosen: list[Transaction] = []
        while heap and len(chosen) < max_txs:
            tx = heapq.heappop(heap)[2]
            try:
                apply_tx(trial, tx, self._params)
            except ChainError:
                continue  # this one can't run, so neither can this sender's later nonces
            chosen.append(tx)
            if (following := self._by_sender[tx.sender].get(tx.nonce + 1)) is not None:
                heapq.heappush(heap, _priority(following))
        return chosen

    def waiting(self) -> list[Transaction]:
        return [tx for waiting in self._by_sender.values() for tx in waiting.values()]

    def remove(self, tx: Transaction) -> None:
        waiting = self._by_sender.get(tx.sender, {})
        if waiting.get(tx.nonce) == tx:
            del waiting[tx.nonce]
            self._ids.discard(tx.id)
            if not waiting:
                del self._by_sender[tx.sender]

    def on_new_tip(self, block: Block, state: State) -> list[tuple[Transaction, Code]]:
        """A block joined the chain: drop what it included, and anything `state` has made unusable."""
        for tx in block.txs:
            self.remove(tx)
        return self.prune(state)

    def prune(self, state: State) -> list[tuple[Transaction, Code]]:
        """Remove what `state` has made unusable, and return it with the reason.

        Trying each sender's next transactions matters: a stale accept left waiting would hold up that sender's
        later nonces forever.
        """
        dropped: list[tuple[Transaction, Code]] = []
        for sender, waiting in list(self._by_sender.items()):
            budget = state.balance(sender)
            for nonce in sorted(waiting):
                tx = waiting[nonce]
                if nonce < state.nonce(sender):
                    self.remove(tx)
                    dropped.append((tx, Code.NONCE_TOO_LOW))
                elif tx.spend > budget:
                    self.remove(tx)
                    dropped.append((tx, Code.INSUFFICIENT_BALANCE))
                else:
                    budget -= tx.spend
            dropped += self._prune_unrunnable(sender, state)
        return dropped

    def _prune_unrunnable(self, sender: str, state: State) -> list[tuple[Transaction, Code]]:
        waiting = self._by_sender.get(sender, {})
        trial = self._trial(state)
        nonce = trial.nonce(sender)
        while nonce in waiting:
            tx = waiting[nonce]
            try:
                apply_tx(trial, tx, self._params)
            except ChainError as e:
                self.remove(tx)
                return [(tx, e.code)]
            nonce += 1
        return []

    def readd_after_reorg(self, txs: list[Transaction], state: State) -> dict[bytes, Code]:
        """Offer back the transactions from abandoned blocks. Returns the ones refused, by ID, with the reason."""
        self.prune(state)
        refused: dict[bytes, Code] = {}
        for tx in sorted((tx for tx in txs if tx.kind in USER_KINDS), key=lambda tx: (tx.sender, tx.nonce, tx.id)):
            try:
                self.add(tx, state)
            except ChainError as e:
                refused[tx.id] = e.code
        return refused


def _priority(tx: Transaction) -> tuple[int, bytes, Transaction]:
    return -tx.fee, tx.id, tx
