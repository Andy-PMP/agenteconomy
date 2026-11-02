"""Ed25519 keys. An address is the hex public key. The primitives come from `cryptography`; never hand-roll them."""

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# Encodings of low-order points. For these keys a forged signature can verify against ANY message. OpenSSL accepted
# one in testing while Node's WebCrypto refused it, so without this check the backend and a browser could disagree
# about a chain. The list follows libsodium's blocklist, in both sign-bit variants.
_LOW_ORDER_KEYS = frozenset(
    base[:-2] + f"{int(base[-2:], 16) | sign:02x}"
    for sign in (0x00, 0x80)
    for base in (
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0100000000000000000000000000000000000000000000000000000000000000",
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    )
)


class KeyPair:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._private_key = private_key
        self.public_hex = private_key.public_key().public_bytes_raw().hex()

    @classmethod
    def generate(cls) -> KeyPair:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> KeyPair:
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    def sign(self, message: bytes) -> str:
        return self._private_key.sign(message).hex()

    def __repr__(self) -> str:
        return f"KeyPair(public_hex={self.public_hex!r})"  # never the private key: this could end up in a log


def is_usable_public_key(public_hex: str) -> bool:
    if public_hex in _LOW_ORDER_KEYS:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
    except ValueError:
        return False
    return True


def verify(public_hex: str, signature_hex: str, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(bytes.fromhex(signature_hex), message)
    except (InvalidSignature, ValueError):
        return False
    return True
