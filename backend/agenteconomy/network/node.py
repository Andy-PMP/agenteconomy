"""One independent node: its own block tree, its own mempool, and only messages to learn what others know.

Fork choice: the most total work wins, and on equal work the block seen first stays. Settling adds one rule, in
resolve_tie(). A settled tip becomes a checkpoint nothing can fork below, because published days never change.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from ..chain.block import Block, block_work, compute_merkle_root, meets_target
from ..chain.block import mine as search_nonce
from ..chain.errors import ChainError, Code
from ..chain.mempool import Mempool
from ..chain.params import ChainParams
from ..chain.state import State
from ..chain.transaction import Transaction
from ..chain.validation import build_block, make_genesis, validate_block, validate_chain
from .messages import (
    MAX_LOCATOR_HASHES,
    MAX_SYNC_BATCH,
    BlockMessage,
    BlocksMessage,
    GetBlocksMessage,
    StatusMessage,
    TxMessage,
    decode,
    encode_block,
    encode_blocks,
    encode_get_blocks,
    encode_status,
    encode_tx,
)
from .network import EventLog, Network
from .simulator import Simulator

# Transaction problems no honest node would relay. The rest (say, a stale nonce) can be innocent timing.
_PROVABLY_BAD_TX = {Code.BAD_SIGNATURE, Code.WRONG_CHAIN, Code.SELF_TRANSFER, Code.INVALID_AMOUNT, Code.FEE_TOO_LOW,
                    Code.INVALID_PUBLIC_KEY, Code.UNEXPECTED_KIND}
_SYNC_RETRY_MS = 5_000


@dataclass
class _Entry:
    block: Block
    state: State | None  # None once finalized below the checkpoint: nothing can build on it again
    work: int

    @property
    def hash(self) -> str:
        return self.block.header.hash_hex

    @property
    def height(self) -> int:
        return self.block.header.height


class Node:
    def __init__(self, node_id: str, params: ChainParams, sim: Simulator, network: Network, log: EventLog, *,
                 chain: Sequence[Block] | None = None, ban_threshold: int = 10, max_orphans: int = 100,
                 max_future_ms: int = 60_000):
        self.id = node_id
        self.params = params
        self.mempool = Mempool(params)
        self.misbehavior: dict[str, int] = {}
        self.banned: set[str] = set()
        self._sim = sim
        self._network = network
        self._log = log
        self._ban_threshold = ban_threshold
        self._max_orphans = max_orphans
        self._max_future_ms = max_future_ms
        self._entries: dict[str, _Entry] = {}
        self._main: list[str] = []
        self._invalid: set[str] = set()
        self._orphans: dict[str, tuple[Block, str | None]] = {}
        self._sync_retry_at: dict[str, int] = {}
        self._search_nonce: dict[tuple[str, str], int] = {}
        self._tip_listeners: list[Callable[[], None]] = []
        self._closed = False
        self._settling = False
        self._load_chain(list(chain) if chain else [make_genesis(params)])
        network.register(node_id, self.on_message, self.announce)

    def _load_chain(self, blocks: list[Block]) -> None:
        """Start from a saved chain. It was settled when saved, so its tip is the checkpoint."""
        state = validate_chain(blocks, self.params)
        work = 0
        for block in blocks:
            if block.header.height > 0:
                work += block_work(block.header.difficulty_bits)
            self._entries[block.header.hash_hex] = _Entry(block, None, work)
            self._main.append(block.header.hash_hex)
        self._tip = self._entries[self._main[-1]]
        self._tip.state = state
        self._checkpoint_height = self._tip.height

    # What the rest of the system reads

    @property
    def tip(self) -> str:
        return self._tip.hash

    @property
    def tip_block(self) -> Block:
        return self._tip.block

    @property
    def height(self) -> int:
        return self._tip.height

    @property
    def work(self) -> int:
        return self._tip.work

    @property
    def state(self) -> State:
        return self._tip.state

    @property
    def checkpoint_height(self) -> int:
        return self._checkpoint_height

    def knows(self, block_hash: str) -> bool:
        return block_hash in self._entries

    def main_chain(self) -> list[Block]:
        return [self._entries[block_hash].block for block_hash in self._main]

    def side_blocks(self) -> list[Block]:
        """Valid blocks this node stored that aren't on its main chain."""
        main = set(self._main)
        return [entry.block for key, entry in self._entries.items() if key not in main]

    def subscribe_tip(self, listener: Callable[[], None]) -> None:
        self._tip_listeners.append(listener)

    # What agents and miners do through a node

    def submit_tx(self, tx: Transaction) -> None:
        """Admit a local transaction and relay it, or raise ChainError with the reason."""
        if self._closed:
            raise ChainError(Code.DAY_CLOSED)
        self.mempool.add(tx, self.state)
        self._log.record(self.id, "tx_accepted", tx=tx.id.hex())
        self._broadcast(encode_tx(tx))

    def build_template(self, miner: str) -> Block:
        """An unmined block on the current tip, with the best waiting transactions."""
        txs = self.mempool.select(self.state, self.params.max_block_txs - 1)
        timestamp = max(self._sim.now, self._tip.block.header.timestamp)
        return build_block(self.params, self._tip.block.header, miner=miner, txs=txs, timestamp=timestamp)

    def mine(self, miner: str, max_attempts: int) -> Block | None:
        """Spend a fixed hash budget on the tip. If it finds a block, it's accepted and relayed."""
        if self._closed:
            raise ChainError(Code.DAY_CLOSED)
        template = self.build_template(miner)
        # Keep going where the last budget stopped. Restarting at 0 would retry the same hashes forever.
        search = (self.tip, miner)
        start = self._search_nonce.get(search, 0)
        header = search_nonce(replace(template.header, nonce=start), max_attempts=max_attempts)
        if header is None:
            self._search_nonce[search] = start + max_attempts
            return None
        block = Block(header, template.txs)
        self.submit_block(block)
        return block

    def submit_block(self, block: Block) -> None:
        """A block mined here. It goes through the same checks as anyone else's."""
        self._on_block(block, None)

    # Messages

    def on_message(self, sender: str, data: bytes) -> None:
        if sender in self.banned:
            return
        try:
            message = decode(data)
        except ChainError as e:
            self._penalize(sender, e.code, "bad message")
            return

        match message:
            case TxMessage(tx):
                self._on_tx(tx, sender)
            case BlockMessage(block):
                self._on_block(block, sender)
            case BlocksMessage(blocks):
                self._on_blocks(blocks, sender)
            case StatusMessage():
                self._on_status(message, sender)
            case GetBlocksMessage(locator):
                self._on_get_blocks(locator, sender)

    def _broadcast(self, data: bytes, exclude: str | None = None) -> None:
        for peer in self._network.peers_of(self.id):
            if peer != exclude and peer not in self.banned:
                self._network.send(self.id, peer, data)

    def _penalize(self, sender: str | None, code: Code, what: str) -> None:
        self._log.record(self.id, "rejected", what=what, code=str(code), peer=sender)
        if sender is None:
            return
        self.misbehavior[sender] = self.misbehavior.get(sender, 0) + 1
        if self.misbehavior[sender] >= self._ban_threshold and sender not in self.banned:
            self.banned.add(sender)
            self._log.record(self.id, "peer_banned", peer=sender)

    def _on_tx(self, tx: Transaction, sender: str) -> None:
        if self._closed or tx in self.mempool:
            return
        try:
            self.mempool.add(tx, self.state)
        except ChainError as e:
            if e.code in _PROVABLY_BAD_TX:
                self._penalize(sender, e.code, "tx")
            return
        self._log.record(self.id, "tx_accepted", tx=tx.id.hex(), peer=sender)
        self._broadcast(encode_tx(tx), exclude=sender)

    # Blocks

    def _on_block(self, block: Block, sender: str | None) -> None:
        header = block.header
        key = header.hash_hex
        if key in self._entries or key in self._orphans:
            return
        if key in self._invalid:
            self._penalize(sender, Code.KNOWN_INVALID, "block")
            return

        # Checks that cost almost nothing and prove nothing permanent about this hash, so they aren't remembered.
        # In particular a real header with swapped transactions must not get the honest block refused later.
        if header.difficulty_bits != self.params.difficulty_bits:
            self._penalize(sender, Code.BAD_DIFFICULTY, "block")
            return
        if not meets_target(header.hash, header.difficulty_bits):
            self._penalize(sender, Code.BAD_POW, "block")
            return
        if not block.txs:
            self._penalize(sender, Code.BAD_COINBASE, "block")
            return
        if compute_merkle_root(block.txs) != header.merkle_root:
            self._penalize(sender, Code.BAD_MERKLE_ROOT, "block")
            return

        # Not a mark against the sender: the block may become acceptable later, or be innocently late.
        if header.timestamp > self._sim.now + self._max_future_ms:
            self._log.record(self.id, "block_ignored", block=key, code=str(Code.FUTURE_TIMESTAMP), peer=sender)
            return
        if header.height <= self._checkpoint_height:
            self._log.record(self.id, "block_ignored", block=key, code=str(Code.BELOW_CHECKPOINT), peer=sender)
            return

        if header.prev_hash in self._invalid:
            self._invalid.add(key)
            self._penalize(sender, Code.INVALID_PARENT, "block")
            return

        parent = self._entries.get(header.prev_hash)
        if parent is None:
            self._add_orphan(block, sender)
            return
        if self._connect(block, parent, sender):
            self._connect_orphans(key)

    def _connect(self, block: Block, parent: _Entry, sender: str | None) -> bool:
        key = block.header.hash_hex
        try:
            state = validate_block(block, parent.block.header, parent.state, self.params)
        except ChainError as e:
            self._invalid.add(key)  # its transactions match its header, so this hash is bad for good
            self._penalize(sender, e.code, "block")
            self._reject_orphan_children(key)
            return False

        entry = _Entry(block, state, parent.work + block_work(block.header.difficulty_bits))
        self._entries[key] = entry
        self._log.record(self.id, "block_accepted", block=key, height=entry.height, peer=sender)
        self._broadcast(encode_block(block), exclude=sender)
        if entry.work > self._tip.work:
            self._switch_tip(entry)
        return True

    def _add_orphan(self, block: Block, sender: str | None) -> None:
        if len(self._orphans) >= self._max_orphans:
            del self._orphans[next(iter(self._orphans))]
        self._orphans[block.header.hash_hex] = (block, sender)
        if sender is not None:
            self._request_sync(sender)

    def _connect_orphans(self, parent_key: str) -> None:
        pending = [parent_key]
        while pending:
            current = pending.pop()
            parent = self._entries[current]
            children = [key for key, (block, _) in self._orphans.items() if block.header.prev_hash == current]
            for key in children:
                block, sender = self._orphans.pop(key)
                if self._connect(block, parent, sender):
                    pending.append(key)

    def _reject_orphan_children(self, parent_key: str) -> None:
        children = [key for key, (block, _) in self._orphans.items() if block.header.prev_hash == parent_key]
        for key in children:
            _, sender = self._orphans.pop(key)
            self._invalid.add(key)
            self._penalize(sender, Code.INVALID_PARENT, "block")
            self._reject_orphan_children(key)

    def _switch_tip(self, new: _Entry) -> None:
        old = self._tip
        adopted: list[Block] = []
        abandoned: list[Block] = []
        branch_new, branch_old = new, old
        while branch_new.hash != branch_old.hash:
            if branch_new.height >= branch_old.height:
                adopted.append(branch_new.block)
                branch_new = self._entries[branch_new.block.header.prev_hash]
            else:
                abandoned.append(branch_old.block)
                branch_old = self._entries[branch_old.block.header.prev_hash]
        adopted.reverse()
        abandoned.reverse()

        del self._main[branch_new.height + 1:]
        self._main.extend(block.header.hash_hex for block in adopted)
        self._tip = new

        for block in adopted:
            self.mempool.on_new_tip(block, new.state)
        if abandoned:
            self._return_abandoned(old, new, abandoned, adopted)
        self._log.record(self.id, "tip_changed", tip=new.hash, height=new.height)
        for listener in self._tip_listeners:
            listener()

    def _return_abandoned(self, old: _Entry, new: _Entry, abandoned: list[Block], adopted: list[Block]) -> None:
        mined_again = {tx.id for block in adopted for tx in block.txs}
        txs = [tx for block in abandoned for tx in block.txs if tx.id not in mined_again]
        refused = self.mempool.readd_after_reorg(txs, new.state)
        returned = [tx.id.hex() for tx in txs if tx in self.mempool]
        self._log.record(self.id, "reorg", depth=len(abandoned), old_tip=old.hash, new_tip=new.hash,
                         orphaned=[block.header.hash_hex for block in abandoned], returned=returned,
                         refused={tx_id.hex(): str(code) for tx_id, code in refused.items()})

    # Catching up

    def announce(self) -> None:
        self._broadcast(encode_status(self.tip, self.height, self.work))

    def _on_status(self, status: StatusMessage, sender: str) -> None:
        if status.work < self.work:  # they're behind and may not know it
            self._network.send(self.id, sender, encode_status(self.tip, self.height, self.work))
        elif status.tip not in self._entries and (status.work > self.work or self._settling):
            self._request_sync(sender)

    def _request_sync(self, peer: str) -> None:
        if self._sim.now < self._sync_retry_at.get(peer, 0):
            return
        self._sync_retry_at[peer] = self._sim.now + _SYNC_RETRY_MS
        self._network.send(self.id, peer, encode_get_blocks(self._locator()))

    def _locator(self) -> list[str]:
        """Hashes from the tip back to genesis: every recent one, then increasingly spaced."""
        heights = []
        height, step = len(self._main) - 1, 1
        while height > 0 and len(heights) < MAX_LOCATOR_HASHES - 1:
            heights.append(height)
            if len(heights) >= 10:
                step *= 2
            height -= step
        return [self._main[height] for height in heights] + [self._main[0]]

    def _on_get_blocks(self, locator: tuple[str, ...], sender: str) -> None:
        start = 1
        for block_hash in locator:
            entry = self._entries.get(block_hash)
            if entry is not None and entry.height < len(self._main) and self._main[entry.height] == block_hash:
                start = entry.height + 1
                break
        batch = self.main_chain()[start:start + MAX_SYNC_BATCH]
        self._network.send(self.id, sender, encode_blocks(batch))

    def _on_blocks(self, blocks: tuple[Block, ...], sender: str) -> None:
        self._sync_retry_at.pop(sender, None)
        for block in blocks:
            self._on_block(block, sender)
        if len(blocks) == MAX_SYNC_BATCH:
            self._request_sync(sender)

    # Settling

    def close_day(self) -> None:
        self._closed = True
        self._settling = True

    def open_day(self) -> None:
        self._closed = False
        self._settling = False

    def resolve_tie(self) -> None:
        """Settle only: of the most-work blocks this node knows, take the lowest hash.

        First-seen can leave nodes split forever once mining has stopped. This rule needs no communication beyond
        everyone knowing the same tied blocks.
        """
        tied = [entry for entry in self._entries.values() if entry.work == self.work]
        best = min(tied, key=lambda entry: entry.hash)
        if best.hash != self.tip:
            self._switch_tip(best)

    def finalize(self) -> None:
        """Make the tip a checkpoint: drop every other branch, and the saved states below it."""
        main = set(self._main)
        self._entries = {key: entry for key, entry in self._entries.items() if key in main}
        for entry in self._entries.values():
            if entry is not self._tip:
                entry.state = None
        self._checkpoint_height = self.height
        self._orphans.clear()
        self._search_nonce.clear()
        self._log.record(self.id, "checkpoint", tip=self.tip, height=self.height)
