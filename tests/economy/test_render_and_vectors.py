import hashlib
import json
from dataclasses import replace
from itertools import product
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from agenteconomy.art.parts import CANVAS_PIXELS, PARTS_DIR, part_names
from agenteconomy.art.render import ARM_JOINTS, DETAIL_BASES, EYE_SPACING, LAYOUTS, LEG_JOINTS, render
from agenteconomy.chain.creature import ARMS, DETAILS, EYE_COUNTS, LEGS, SHAPES, SPECIES, hatch
from agenteconomy.chain.errors import ChainError
from agenteconomy.economy.vectors import (
    expected_code,
    params_from_dict,
    params_to_dict,
    render_vectors,
    state_to_dict,
    validate_chain_dicts,
)

VECTORS_PATH = Path(__file__).resolve().parents[2] / "testdata" / "economy_vectors_v1.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="ascii"))
PARAMS = params_from_dict(VECTORS["chain"]["params"])
BASE = hatch("ab" * 32, ("blueA",))


# Parts and rendering. These use the committed parts, so they also run in CI, which has no raw pack.


def test_every_part_a_creature_can_use_exists():
    missing = [name for name in part_names() if not (PARTS_DIR / f"{name}.png").is_file()]
    assert not missing


def test_no_stray_files_in_parts():
    expected = {f"{name}.png" for name in part_names()} | {"README.md"}
    assert {path.name for path in PARTS_DIR.iterdir()} == expected


def test_parts_are_small_enough_to_commit():
    assert sum(path.stat().st_size for path in PARTS_DIR.iterdir()) < 2_000_000


def test_tables_cover_every_option():
    assert set(LAYOUTS) == set(SHAPES)
    assert set(ARM_JOINTS) == set(ARMS)
    assert set(LEG_JOINTS) == set(LEGS)
    assert set(DETAIL_BASES) == {detail for detail in DETAILS if detail}
    assert set(EYE_SPACING) == set(EYE_COUNTS)


# Pixel hashes of creatures checked by eye on a contact sheet. If one changes, the art changed: look at
# scripts/render_sheet.py's output, and update these only if it still looks right.
GOLDEN = {
    "blueA": ("golden-1", "4264647fec3125fd29403b0ff41caf7cd563eb616d4dda63d9153933c39401db"),
    "greenB": ("golden-2", "d65c061ef58d946da6a4b90e195344fac1d89e7c14cf5e98cc54f1147b77a604"),
    "redC": ("golden-3", "1b2993d2bc1c1d91e6a8ccfbe4e358460f2094e485da6c3f324e4d6146f62507"),
    "yellowD": ("golden-4", "f09094bee2cbe6221e6e81e782f53f272ff864f5d90b8c079b67efd98a0e45a2"),
    "whiteE": ("golden-5", "f13c0ebb0ef1753cc8fae17ad88c551e9fd8e684e041893863317dda19145308"),
    "darkF": ("golden-6", "d0dca4c3a5812e2d93b15ce400f8842f4f8786d91f72745eca93f33078289969"),
}


@pytest.mark.parametrize("species", GOLDEN)
def test_creatures_look_the_way_they_were_approved(species):
    salt, expected = GOLDEN[species]
    creature = hatch(hashlib.sha256(salt.encode()).hexdigest(), (species,))
    assert hashlib.sha256(render(creature).tobytes()).hexdigest() == expected


def test_rendering_is_deterministic():
    assert render(BASE).tobytes() == render(BASE).tobytes()


def test_render_is_the_right_size_with_a_transparent_background():
    image = render(BASE)
    assert image.size == (CANVAS_PIXELS, CANVAS_PIXELS)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0


def _opaque_bounds(image: Image.Image) -> tuple[int, int, int, int]:
    return image.getchannel("A").getbbox()


def _changed_area(first: Image.Image, second: Image.Image) -> tuple[int, int, int, int] | None:
    # alpha_only=False matters: by default getbbox ignores any change that's only in colour.
    return ImageChops.difference(first, second).getbbox(alpha_only=False)


@pytest.mark.parametrize("shape", SHAPES)
def test_no_combination_runs_off_the_canvas(shape):
    # Every limb and detail on every body. A part that hits the edge would be visibly cut off.
    margin = 1
    for arms, legs, detail in product(ARMS, LEGS, DETAILS):
        creature = replace(BASE, species=f"blue{shape}", arms=arms, legs=legs, detail=detail, eye_count=3)
        left, top, right, bottom = _opaque_bounds(render(creature))
        assert left >= margin and top >= margin, (arms, legs, detail)
        assert right <= CANVAS_PIXELS - margin and bottom <= CANVAS_PIXELS - margin, (arms, legs, detail)


@pytest.mark.parametrize("attribute, value", [
    ("arms", "C"), ("legs", "C"), ("eyes", "dead"), ("eye_count", 3), ("mouth", "mouthB"), ("nose", "red"),
    ("detail", "horn_large"), ("eyebrows", "eyebrowA"), ("species", "redA"),
])
def test_each_trait_changes_the_picture(attribute, value):
    plain = replace(BASE, arms="A", legs="A", eyes="blue", eye_count=2, mouth="mouthA", nose=None, detail=None,
                    eyebrows=None, species="blueA")
    changed = replace(plain, **{attribute: value})
    assert _changed_area(render(plain), render(changed)) is not None


def test_limbs_are_symmetrical_around_the_body():
    # Mirroring a creature with no asymmetric face parts should nearly reproduce it.
    plain = replace(BASE, arms="B", legs="B", eyes="closed_happy", eye_count=1, mouth="mouth_closed_happy", nose=None,
                    detail="horn_small", eyebrows=None, species="blueB")
    image = render(plain)
    left, top, right, bottom = _opaque_bounds(image)
    assert abs((CANVAS_PIXELS - right) - left) <= 2


def test_eyebrows_skip_a_third_eye():
    with_brows = replace(BASE, eye_count=3, eyebrows="eyebrowA", detail=None)
    without = replace(with_brows, eyebrows=None)
    three_eyes = _changed_area(render(with_brows), render(without))
    two_eyes = _changed_area(render(replace(with_brows, eye_count=2)), render(replace(without, eye_count=2)))
    # The lower two eyes of three sit 8 raw pixels lower than a pair, and a brow on the top eye would be far higher.
    assert three_eyes[1] == two_eyes[1] + 4


def test_any_hatched_creature_renders():
    for species in SPECIES:
        value = hashlib.sha256(species.encode()).hexdigest()
        assert _opaque_bounds(render(hatch(value, (species,)))) is not None


def test_raw_pack_is_never_required_to_render(monkeypatch, tmp_path):
    monkeypatch.setattr("agenteconomy.art.parts.RAW_DIR", tmp_path / "does-not-exist")
    assert render(BASE) is not None


# Vectors. scripts/verify_vectors.mjs checks the same file independently in JS.


def test_regenerating_changes_nothing():
    # Failing here means the format changed, or the simulation did: a real simulated chain is in this file.
    # If that's deliberate, run scripts/generate_vectors.py.
    assert VECTORS_PATH.read_bytes() == render_vectors().encode("ascii")


def test_params_round_trip():
    assert params_from_dict(params_to_dict(PARAMS)) == PARAMS


@pytest.mark.parametrize("case", VECTORS["hatching"], ids=lambda case: case["hash"][:8])
def test_hatching_vectors(case):
    assert hatch(case["hash"], tuple(case["pool"])).to_dict() == case["creature"]


@pytest.mark.parametrize("case", VECTORS["tampered_chains"], ids=lambda case: case["name"])
def test_tampered_chains_get_the_stated_code(case):
    assert expected_code(case, PARAMS) == case["code"]


def test_tampered_chains_are_broken_only_in_their_last_block():
    # Otherwise a case could pass for a reason other than the one it names.
    for case in VECTORS["tampered_chains"]:
        if case["name"] == "accept of an expired offer":
            continue
        validate_chain_dicts(case["blocks"][:-1], PARAMS)


def test_every_chain_in_the_file_replays_to_its_stated_state():
    for section in ("chain", "cap_chain", "simulated_chain"):
        data = VECTORS[section]
        state = validate_chain_dicts(data["blocks"], params_from_dict(data["params"]))
        assert json.loads(json.dumps(state_to_dict(state))) == data["final_state"], section


def test_overflow_chain_is_refused():
    data = VECTORS["overflow_chain"]
    assert expected_code(data, params_from_dict(data["params"])) == data["code"]


def test_cap_chain_really_reaches_the_cap_and_recovers():
    data = VECTORS["cap_chain"]
    amounts = [block["txs"][0]["body"]["amount"] for block in data["blocks"][1:]]
    assert 0 in amounts
    assert amounts[-1] > 0
    params = params_from_dict(data["params"])
    assert expected_code(data["tampered"], params) == data["tampered"]["code"]


def test_simulated_chain_really_has_an_economy():
    data = VECTORS["simulated_chain"]
    kinds = {tx["kind"] for block in data["blocks"] for tx in block["txs"]}
    assert {"offer", "accept"} <= kinds
    assert data["trades"] > 0
    assert data["final_state"]["creatures"]


def test_vectors_cover_a_broad_set_of_codes():
    codes = {case["code"] for case in VECTORS["tampered_chains"]}
    assert {"not_owner", "offer_not_open", "not_offer_party", "invalid_offer", "too_many_offers",
            "insufficient_balance", "bad_coinbase", "bad_signature", "malformed", "self_transfer"} <= codes


def test_a_vector_that_should_fail_really_raises():
    with pytest.raises(ChainError):
        validate_chain_dicts(VECTORS["tampered_chains"][0]["blocks"], PARAMS)
