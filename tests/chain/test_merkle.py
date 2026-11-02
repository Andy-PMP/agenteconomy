import hashlib

import pytest

from agenteconomy.chain.merkle import merkle_proof, merkle_root, verify_proof


def item(i: int) -> bytes:
    return hashlib.sha256(str(i).encode()).digest()


def sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def test_known_structure_by_hand():
    a, b, c = item(1), item(2), item(3)
    la, lb, lc = sha(b"\x00" + a), sha(b"\x00" + b), sha(b"\x00" + c)
    assert merkle_root([a]) == la
    assert merkle_root([a, b]) == sha(b"\x01" + la + lb)
    # The odd leaf moves up unchanged.
    assert merkle_root([a, b, c]) == sha(b"\x01" + sha(b"\x01" + la + lb) + lc)


def test_empty_list_is_an_error():
    with pytest.raises(ValueError):
        merkle_root([])


def test_order_matters():
    assert merkle_root([item(1), item(2)]) != merkle_root([item(2), item(1)])


def test_any_changed_item_changes_the_root():
    items = [item(i) for i in range(7)]
    root = merkle_root(items)
    for i in range(7):
        assert merkle_root([*items[:i], item(100), *items[i + 1:]]) != root


@pytest.mark.parametrize("count", [3, 5, 6, 7])
def test_repeating_the_last_item_changes_the_root(count):
    # CVE-2012-2459: in Bitcoin's tree, [a, b, c] and [a, b, c, c] share a root.
    items = [item(i) for i in range(count)]
    assert merkle_root(items) != merkle_root([*items, items[-1]])


def test_inner_node_cannot_pass_as_a_leaf():
    # Second-preimage attack: claim the two children of an inner node are themselves a one-item tree.
    a, b = item(1), item(2)
    inner = sha(b"\x00" + a) + sha(b"\x00" + b)
    assert merkle_root([a, b]) != merkle_root([inner])
    assert not verify_proof(inner, [], merkle_root([a, b]))


@pytest.mark.parametrize("count", range(1, 20))
def test_every_proof_verifies(count):
    items = [item(i) for i in range(count)]
    root = merkle_root(items)
    for i in range(count):
        assert verify_proof(items[i], merkle_proof(items, i), root)


def test_proof_fails_for_the_wrong_item_root_or_path():
    items = [item(i) for i in range(6)]
    root = merkle_root(items)
    proof = merkle_proof(items, 2)
    assert not verify_proof(item(99), proof, root)
    assert not verify_proof(items[2], proof, sha(b"other"))
    assert not verify_proof(items[2], merkle_proof(items, 3), root)
    flipped = [("right" if side == "left" else "left", sibling) for side, sibling in proof]
    assert not verify_proof(items[2], flipped, root)


def test_proof_index_out_of_range():
    with pytest.raises(IndexError):
        merkle_proof([item(1)], 1)
