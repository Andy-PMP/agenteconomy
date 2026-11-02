"""The published vectors must match the code. scripts/verify_vectors.mjs checks the same file independently in JS."""

import json
from pathlib import Path

import pytest

from agenteconomy.chain.canonical import canonical_json
from agenteconomy.chain.errors import ChainError
from agenteconomy.chain.vectors import params_from_dict, render_vectors, validate_chain_dicts

VECTORS_PATH = Path(__file__).resolve().parents[2] / "testdata" / "chain_vectors_v1.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="ascii"))
PARAMS = params_from_dict(VECTORS["chain"]["params"])


def test_regenerating_changes_nothing():
    # Failing here means the format changed: every hash on a live chain would change with it.
    # If that's deliberate, run scripts/generate_vectors.py and bump the version.
    assert VECTORS_PATH.read_bytes() == render_vectors().encode("ascii")


@pytest.mark.parametrize("case", VECTORS["canonical"]["valid"], ids=lambda case: case["canonical"][:30])
def test_canonical_vectors(case):
    assert canonical_json(case["input"]).decode("utf-8") == case["canonical"]


@pytest.mark.parametrize("case", VECTORS["canonical"]["reject"], ids=lambda case: case["name"])
def test_canonical_reject_vectors(case):
    with pytest.raises(ChainError):
        canonical_json(case["input"])


def test_sample_chain_is_valid_and_reaches_the_stated_state():
    state = validate_chain_dicts(VECTORS["chain"]["blocks"], PARAMS)
    expected = VECTORS["chain"]["final_state"]
    assert state.balances == expected["balances"]
    assert state.nonces == expected["nonces"]
    assert state.minted == expected["minted"]
    assert state.burned == expected["burned"]
    assert sum(state.balances.values()) == state.supply


@pytest.mark.parametrize("case", VECTORS["tampered_chains"], ids=lambda case: case["name"])
def test_tampered_chains_are_rejected_with_the_stated_code(case):
    with pytest.raises(ChainError) as e:
        validate_chain_dicts(case["blocks"], PARAMS)
    assert e.value.code == case["code"], e.value


def test_vectors_cover_a_broad_set_of_rejection_codes():
    codes = {case["code"] for case in VECTORS["tampered_chains"]}
    assert len(codes) >= 15, sorted(codes)
