from dataclasses import replace

import pytest
from chain_helpers import ALICE, BOB, CAROL, PARAMS, pay

from agenteconomy.chain.canonical import TX_SIGN_TAG, canonical_json, tagged_bytes
from agenteconomy.chain.errors import ChainError, Code
from agenteconomy.chain.params import ChainParams
from agenteconomy.chain.transaction import Transaction, check_transaction, make_coinbase, make_genesis_tx
from agenteconomy.chain.vectors import malleate_signature

LOW_ORDER = "01" + "00" * 31


def code_of(tx: Transaction, params: ChainParams = PARAMS) -> Code | None:
    try:
        check_transaction(tx, params)
    except ChainError as e:
        return e.code
    return None


def test_signed_transfer_is_valid():
    assert code_of(pay()) is None


def test_round_trip_through_a_dict():
    tx = pay()
    assert Transaction.from_dict(tx.to_dict()) == tx
    assert Transaction.from_dict(tx.to_dict()).id == tx.id


def test_signing_bytes_cover_everything_but_the_signature():
    tx = pay()
    unsigned = {key: value for key, value in tx.to_dict().items() if key != "signature"}
    assert tx.signing_bytes == tagged_bytes(TX_SIGN_TAG, canonical_json(unsigned))


def test_id_changes_with_the_signature():
    tx = pay()
    assert replace(tx, signature="00" * 64).id != tx.id


@pytest.mark.parametrize("changes, code", [
    ({"version": 2}, Code.UNSUPPORTED_VERSION),
    ({"version": True}, Code.MALFORMED),
    ({"chain_id": "other-chain"}, Code.WRONG_CHAIN),
    ({"chain_id": "NOT VALID"}, Code.MALFORMED),
    ({"kind": "mint"}, Code.UNKNOWN_KIND),
    ({"sender": CAROL.public_hex}, Code.BAD_SIGNATURE),
    ({"nonce": 7}, Code.BAD_SIGNATURE),
    ({"fee": 9}, Code.BAD_SIGNATURE),
    ({"body": {"to": CAROL.public_hex, "amount": 10}}, Code.BAD_SIGNATURE),
    ({"body": {"to": BOB.public_hex, "amount": 11}}, Code.BAD_SIGNATURE),
    ({"signature": "00" * 64}, Code.BAD_SIGNATURE),
    ({"signature": None}, Code.MALFORMED),
])
def test_changing_any_field_is_caught(changes, code):
    assert code_of(replace(pay(), **changes)) == code


def test_every_field_is_covered_by_the_table_above():
    # If a field gets added to transactions, this forces a decision about its tamper test.
    assert set(pay().to_dict()) == {"version", "chain_id", "kind", "sender", "nonce", "fee", "body", "signature"}


def test_forged_sender_is_rejected():
    # Bob signs a payment out of Alice's account.
    forged = replace(pay(BOB, CAROL), sender=ALICE.public_hex)
    assert code_of(forged) == Code.BAD_SIGNATURE


def test_oversized_signature_form_is_rejected():
    tx = pay()
    assert code_of(replace(tx, signature=malleate_signature(tx.signature))) == Code.BAD_SIGNATURE


def test_signature_for_another_chain_is_rejected_even_if_chain_id_is_rewritten():
    tx = pay(chain_id="other-chain")
    assert code_of(tx) == Code.WRONG_CHAIN
    assert code_of(replace(tx, chain_id=PARAMS.chain_id)) == Code.BAD_SIGNATURE


def test_self_transfer_is_rejected():
    assert code_of(pay(ALICE, ALICE)) == Code.SELF_TRANSFER


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amounts_are_rejected(amount):
    assert code_of(pay(amount=amount)) == Code.INVALID_AMOUNT


def test_fee_below_the_minimum_is_rejected():
    assert code_of(pay(fee=0)) == Code.FEE_TOO_LOW


def test_low_order_sender_with_forged_signature_is_rejected():
    tx = replace(pay(), sender=LOW_ORDER, signature=LOW_ORDER + "00" * 32)
    assert code_of(tx) == Code.INVALID_PUBLIC_KEY


def test_low_order_recipient_is_rejected():
    # Anyone could forge spends from that address, so coins must not reach it.
    tx = pay()
    assert code_of(replace(tx, body={**tx.body, "to": LOW_ORDER})) == Code.INVALID_PUBLIC_KEY


@pytest.mark.parametrize("edit", [
    lambda d: d["body"].update(memo="free text"),
    lambda d: d.update(memo="free text"),
    lambda d: d.pop("fee"),
    lambda d: d["body"].pop("amount"),
    lambda d: d.update(nonce=True),
    lambda d: d.update(nonce=1.0),
    lambda d: d.update(nonce=-1),
    lambda d: d.update(nonce=2**53),
    lambda d: d.update(fee="1"),
    lambda d: d.update(sender=d["sender"].upper()),
    lambda d: d.update(sender=d["sender"] + "\n"),
    lambda d: d.update(sender="ab"),
    lambda d: d.update(signature=d["signature"].upper()),
    lambda d: d["body"].update(to=d["body"]["to"].upper()),
    lambda d: d["body"].update(amount=1.5),
    lambda d: d["body"].update(amount=True),
    lambda d: d["body"].update(amount=2**53),
    lambda d: d.update(body=[]),
])
def test_malformed_data_is_refused_when_parsed(edit):
    data = pay().to_dict()
    data["body"] = dict(data["body"])
    edit(data)
    with pytest.raises(ChainError) as e:
        Transaction.from_dict(data)
    assert e.value.code == Code.MALFORMED


@pytest.mark.parametrize("data", [None, [], "tx", {}])
def test_non_transaction_data_is_refused(data):
    with pytest.raises(ChainError):
        Transaction.from_dict(data)


def test_free_text_has_nowhere_to_go():
    """Every string on a transaction must be hex or a known value, so the content filter never has to remove one."""
    for tx in (pay(), make_coinbase(PARAMS, to=ALICE.public_hex, height=1),
               make_genesis_tx(PARAMS, to=ALICE.public_hex, amount=1)):
        Transaction.from_dict(tx.to_dict())
        for field, bad in (("chain_id", "Hello there"), ("kind", "Hello there")):
            with pytest.raises(ChainError):
                Transaction.from_dict({**tx.to_dict(), field: bad})
        with pytest.raises(ChainError):
            Transaction.from_dict({**tx.to_dict(), "body": {**tx.body, "to": "Hello there"}})


def test_protocol_kinds_cannot_carry_a_sender_or_signature():
    coinbase = make_coinbase(PARAMS, to=ALICE.public_hex, height=1)
    for changes in ({"sender": ALICE.public_hex}, {"signature": "00" * 64}, {"nonce": 1}, {"fee": 1}):
        assert code_of(replace(coinbase, **changes)) == Code.MALFORMED


def test_coinbases_at_different_heights_have_different_ids():
    first = make_coinbase(PARAMS, to=ALICE.public_hex, height=1)
    assert first.id != make_coinbase(PARAMS, to=ALICE.public_hex, height=2).id


@pytest.mark.parametrize("kind", [5, None, ["transfer"], {"a": 1}])
def test_kind_of_the_wrong_type_is_malformed_not_a_crash(kind):
    assert code_of(replace(pay(), kind=kind)) == Code.MALFORMED


@pytest.mark.parametrize("height", [-1, True, "1", 1.5, 2**53])
def test_coinbase_height_of_the_wrong_type_is_malformed(height):
    coinbase = make_coinbase(PARAMS, to=ALICE.public_hex, height=1)
    assert code_of(replace(coinbase, body={**coinbase.body, "height": height})) == Code.MALFORMED
