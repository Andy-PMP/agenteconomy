from dataclasses import replace

import pytest
from economy_helpers import ALICE, BOB, CAROL, DAVE, PARAMS, Chain

from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.mempool import Mempool
from agenteconomy.chain.transaction import Transaction, check_transaction


@pytest.fixture
def chain():
    return Chain()


@pytest.fixture
def creature(chain):
    return chain.hatch_for(ALICE)


def shape_code(tx: Transaction) -> Code | None:
    try:
        check_transaction(tx, PARAMS)
    except ChainError as e:
        return e.code
    return None


# Trades


def test_offer_opens_with_the_signed_terms(chain, creature):
    tx = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([tx])
    offer = chain.state.offers[tx.id.hex()]
    assert offer.proposer == ALICE.public_hex and offer.to == BOB.public_hex
    assert offer.give_creatures == (creature,) and offer.want_coins == 100


def test_an_offer_moves_nothing_until_accepted(chain, creature):
    before = chain.state.balance(ALICE.public_hex)
    chain.add([chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)])
    assert chain.state.owners[creature] == ALICE.public_hex
    assert chain.state.balance(ALICE.public_hex) == before - 1  # only the fee


def test_accept_swaps_the_creature_for_coins_and_burns_the_trade_fee(chain, creature):
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([offer])
    alice, bob, burned = (chain.state.balance(ALICE.public_hex), chain.state.balance(BOB.public_hex),
                          chain.state.burned)
    chain.add([chain.accept(BOB, offer)])

    assert chain.state.owners[creature] == BOB.public_hex
    assert chain.state.balance(ALICE.public_hex) == alice + 98  # 2% of the coins burned
    assert chain.state.balance(BOB.public_hex) == bob - 100 - 1  # plus the accept's own fee
    assert chain.state.burned == burned + 2 + 1
    assert offer.id.hex() not in chain.state.offers


def test_offer_of_coins_for_a_creature_burns_from_the_seller(chain, creature):
    offer = chain.offer(BOB, ALICE, give_coins=100, want_creatures=(creature,))
    chain.add([offer])
    alice = chain.state.balance(ALICE.public_hex)
    chain.add([chain.accept(ALICE, offer)])
    assert chain.state.owners[creature] == BOB.public_hex
    assert chain.state.balance(ALICE.public_hex) == alice + 98 - 1


def test_trade_fee_rounds_down_so_a_tiny_trade_burns_nothing(chain, creature):
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=49)
    chain.add([offer])
    burned = chain.state.burned
    chain.add([chain.accept(BOB, offer, fee=1)])
    assert chain.state.burned == burned + 1  # 2% of 49 rounds to 0; only the accept's fee burns


def test_creatures_can_swap_for_creatures(chain):
    mine, yours = chain.hatch_for(ALICE), chain.hatch_for(BOB)
    offer = chain.offer(ALICE, BOB, give_creatures=(mine,), want_creatures=(yours,))
    chain.add([offer])
    burned = chain.state.burned
    chain.add([chain.accept(BOB, offer)])
    assert chain.state.owners[mine] == BOB.public_hex
    assert chain.state.owners[yours] == ALICE.public_hex
    assert chain.state.burned == burned + 1


def test_coins_only_gift_is_allowed(chain):
    offer = chain.offer(ALICE, BOB, give_coins=10)
    chain.add([offer])
    chain.add([chain.accept(BOB, offer)])
    assert chain.state.balance(BOB.public_hex) == 500 + 10 - 1


def test_accept_executes_the_signed_terms_and_names_nothing_else(chain, creature):
    """The injection defense: accepting takes only an ID, so no message can change what it does."""
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([offer])
    accept = chain.accept(BOB, offer)
    assert set(accept.body) == {"offer_id"}


def test_an_offer_and_its_accept_can_share_a_block(chain, creature):
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([offer, chain.accept(BOB, offer)])
    assert chain.state.owners[creature] == BOB.public_hex


# Counters


def test_counter_closes_the_original_and_goes_back_to_the_proposer(chain, creature):
    first = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([first])
    counter = chain.offer(BOB, ALICE, want_creatures=(creature,), give_coins=80, counter_of=first.id.hex())
    chain.add([counter])
    assert first.id.hex() not in chain.state.offers
    assert chain.state.offers[counter.id.hex()].to == ALICE.public_hex

    chain.add([chain.accept(ALICE, counter)])
    assert chain.state.owners[creature] == BOB.public_hex


def test_counter_chains_can_go_back_and_forth(chain, creature):
    first = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([first])
    second = chain.offer(BOB, ALICE, want_creatures=(creature,), give_coins=80, counter_of=first.id.hex())
    chain.add([second])
    third = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=90, counter_of=second.id.hex())
    chain.add([third])
    assert list(chain.state.offers) == [third.id.hex()]


def test_closed_original_cannot_be_accepted_after_a_counter(chain, creature):
    first = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([first])
    chain.add([chain.offer(BOB, ALICE, want_creatures=(creature,), give_coins=80, counter_of=first.id.hex())])
    assert chain.code_of(chain.accept(BOB, first)) == Code.OFFER_NOT_OPEN


def test_only_the_recipient_can_counter_and_only_back_to_the_proposer(chain, creature):
    first = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([first])
    stranger = chain.offer(CAROL, ALICE, give_coins=1, counter_of=first.id.hex())
    assert chain.code_of(stranger) == Code.NOT_OFFER_PARTY
    wrong_target = chain.offer(BOB, CAROL, give_coins=1, counter_of=first.id.hex())
    assert chain.code_of(wrong_target) == Code.NOT_OFFER_PARTY
    own = chain.offer(ALICE, BOB, give_coins=1, counter_of=first.id.hex())
    assert chain.code_of(own) == Code.NOT_OFFER_PARTY


def test_counter_of_a_closed_offer_is_rejected(chain):
    assert chain.code_of(chain.offer(BOB, ALICE, give_coins=1, counter_of="ab" * 32)) == Code.OFFER_NOT_OPEN


# Rejecting and withdrawing


def test_recipient_can_reject_and_proposer_can_withdraw(chain):
    declined = chain.offer(ALICE, BOB, give_coins=5)
    withdrawn = chain.offer(ALICE, BOB, give_coins=6, nonce=chain.nonce(ALICE) + 1)
    chain.add([declined, withdrawn])
    chain.add([chain.reject(BOB, declined), chain.reject(ALICE, withdrawn)])
    assert chain.state.offers == {}


def test_a_stranger_cannot_reject(chain):
    offer = chain.offer(ALICE, BOB, give_coins=5)
    chain.add([offer])
    assert chain.code_of(chain.reject(CAROL, offer)) == Code.NOT_OFFER_PARTY


def test_rejecting_a_closed_offer_fails(chain):
    assert chain.code_of(chain.reject(CAROL, "ab" * 32)) == Code.OFFER_NOT_OPEN


def test_rejected_offer_cannot_then_be_accepted(chain):
    offer = chain.offer(ALICE, BOB, give_coins=5)
    chain.add([offer])
    chain.add([chain.reject(BOB, offer)])
    assert chain.code_of(chain.accept(BOB, offer)) == Code.OFFER_NOT_OPEN


# Who can do what


def test_only_the_recipient_can_accept(chain, creature):
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([offer])
    assert chain.code_of(chain.accept(CAROL, offer)) == Code.NOT_OFFER_PARTY
    assert chain.code_of(chain.accept(ALICE, offer)) == Code.NOT_OFFER_PARTY


def test_an_offer_can_only_be_accepted_once(chain, creature):
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=100)
    chain.add([offer])
    chain.add([chain.accept(BOB, offer)])
    assert chain.code_of(chain.accept(BOB, offer)) == Code.OFFER_NOT_OPEN


def test_accept_of_an_unknown_offer(chain):
    assert chain.code_of(chain.accept(BOB, "ab" * 32)) == Code.OFFER_NOT_OPEN


# Ownership


def test_cannot_offer_what_you_do_not_own(chain, creature):
    assert chain.code_of(chain.offer(BOB, CAROL, give_creatures=(creature,), want_coins=1)) == Code.NOT_OWNER
    assert chain.code_of(chain.offer(BOB, CAROL, give_creatures=("ab" * 32,), want_coins=1)) == Code.NOT_OWNER


def test_creature_hatched_in_this_block_cannot_be_offered_in_it(chain):
    # The hatch happens after a block's transactions, and an ID depends on the block anyway.
    assert chain.code_of(chain.offer(ALICE, BOB, give_creatures=("ab" * 32,), want_coins=1)) == Code.NOT_OWNER


def test_accept_fails_if_the_proposer_no_longer_owns_the_creature(chain, creature):
    to_bob = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=1)
    to_carol = chain.offer(ALICE, CAROL, give_creatures=(creature,), want_coins=1, nonce=chain.nonce(ALICE) + 1)
    chain.add([to_bob, to_carol])
    chain.add([chain.accept(BOB, to_bob)])
    # No escrow, so the second offer is still open, but it can't be honored.
    assert chain.code_of(chain.accept(CAROL, to_carol)) == Code.NOT_OWNER


def test_accept_fails_if_the_acceptor_does_not_own_what_is_wanted(chain):
    wanted = chain.hatch_for(BOB)
    offer = chain.offer(ALICE, CAROL, give_coins=1, want_creatures=(wanted,))
    chain.add([offer])
    assert chain.code_of(chain.accept(CAROL, offer)) == Code.NOT_OWNER


def test_accept_fails_if_the_proposer_cannot_pay(chain, creature):
    """Bait: valid until someone accepts it, because nothing was locked."""
    bait = chain.offer(BOB, ALICE, give_coins=10**6, want_creatures=(creature,))
    chain.add([bait])
    assert bait.id.hex() in chain.state.offers
    assert chain.code_of(chain.accept(ALICE, bait)) == Code.INSUFFICIENT_BALANCE


def test_accept_fails_if_the_acceptor_cannot_pay_coins_plus_fee(chain, creature):
    balance = chain.state.balance(BOB.public_hex)
    too_much = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=balance)
    chain.add([too_much])
    assert chain.code_of(chain.accept(BOB, too_much)) == Code.INSUFFICIENT_BALANCE


def test_failed_accept_changes_nothing(chain, creature):
    bait = chain.offer(BOB, ALICE, give_coins=10**6, want_creatures=(creature,))
    chain.add([bait])
    before = chain.state.copy()
    assert chain.code_of(chain.accept(ALICE, bait)) is not None
    assert chain.state == before


def test_acceptor_can_pay_exactly(chain, creature):
    balance = chain.state.balance(BOB.public_hex)
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=balance - 1)
    chain.add([offer])
    chain.add([chain.accept(BOB, offer, fee=1)])
    assert chain.state.balance(BOB.public_hex) == 0


def test_fee_must_be_affordable_for_every_kind(chain):
    # Carol has nothing, and Bob mines these blocks so no reward reaches her first.
    assert chain.code_of(chain.offer(CAROL, ALICE, give_coins=1), miner=BOB) == Code.INSUFFICIENT_BALANCE
    offer = chain.offer(ALICE, CAROL, give_coins=1)
    chain.add([offer], BOB)
    assert chain.code_of(chain.accept(CAROL, offer), miner=BOB) == Code.INSUFFICIENT_BALANCE
    assert chain.code_of(chain.reject(CAROL, offer), miner=BOB) == Code.INSUFFICIENT_BALANCE


def test_a_gift_works_from_a_proposer_who_has_never_held_coins():
    # A regression waiting to happen: a trade moving no coins must not touch a balance that doesn't exist.
    chain = Chain(replace(PARAMS, min_fee=0))
    creature = chain.hatch_for(ALICE)
    offer = chain.offer(CAROL, ALICE, want_creatures=(creature,), fee=0)
    chain.add([offer], ALICE)
    chain.add([chain.accept(ALICE, offer, fee=0)], ALICE)
    assert chain.state.owners[creature] == CAROL.public_hex


# Expiry


def test_offer_expires_at_its_height(chain, creature):
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=1, expires_at=chain.state.height + 3)
    chain.add([offer])
    chain.add()
    assert offer.id.hex() in chain.state.offers
    assert chain.code_of(chain.accept(BOB, offer)) == Code.OFFER_NOT_OPEN  # would land at the expiry height
    chain.add()
    assert offer.id.hex() not in chain.state.offers


def test_offer_can_be_accepted_in_the_last_block_before_it_expires(chain, creature):
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=1, expires_at=chain.state.height + 3)
    chain.add([offer])
    chain.add([chain.accept(BOB, offer)])
    assert chain.state.owners[creature] == BOB.public_hex


def test_expired_offers_leave_state(chain):
    chain.add([chain.offer(ALICE, BOB, give_coins=1, ttl=2)])
    chain.add()
    chain.add()
    assert chain.state.offers == {}


@pytest.mark.parametrize("delta", [0, 1])
def test_offer_must_expire_after_the_block_it_is_in(chain, delta):
    assert chain.code_of(chain.offer(ALICE, BOB, give_coins=1, expires_at=chain.state.height + delta)) == \
        Code.INVALID_OFFER


def test_offer_can_expire_just_after_its_block(chain):
    assert chain.code_of(chain.offer(ALICE, BOB, give_coins=1, expires_at=chain.state.height + 2)) is None


def test_offer_lifetime_limit(chain):
    limit = chain.state.height + 1 + PARAMS.max_offer_ttl
    assert chain.code_of(chain.offer(ALICE, BOB, give_coins=1, expires_at=limit)) is None
    assert chain.code_of(chain.offer(ALICE, BOB, give_coins=1, expires_at=limit + 1)) == Code.INVALID_OFFER


def test_open_offers_per_sender_are_capped(chain):
    offers = [chain.offer(ALICE, BOB, give_coins=1, nonce=i) for i in range(PARAMS.max_open_offers)]
    chain.add(offers)
    assert chain.code_of(chain.offer(ALICE, BOB, give_coins=1)) == Code.TOO_MANY_OFFERS
    assert chain.code_of(chain.offer(BOB, ALICE, give_coins=1)) is None  # someone else's limit is separate

    chain.add([chain.reject(ALICE, offers[0])])
    assert chain.code_of(chain.offer(ALICE, BOB, give_coins=1)) is None


# Format


HASH_A, HASH_B = "aa" * 32, "bb" * 32


@pytest.mark.parametrize("terms", [
    {"give_coins": 1, "want_coins": 1},
    {},
    {"give_creatures": (HASH_B, HASH_A), "want_coins": 1},
    {"give_creatures": (HASH_A, HASH_A), "want_coins": 1},
    {"want_creatures": (HASH_B, HASH_A), "give_coins": 1},
    {"want_creatures": (HASH_A, HASH_A), "give_coins": 1},
    {"give_creatures": (HASH_A,), "want_creatures": (HASH_A,)},
    {"give_creatures": tuple(c * 64 for c in "abcd"), "want_coins": 1},
    {"want_creatures": tuple(c * 64 for c in "abcd"), "give_coins": 1},
])
def test_invalid_terms(chain, terms):
    assert shape_code(chain.offer(ALICE, BOB, **terms)) == Code.INVALID_OFFER


def test_creature_limit_is_exact(chain):
    creatures = tuple(c * 64 for c in "abc")
    assert shape_code(chain.offer(ALICE, BOB, give_creatures=creatures, want_coins=1)) is None


def test_offer_to_yourself_is_rejected(chain):
    assert shape_code(chain.offer(ALICE, ALICE, give_coins=1)) == Code.SELF_TRANSFER


def test_low_order_recipient_is_rejected(chain):
    tx = chain.offer(ALICE, BOB, give_coins=1)
    assert shape_code(replace(tx, body={**tx.body, "to": "01" + "00" * 31})) == Code.INVALID_PUBLIC_KEY


def test_fee_below_minimum(chain):
    assert shape_code(chain.offer(ALICE, BOB, give_coins=1, fee=0)) == Code.FEE_TOO_LOW
    assert shape_code(chain.accept(BOB, HASH_A, fee=0)) == Code.FEE_TOO_LOW
    assert shape_code(chain.reject(BOB, HASH_A, fee=0)) == Code.FEE_TOO_LOW


@pytest.mark.parametrize("field, value", [
    ("to", CAROL.public_hex), ("give_coins", 7), ("give_creatures", [HASH_A]), ("want_coins", 6),
    ("want_creatures", [HASH_B]), ("expires_at", 999), ("counter_of", HASH_A),
])
def test_changing_any_term_after_signing_breaks_the_signature(chain, field, value):
    terms = {"want_coins": 5} if field == "want_coins" else {"give_coins": 1}
    tx = chain.offer(ALICE, BOB, **terms)
    assert shape_code(replace(tx, body={**tx.body, field: value})) == Code.BAD_SIGNATURE


def test_every_offer_field_is_covered_by_the_table_above(chain):
    assert set(chain.offer(ALICE, BOB, give_coins=1).body) == {"to", "give_coins", "give_creatures", "want_coins",
                                                                "want_creatures", "expires_at", "counter_of"}


def test_changing_the_offer_id_after_signing_breaks_the_signature(chain):
    for tx in (chain.accept(BOB, HASH_A), chain.reject(BOB, HASH_A)):
        assert shape_code(replace(tx, body={"offer_id": HASH_B})) == Code.BAD_SIGNATURE


@pytest.mark.parametrize("edit", [
    lambda body: body.update(memo="free text"),
    lambda body: body.pop("counter_of"),
    lambda body: body.update(give_coins=True),
    lambda body: body.update(give_coins=-1),
    lambda body: body.update(give_coins=1.5),
    lambda body: body.update(want_coins="1"),
    lambda body: body.update(expires_at=-1),
    lambda body: body.update(give_creatures="x"),
    lambda body: body.update(give_creatures=["Fluffy"]),
    lambda body: body.update(want_creatures=[HASH_A.upper()]),
    lambda body: body.update(want_creatures=[1]),
    lambda body: body.update(counter_of="please"),
    lambda body: body.update(counter_of=5),
])
def test_malformed_offers_are_refused_when_parsed(chain, edit):
    data = chain.offer(ALICE, BOB, give_coins=1).to_dict()
    edit(data["body"])
    with pytest.raises(ChainError) as e:
        Transaction.from_dict(data)
    assert e.value.code == Code.MALFORMED


@pytest.mark.parametrize("body", [{"offer_id": "x"}, {"offer_id": 5}, {}, {"offer_id": HASH_A, "memo": "hi"}])
def test_malformed_accepts_and_rejects_are_refused(chain, body):
    for tx in (chain.accept(BOB, HASH_A), chain.reject(BOB, HASH_A)):
        with pytest.raises(ChainError):
            Transaction.from_dict({**tx.to_dict(), "body": body})


def test_no_new_kind_has_room_for_free_text(chain):
    """Every string in these bodies is a hash or an address, so the content filter never has to touch the chain."""
    for tx in (chain.offer(ALICE, BOB, give_coins=1, want_creatures=(HASH_A,), counter_of=HASH_B),
               chain.accept(BOB, HASH_A), chain.reject(BOB, HASH_A)):
        for value in tx.body.values():
            for item in value if isinstance(value, list) else [value]:
                assert item is None or isinstance(item, int) or len(item) == 64


def test_offers_round_trip_through_a_dict(chain):
    tx = chain.offer(ALICE, BOB, give_coins=1, want_creatures=(HASH_A,), counter_of=HASH_B)
    assert Transaction.from_dict(tx.to_dict()).id == tx.id


# The mempool


def test_mempool_refuses_what_cannot_run_and_says_why(chain, creature):
    pool = Mempool(PARAMS)
    for tx, code in [
        (chain.offer(BOB, CAROL, give_creatures=(creature,), want_coins=1), Code.NOT_OWNER),
        (chain.accept(BOB, "ab" * 32), Code.OFFER_NOT_OPEN),
        (chain.reject(BOB, "ab" * 32), Code.OFFER_NOT_OPEN),
    ]:
        with pytest.raises(ChainError) as e:
            pool.add(tx, chain.state)
        assert e.value.code == code
    assert len(pool) == 0


def test_mempool_judges_a_transaction_after_the_senders_waiting_ones(chain, creature):
    pool = Mempool(PARAMS)
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=1)
    pool.add(offer, chain.state)
    withdraw = chain.reject(ALICE, offer, nonce=chain.nonce(ALICE) + 1)
    pool.add(withdraw, chain.state)  # only valid because the offer ahead of it opens it
    assert len(pool) == 2

    again = chain.reject(ALICE, offer, nonce=chain.nonce(ALICE) + 2)
    with pytest.raises(ChainError) as e:
        pool.add(again, chain.state)
    assert e.value.code == Code.OFFER_NOT_OPEN


def test_a_later_nonce_is_not_judged_yet(chain):
    pool = Mempool(PARAMS)
    gap = chain.accept(ALICE, "ab" * 32, nonce=chain.nonce(ALICE) + 1)
    pool.add(gap, chain.state)
    assert gap in pool


def test_stale_accept_does_not_block_the_senders_later_transactions(chain):
    """If it stayed at the head of the queue, nothing else from this sender could ever be mined."""
    creature = chain.hatch_for(ALICE)
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=1, ttl=3)
    chain.add([offer])
    pool = Mempool(PARAMS)
    stale = chain.accept(BOB, offer, nonce=chain.nonce(BOB))
    pool.add(stale, chain.state)

    # The offer is withdrawn elsewhere: the accept can never run now.
    block = chain.add([chain.reject(ALICE, offer)])
    dropped = pool.on_new_tip(block, chain.state)
    assert stale not in pool
    assert dropped == [(stale, Code.OFFER_NOT_OPEN)]

    next_tx = chain.pay(BOB, CAROL, 1)
    pool.add(next_tx, chain.state)
    assert pool.select(chain.state, 10) == [next_tx]


def test_stale_transaction_stops_at_expiry_too(chain):
    creature = chain.hatch_for(ALICE)
    offer = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=1, expires_at=chain.state.height + 3)
    chain.add([offer])
    pool = Mempool(PARAMS)
    accept = chain.accept(BOB, offer)
    pool.add(accept, chain.state)
    assert pool.select(chain.state, 10) == [accept]

    block = chain.add()
    assert pool.on_new_tip(block, chain.state) == [(accept, Code.OFFER_NOT_OPEN)]


def test_prune_reports_what_it_dropped_and_why(chain):
    pool = Mempool(PARAMS)
    old = chain.pay(ALICE, BOB, 1)
    too_much = chain.pay(ALICE, DAVE, 900, nonce=chain.nonce(ALICE) + 1)
    pool.add(old, chain.state)
    pool.add(too_much, chain.state)
    state = chain.state.copy()
    state.nonces[ALICE.public_hex] = 1
    state.balances[ALICE.public_hex] = 500
    assert pool.prune(state) == [(old, Code.NONCE_TOO_LOW), (too_much, Code.INSUFFICIENT_BALANCE)]


def test_included_transactions_are_not_reported_as_dropped(chain):
    pool = Mempool(PARAMS)
    tx = chain.pay(ALICE, BOB, 1)
    pool.add(tx, chain.state)
    block = chain.add([tx])
    assert pool.on_new_tip(block, chain.state) == []
    assert tx not in pool


def test_a_later_nonce_is_still_held_to_the_balance(chain):
    # It can't be tried yet, so the plain balance check is all that stops it.
    pool = Mempool(PARAMS)
    with pytest.raises(ChainError) as e:
        pool.add(chain.pay(ALICE, BOB, 5000, nonce=chain.nonce(ALICE) + 3), chain.state)
    assert e.value.code == Code.INSUFFICIENT_BALANCE


def test_prune_catches_a_later_nonce_that_can_no_longer_be_afforded(chain):
    pool = Mempool(PARAMS)
    far_ahead = chain.pay(ALICE, BOB, 900, nonce=chain.nonce(ALICE) + 3)
    pool.add(far_ahead, chain.state)
    poorer = chain.state.copy()
    poorer.balances[ALICE.public_hex] = 100
    assert pool.prune(poorer) == [(far_ahead, Code.INSUFFICIENT_BALANCE)]


def test_removing_a_senders_last_transaction_forgets_the_sender(chain):
    pool = Mempool(PARAMS)
    tx = chain.pay(ALICE, BOB, 1)
    pool.add(tx, chain.state)
    pool.remove(tx)
    assert pool._by_sender == {}


def test_selection_skips_a_transaction_that_a_better_one_invalidates(chain):
    creature = chain.hatch_for(ALICE)
    to_bob = chain.offer(ALICE, BOB, give_creatures=(creature,), want_coins=1)
    to_carol = chain.offer(ALICE, CAROL, give_creatures=(creature,), want_coins=1, nonce=chain.nonce(ALICE) + 1)
    chain.add([to_bob, to_carol])
    pool = Mempool(PARAMS)
    first = chain.accept(BOB, to_bob, fee=5)
    second = chain.accept(CAROL, to_carol, fee=1)
    pool.add(first, chain.state)
    pool.add(second, chain.state)  # fine on its own, but both can't happen
    assert pool.select(chain.state, 10) == [first]


def test_fees_are_all_a_non_transfer_commits_to_up_front(chain):
    assert chain.offer(ALICE, BOB, give_coins=10**6, fee=3).spend == 3
    assert chain.accept(BOB, HASH_A, fee=2).spend == 2
