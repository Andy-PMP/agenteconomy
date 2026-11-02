"""Builds testdata/economy_vectors_v1.json: known answers for the economy rules, including a real simulated chain.

Seeds here are public test values. Everything is deterministic, so regenerating must not change a byte.
"""

import hashlib
import json
from dataclasses import asdict, replace
from typing import Any

from ..chain.block import Block, compute_merkle_root, mine
from ..chain.canonical import MAX_SAFE_INT
from ..chain.creature import SPECIES, hatch, hatching_tables
from ..chain.errors import ChainError
from ..chain.keys import KeyPair
from ..chain.params import ChainParams, Season
from ..chain.state import State
from ..chain.transaction import make_coinbase, sign_accept, sign_offer, sign_reject, sign_transfer
from ..chain.validation import build_block, make_genesis, validate_block, validate_chain
from ..chain.vectors import sample_keys
from .agents import ROSTER
from .world import Member, World, WorldConfig, economy_params

VECTOR_POOL = ("blueA", "greenB", "redC", "yellowD")


def params_to_dict(params: ChainParams) -> dict[str, Any]:
    data = asdict(params)
    data["genesis_allocations"] = [list(item) for item in params.genesis_allocations]
    data["seasons"] = [{"start_height": season.start_height, "species": list(season.species)}
                       for season in params.seasons]
    return data


def params_from_dict(data: dict[str, Any]) -> ChainParams:
    return ChainParams(**{
        **data,
        "genesis_allocations": tuple((address, amount) for address, amount in data["genesis_allocations"]),
        "seasons": tuple(Season(season["start_height"], tuple(season["species"])) for season in data["seasons"]),
    })


def state_to_dict(state: State) -> dict[str, Any]:
    return {
        "balances": state.balances, "nonces": state.nonces, "minted": state.minted, "burned": state.burned,
        "height": state.height, "owners": state.owners,
        "creatures": {key: creature.to_dict() for key, creature in state.creatures.items()},
        "offers": {key: asdict(offer) for key, offer in state.offers.items()},
    }


def validate_chain_dicts(blocks: list[dict[str, Any]], params: ChainParams) -> State:
    return validate_chain([Block.from_dict(block) for block in blocks], params)


class _Builder:
    """Builds a valid chain block by block, so a test case can break just its last block."""

    def __init__(self, params: ChainParams):
        self.params = params
        self.blocks = [make_genesis(params)]
        self.state = validate_chain(self.blocks, params)

    def block(self, txs: list, miner: KeyPair) -> Block:
        block = self.unchecked(txs, miner)
        self.state = validate_block(block, self.blocks[-1].header, self.state, self.params)
        self.blocks.append(block)
        return block

    def unchecked(self, txs: list, miner: KeyPair) -> Block:
        parent = self.blocks[-1].header
        block = build_block(self.params, parent, miner=miner.public_hex, txs=txs, timestamp=parent.timestamp + 10,
                            supply=self.state.supply)
        return _remine(block)

    def nonce(self, keys: KeyPair) -> int:
        return self.state.nonce(keys.public_hex)


def _remine(block: Block, txs: tuple | None = None) -> Block:
    txs = block.txs if txs is None else txs
    header = mine(replace(block.header, merkle_root=compute_merkle_root(txs), nonce=0), max_attempts=1 << 20)
    assert header is not None
    return Block(header, txs)


def _offer_scenario() -> tuple[_Builder, dict[str, Any]]:
    """Two creatures hatch, a haggle ends in a trade, and more offers are left open for the tampered cases."""
    alice, bob, carol = sample_keys("alice"), sample_keys("bob"), sample_keys("carol")
    params = ChainParams(
        chain_id="economy-v1", difficulty_bits=6, block_reward=50, min_fee=1, supply_cap=5_000, trade_fee_bps=200,
        seasons=(Season(1, VECTOR_POOL),), max_block_txs=6, max_offer_creatures=2, max_offer_ttl=20,
        max_open_offers=3, genesis_timestamp=100,
        genesis_allocations=((alice.public_hex, 1000), (bob.public_hex, 500)),
    )
    chain = _Builder(params)
    chain_id = params.chain_id
    alice_creature = chain.block([], alice).header.hash_hex
    bob_creature = chain.block([], bob).header.hash_hex
    carol_creature = chain.block([], carol).header.hash_hex

    first = sign_offer(alice, chain_id, to=bob.public_hex, give_creatures=(alice_creature,), want_coins=100,
                       expires_at=chain.state.height + 10, nonce=chain.nonce(alice), fee=1)
    chain.block([first], carol)
    counter = sign_offer(bob, chain_id, to=alice.public_hex, want_creatures=(alice_creature,), give_coins=80,
                         expires_at=chain.state.height + 10, counter_of=first.id.hex(), nonce=chain.nonce(bob), fee=1)
    chain.block([counter], carol)
    chain.block([sign_accept(alice, chain_id, offer_id=counter.id.hex(), nonce=chain.nonce(alice), fee=2)], carol)

    open_offer = sign_offer(bob, chain_id, to=carol.public_hex, give_creatures=(alice_creature,), want_coins=10,
                            expires_at=chain.state.height + 10, nonce=chain.nonce(bob), fee=1)
    declined = sign_offer(bob, chain_id, to=carol.public_hex, give_coins=5, expires_at=chain.state.height + 10,
                          nonce=chain.nonce(bob) + 1, fee=1)
    chain.block([open_offer, declined], carol)
    chain.block([sign_reject(carol, chain_id, offer_id=declined.id.hex(), nonce=chain.nonce(carol), fee=1)], alice)
    names = {"alice": alice, "bob": bob, "carol": carol, "alice_creature": alice_creature,
             "bob_creature": bob_creature, "carol_creature": carol_creature, "open_offer": open_offer}
    return chain, names


def _tampered(chain: _Builder, names: dict[str, Any]) -> list[dict[str, Any]]:
    alice, bob, carol = names["alice"], names["bob"], names["carol"]
    chain_id, height = chain.params.chain_id, chain.state.height
    open_id = names["open_offer"].id.hex()
    # Bob owns alice_creature now, and his offer of it to carol is open.
    traded, kept = names["alice_creature"], names["bob_creature"]
    carols = names["carol_creature"]
    expires = height + 10

    def nonce(keys):
        return chain.nonce(keys)

    def offer(keys, to, **terms):
        return sign_offer(keys, chain_id, to=to.public_hex, nonce=nonce(keys), fee=terms.pop("fee", 1),
                          expires_at=terms.pop("expires_at", expires), **terms)

    def accept(keys, offer_id):
        return sign_accept(keys, chain_id, offer_id=offer_id, nonce=nonce(keys), fee=1)

    def with_txs(*txs) -> Block:
        return chain.unchecked(list(txs), carol)

    def case(*txs) -> list[Block]:
        return [*chain.blocks, with_txs(*txs)]

    ids = sorted((traded, kept))
    bait = offer(alice, bob, give_coins=10**6)
    bait_chain = _Builder(chain.params)
    bait_chain.blocks, bait_chain.state = list(chain.blocks), chain.state
    bait_chain.block([bait], carol)
    bait_accept = sign_accept(bob, chain_id, offer_id=bait.id.hex(), nonce=bait_chain.nonce(bob), fee=1)

    moved = offer(bob, alice, give_creatures=(traded,), want_coins=1)
    moved_chain = _Builder(chain.params)
    moved_chain.blocks, moved_chain.state = list(chain.blocks), chain.state
    moved_chain.block([moved, sign_accept(carol, chain_id, offer_id=open_id, nonce=nonce(carol), fee=1)], alice)
    moved_accept = sign_accept(alice, chain_id, offer_id=moved.id.hex(), nonce=moved_chain.nonce(alice), fee=1)

    pricey = offer(bob, alice, give_creatures=(kept,), want_coins=10**6)
    pricey_chain = _Builder(chain.params)
    pricey_chain.blocks, pricey_chain.state = list(chain.blocks), chain.state
    pricey_chain.block([pricey], carol)
    pricey_accept = sign_accept(alice, chain_id, offer_id=pricey.id.hex(), nonce=pricey_chain.nonce(alice), fee=1)

    filler = [sign_offer(bob, chain_id, to=carol.public_hex, give_coins=1, expires_at=expires + i,
                         nonce=nonce(bob) + i, fee=1) for i in range(3)]

    expiry_chain = _Builder(chain.params)
    expiry_chain.blocks, expiry_chain.state = list(chain.blocks), chain.state
    for _ in range(10):
        expiry_chain.block([], alice)

    coinbase = with_txs().txs[0]
    cases: dict[str, tuple[str, list[Block]]] = {
        "accept by someone other than the recipient": ("not_offer_party", case(accept(alice, open_id))),
        "accept of an offer that was never made": ("offer_not_open", case(accept(carol, "ab" * 32))),
        "accept of an offer already closed by a counter": ("offer_not_open", case(
            accept(bob, chain.blocks[4].txs[1].id.hex()))),
        "accept of an expired offer": ("offer_not_open", [*expiry_chain.blocks, _on(expiry_chain, [
            sign_accept(carol, chain_id, offer_id=open_id, nonce=nonce(carol), fee=1)], carol)]),
        "reject by a stranger": ("not_offer_party", case(sign_reject(
            alice, chain_id, offer_id=open_id, nonce=nonce(alice), fee=1))),
        "reject of an offer that isn't open": ("offer_not_open", case(sign_reject(
            carol, chain_id, offer_id="ab" * 32, nonce=nonce(carol), fee=1))),
        "counter from someone other than the recipient": ("not_offer_party", case(
            offer(alice, bob, give_coins=1, counter_of=open_id))),
        "counter sent to someone other than the proposer": ("not_offer_party", case(
            offer(carol, alice, give_coins=1, counter_of=open_id))),
        "counter of an offer that isn't open": ("offer_not_open", case(
            offer(carol, bob, give_coins=1, counter_of="ab" * 32))),
        "offering a creature you don't own": ("not_owner", case(
            offer(alice, carol, give_creatures=(kept,), want_coins=1))),
        "offering a creature that doesn't exist": ("not_owner", case(
            offer(alice, carol, give_creatures=("ab" * 32,), want_coins=1))),
        "creature changed hands before the accept": ("not_owner", [*moved_chain.blocks, _on(
            moved_chain, [moved_accept], carol)]),
        "wanted creature isn't the acceptor's": ("not_owner", _with_wanted_missing(chain, alice, carol, carols, kept)),
        "proposer can't pay the coins it promised": ("insufficient_balance", [*bait_chain.blocks, _on(
            bait_chain, [bait_accept], carol)]),
        "acceptor can't pay": ("insufficient_balance", [*pricey_chain.blocks, _on(
            pricey_chain, [pricey_accept], carol)]),
        "expiry already passed": ("invalid_offer", case(offer(
            alice, bob, give_coins=1, expires_at=height + 1))),
        "expiry too far ahead": ("invalid_offer", case(offer(
            alice, bob, give_coins=1, expires_at=height + 22))),
        "too many open offers": ("too_many_offers", case(*filler)),
        "coins going both ways": ("invalid_offer", case(offer(alice, bob, give_coins=1, want_coins=1))),
        "offer exchanges nothing": ("invalid_offer", case(offer(alice, bob))),
        "unsorted creature list": ("invalid_offer", case(offer(
            bob, alice, give_creatures=(ids[1], ids[0]), want_coins=1))),
        "repeated creature": ("invalid_offer", case(offer(
            bob, alice, give_creatures=(kept, kept), want_coins=1))),
        "same creature on both sides": ("invalid_offer", case(offer(
            bob, alice, give_creatures=(kept,), want_creatures=(kept,)))),
        "too many creatures": ("invalid_offer", case(offer(
            bob, alice, give_creatures=tuple(sorted(("aa" * 32, "bb" * 32, "cc" * 32))), want_coins=1))),
        "offer to yourself": ("self_transfer", case(offer(alice, alice, give_coins=1))),
        "terms changed after signing": ("bad_signature", [*chain.blocks, _tampered_terms(
            with_txs, alice, bob, offer)]),
        "first transaction is a transfer paying exactly the reward": ("bad_coinbase", [*chain.blocks, _remine(
            with_txs(), (sign_transfer(alice, chain_id, to=bob.public_hex, amount=coinbase.body["amount"],
                                       nonce=nonce(alice), fee=1),))]),
        "coinbase pays the flat reward instead of the soft-cap one": ("bad_coinbase", [*chain.blocks, _remine(
            with_txs(), (replace(coinbase, body={**coinbase.body, "amount": 50}),))]),
    }
    vectors = [{"name": name, "code": code, "blocks": [block.to_dict() for block in blocks]}
               for name, (code, blocks) in cases.items()]

    def raw(name: str, code: str, edit) -> None:
        last = with_txs(offer(alice, bob, give_coins=1))
        blocks = [block.to_dict() for block in [*chain.blocks, last]]
        edit(blocks[-1]["txs"][1])
        vectors.append({"name": name, "code": code, "blocks": blocks})

    raw("free text in an offer", "malformed", lambda tx: tx["body"].update(memo="hello"))
    raw("creature ID that isn't a hash", "malformed", lambda tx: tx["body"].update(give_creatures=["Fluffy"]))
    raw("counter_of that isn't a hash", "malformed", lambda tx: tx["body"].update(counter_of="please"))
    raw("true used as coins", "malformed", lambda tx: tx["body"].update(give_coins=True))
    raw("negative coins", "malformed", lambda tx: tx["body"].update(want_coins=-1))
    raw("list of creatures isn't a list", "malformed", lambda tx: tx["body"].update(want_creatures="x"))
    raw("version isn't an integer", "malformed", lambda tx: tx.update(version="1"))
    raw("chain ID has the wrong form", "malformed", lambda tx: tx.update(chain_id="NOT VALID"))
    raw("kind isn't a string", "malformed", lambda tx: tx.update(kind=5))
    raw("recipient in uppercase", "malformed", lambda tx: tx["body"].update(to=tx["body"]["to"].upper()))
    raw("transfer amount isn't an integer", "malformed", lambda tx: tx.update(
        kind="transfer", body={"to": tx["body"]["to"], "amount": 1.5}))
    raw("offer ID isn't a hash", "malformed", lambda tx: tx.update(kind="accept", body={"offer_id": "x"}))

    def raw_block(name: str, edit) -> None:
        blocks = [block.to_dict() for block in [*chain.blocks, with_txs()]]
        edit(blocks[-1])
        vectors.append({"name": name, "code": "malformed", "blocks": blocks})

    raw_block("coinbase height is negative", lambda b: b["txs"][0]["body"].update(height=-1))
    raw_block("coinbase has a sender", lambda b: b["txs"][0].update(sender=alice.public_hex))
    raw_block("unknown block field", lambda b: b.update(extra=1))
    raw_block("unknown header field", lambda b: b["header"].update(extra=1))
    raw_block("header version isn't an integer", lambda b: b["header"].update(version="1"))
    raw_block("previous hash in uppercase", lambda b: b["header"].update(prev_hash=b["header"]["prev_hash"].upper()))
    raw_block("negative height", lambda b: b["header"].update(height=-1))
    raw_block("difficulty out of range", lambda b: b["header"].update(difficulty_bits=256))
    return vectors


def _on(builder: _Builder, txs: list, miner: KeyPair) -> Block:
    return builder.unchecked(txs, miner)


def _with_wanted_missing(chain: _Builder, alice, carol, carols: str, kept: str) -> list[Block]:
    builder = _Builder(chain.params)
    builder.blocks, builder.state = list(chain.blocks), chain.state
    wants = sign_offer(alice, chain.params.chain_id, to=carol.public_hex, want_creatures=(kept,), give_coins=1,
                       expires_at=chain.state.height + 10, nonce=chain.nonce(alice), fee=1)
    builder.block([wants], carol)
    accept = sign_accept(carol, chain.params.chain_id, offer_id=wants.id.hex(), nonce=builder.nonce(carol), fee=1)
    return [*builder.blocks, _on(builder, [accept], alice)]


def _tampered_terms(with_txs, alice, bob, offer) -> Block:
    tx = offer(alice, bob, give_coins=1)
    forged = replace(tx, body={**tx.body, "give_coins": 2})
    base = with_txs()
    return _remine(base, (base.txs[0], forged))


def _reward_vectors() -> list[dict[str, Any]]:
    genesis = ((sample_keys("alice").public_hex, 10),)
    cases = []
    for cap, base, supply in [(100_000, 50, 0), (100_000, 50, 14_000), (100_000, 50, 50_000), (100_000, 50, 99_000),
                              (100_000, 50, 99_999), (100_000, 50, 100_000), (100_000, 50, 100_001), (1_000, 50, 999),
                              (1_000, 50, 998), (1_000, 7, 0), (1_000, 7, 860)]:
        params = ChainParams("r", 1, base, genesis, supply_cap=cap)
        cases.append({"cap": cap, "base": base, "supply": supply, "reward": params.reward_at(supply)})
    return cases


def _cap_chain() -> dict[str, Any]:
    """A chain that reaches the cap, where a coinbase of 0 is correct, and burning makes mining pay again."""
    alice, bob = sample_keys("alice"), sample_keys("bob")
    params = ChainParams(chain_id="cap-v1", difficulty_bits=4, block_reward=50, min_fee=1, supply_cap=1_010,
                         seasons=(Season(1, ("blueA",)),), genesis_allocations=((alice.public_hex, 1000),))
    chain = _Builder(params)
    while params.reward_at(chain.state.supply) > 0:
        chain.block([], bob)
    chain.block([], bob)  # paid 0
    chain.block([sign_transfer(alice, params.chain_id, to=bob.public_hex, amount=1, nonce=0, fee=5)], bob)
    chain.block([], bob)  # pays again, because the fee burned
    overpaid = chain.unchecked([], bob)
    coinbase = make_coinbase(params, to=bob.public_hex, height=overpaid.header.height,
                             amount=params.reward_at(chain.state.supply) + 1)
    return {
        "params": params_to_dict(params),
        "blocks": [block.to_dict() for block in chain.blocks],
        "final_state": state_to_dict(chain.state),
        "tampered": {"name": "coinbase that would pass the cap", "code": "bad_coinbase",
                     "blocks": [block.to_dict() for block in chain.blocks]
                     + [_remine(overpaid, (coinbase,)).to_dict()]},
    }


def _overflow_chain() -> dict[str, Any]:
    """With no cap, minting past what JavaScript can represent exactly must be refused."""
    alice, bob = sample_keys("alice"), sample_keys("bob")
    params = ChainParams(chain_id="overflow-v1", difficulty_bits=4, block_reward=50,
                         genesis_allocations=((alice.public_hex, MAX_SAFE_INT),))
    chain = _Builder(params)
    return {"params": params_to_dict(params), "name": "minting past the safe integer range", "code": "supply_overflow",
            "blocks": [block.to_dict() for block in chain.blocks] + [chain.unchecked([], bob).to_dict()]}


def _simulated_chain() -> dict[str, Any]:
    """What the scripted economy really produced. JS replays it, so the two can't drift apart on real data."""
    members = [Member(agent(name), sample_keys(name)) for name, agent in ROSTER]
    bits = 8
    params = economy_params([(member.keys.public_hex, 1000) for member in members], chain_id="simulated-v1",
                            difficulty_bits=bits)
    world = World(WorldConfig(params, seed="vectors", hash_budget=1 << bits), members)
    for _ in range(4):
        world.run_day()
    return {"params": params_to_dict(params), "blocks": [block.to_dict() for block in world.blocks],
            "final_state": state_to_dict(world.state), "trades": len(world.trades)}


def build_vectors() -> dict[str, Any]:
    chain, names = _offer_scenario()
    hashes = [hashlib.sha256(f"hatch {i}".encode()).hexdigest() for i in range(40)]
    return {
        "format": "agenteconomy economy vectors",
        "version": 1,
        "notes": "All keys and seeds here are public test values. See docs/DATA.md for the rules.",
        "creature_tables": hatching_tables(),
        "species": list(SPECIES),
        "hatching": [{"hash": value, "pool": list(VECTOR_POOL), "creature": hatch(value, VECTOR_POOL).to_dict()}
                     for value in hashes],
        "reward": _reward_vectors(),
        "chain": {"params": params_to_dict(chain.params), "blocks": [block.to_dict() for block in chain.blocks],
                  "final_state": state_to_dict(chain.state), "tip": chain.blocks[-1].header.hash_hex},
        "tampered_chains": _tampered(chain, names),
        "cap_chain": _cap_chain(),
        "overflow_chain": _overflow_chain(),
        "simulated_chain": _simulated_chain(),
    }


def render_vectors() -> str:
    return json.dumps(build_vectors(), indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def expected_code(case: dict[str, Any], params: ChainParams) -> str | None:
    """The code a chain really gets, for checking the vectors against the rules."""
    try:
        validate_chain_dicts(case["blocks"], params)
    except ChainError as e:
        return str(e.code)
    return None
