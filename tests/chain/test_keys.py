import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agenteconomy.chain.keys import _LOW_ORDER_KEYS, KeyPair, is_usable_public_key, verify
from agenteconomy.chain.vectors import _RFC_8032, malleate_signature, sample_keys

IDENTITY_KEY = "01" + "00" * 31


@pytest.mark.parametrize("vector", _RFC_8032)
def test_rfc_8032_known_answers(vector):
    keys = KeyPair.from_seed(bytes.fromhex(vector["seed"]))
    message = bytes.fromhex(vector["message"])
    assert keys.public_hex == vector["public_key"]
    assert keys.sign(message) == vector["signature"]
    assert verify(vector["public_key"], vector["signature"], message)


def test_signing_is_deterministic():
    assert sample_keys("a").sign(b"m") == sample_keys("a").sign(b"m")


def test_generated_keys_differ_and_work():
    first, second = KeyPair.generate(), KeyPair.generate()
    assert first.public_hex != second.public_hex
    assert verify(first.public_hex, first.sign(b"m"), b"m")
    assert not verify(second.public_hex, first.sign(b"m"), b"m")


def test_verify_refuses_changed_message_and_signature():
    keys = sample_keys("a")
    signature = keys.sign(b"m")
    assert not verify(keys.public_hex, signature, b"n")
    flipped = f"{int(signature[0], 16) ^ 1:x}{signature[1:]}"
    assert not verify(keys.public_hex, flipped, b"m")


def test_verify_refuses_s_above_the_group_order():
    # Otherwise anyone could change a signature, and so a transaction ID, without the key.
    keys = sample_keys("a")
    assert not verify(keys.public_hex, malleate_signature(keys.sign(b"m")), b"m")


@pytest.mark.parametrize("public, signature", [
    ("zz" * 32, "00" * 64),
    ("00" * 31, "00" * 64),
    (sample_keys("a").public_hex, "00" * 63),
])
def test_verify_returns_false_on_bad_input_rather_than_raising(public, signature):
    assert verify(public, signature, b"m") is False


def test_why_low_order_keys_are_blocked():
    # Documents the attack: the library alone accepts this forgery for any message. If this ever fails, the
    # library started refusing it, which is fine; the check below still stands.
    forged = bytes.fromhex(IDENTITY_KEY + "00" * 32)
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(IDENTITY_KEY)).verify(forged, b"pay the attacker")


@pytest.mark.parametrize("key", sorted(_LOW_ORDER_KEYS))
def test_low_order_keys_are_unusable(key):
    assert not is_usable_public_key(key)


def test_normal_keys_are_usable():
    assert is_usable_public_key(sample_keys("a").public_hex)


def test_repr_never_shows_the_private_key():
    seed = bytes(range(32))
    text = repr(KeyPair.from_seed(seed))
    assert seed.hex() not in text
    assert "private" not in text.lower()
