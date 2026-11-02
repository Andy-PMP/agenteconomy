"""Shared setup for network tests: accounts, hand-built blocks, a misbehaving peer, and what settling must guarantee."""

from collections import Counter
from dataclasses import replace

from agenteconomy.chain.block import Block, BlockHeader, compute_merkle_root, mine
from agenteconomy.chain.params import ChainParams
from agenteconomy.chain.state import State
from agenteconomy.chain.transaction import Transaction, sign_transfer
from agenteconomy.chain.validation import build_block
from agenteconomy.chain.vectors import sample_keys
from agenteconomy.network.messages import encode_block
from agenteconomy.network.node import Node
from agenteconomy.network.settle import Cluster, SettleResult

ALICE, BOB, CAROL, DAVE = (sample_keys(name) for name in ("alice", "bob", "carol", "dave"))

PARAMS = ChainParams(
    chain_id="network-test", difficulty_bits=4, block_reward=50, min_fee=1, max_block_txs=20,
    genesis_allocations=((ALICE.public_hex, 1000), (BOB.public_hex, 500)),
)


def pay(sender=ALICE, to=BOB, amount=10, *, nonce=0, fee=1) -> Transaction:
    return sign_transfer(sender, PARAMS.chain_id, to=to.public_hex, amount=amount, nonce=nonce, fee=fee)


def next_block(parent: BlockHeader, txs=(), *, miner=CAROL, timestamp=None) -> Block:
    """A valid mined block on `parent`, which nobody has been told about."""
    timestamp = parent.timestamp if timestamp is None else timestamp
    block = build_block(PARAMS, parent, miner=miner.public_hex, txs=list(txs), timestamp=timestamp)
    header = mine(block.header, max_attempts=1 << 20)
    assert header is not None
    return Block(header, block.txs)


def remined(block: Block, txs=None) -> Block:
    """Keep the Merkle root and proof of work right, so a test's one deliberate fault is what gets judged."""
    txs = block.txs if txs is None else tuple(txs)
    header = mine(replace(block.header, merkle_root=compute_merkle_root(txs), nonce=0), max_attempts=1 << 20)
    assert header is not None
    return Block(header, txs)


def deliver(node: Node, block: Block, sender="peer-x") -> None:
    node.on_message(sender, encode_block(block))


def build_on(node: Node, count: int, *, miner=CAROL) -> list[Block]:
    """Extend a node's chain by submitting blocks to it directly."""
    blocks = []
    for _ in range(count):
        blocks.append(next_block(node.tip_block.header, miner=miner))
        node.submit_block(blocks[-1])
    return blocks


class BadPeer:
    """A network endpoint under a test's full control. It follows no rules and remembers what it's sent."""

    def __init__(self, cluster: Cluster, peer_id: str = "bad-peer"):
        self.id = peer_id
        self._network = cluster.network
        self.received: list[bytes] = []
        self._network.register(peer_id, lambda sender, data: self.received.append(data))

    def send(self, node: Node, data: bytes) -> None:
        self._network.send(self.id, node.id, data)

    def send_block(self, node: Node, block: Block) -> None:
        self.send(node, encode_block(block))


def assert_settled_correctly(cluster: Cluster, result: SettleResult, nodes=None) -> None:
    nodes = nodes or cluster.nodes
    assert {node.tip for node in nodes} == {result.tip}
    assert all(node.state == result.state for node in nodes)
    assert all(node.checkpoint_height == result.height for node in nodes)
    assert_chain_sound(result.blocks, result.state)


def assert_chain_sound(blocks: list[Block], state: State) -> None:
    ids = Counter(tx.id for block in blocks for tx in block.txs)
    assert max(ids.values()) == 1, "a transaction was included twice"
    assert all(balance >= 0 for balance in state.balances.values())
    assert sum(state.balances.values()) == state.minted - state.burned
