"""Creatures: everything about one comes from the hash of the block that hatched it.

Nothing about a creature is stored on-chain except its ID, the block's hash, in offers. Anyone with these tables can
derive the rest. Every roll re-hashes the block hash: a mined hash starts with zeros, so its raw bits are biased.
"""

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

HATCH_TAG = b"agenteconomy/hatch/v1\x00"

SHAPES = ("A", "B", "C", "D", "E", "F")
COLOURS = ("blue", "dark", "green", "red", "white", "yellow")
SPECIES = tuple(f"{colour}{shape}" for colour in COLOURS for shape in SHAPES)

ARMS = ("A", "B", "C", "D", "E")
LEGS = ("A", "B", "C", "D", "E")
EYES = ("blue", "yellow", "red", "human", "human_blue", "human_green", "human_red", "cute_light", "cute_dark",
        "angry_blue", "angry_green", "angry_red", "psycho_light", "psycho_dark", "closed_happy", "closed_feminine",
        "dead")
EYE_COUNTS = (1, 1, 2, 2, 2, 2, 3)  # repeats set the odds: mostly two eyes, sometimes one or three
MOUTHS = ("mouthA", "mouthB", "mouthC", "mouthD", "mouthE", "mouthF", "mouthG", "mouthH", "mouthI", "mouthJ",
          "mouth_closed_fangs", "mouth_closed_happy", "mouth_closed_sad", "mouth_closed_teeth")
NOSES = (None, None, None, "brown", "green", "red", "yellow")
DETAILS = (None, "antenna_large", "antenna_small", "ear", "ear_round", "eye", "horn_large", "horn_small")
EYEBROWS = (None, None, None, "eyebrowA", "eyebrowB", "eyebrowC")

# Chance in 1000, checked in order, with the bonus added to every stat.
RARITIES = (("legendary", 5, 20), ("rare", 50, 10), ("uncommon", 250, 5), ("common", 1000, 0))
MAX_STAT = 100


@dataclass(frozen=True)
class Creature:
    id: str
    species: str
    rarity: str
    arms: str
    legs: str
    eyes: str
    eye_count: int
    mouth: str
    nose: str | None
    detail: str | None
    eyebrows: str | None
    attack: int
    defense: int
    speed: int
    health: int

    @property
    def shape(self) -> str:
        return self.species[-1]

    @property
    def colour(self) -> str:
        return self.species[:-1]

    @property
    def stat_total(self) -> int:
        return self.attack + self.defense + self.speed + self.health

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _roll(block_hash: bytes, label: str) -> int:
    return int.from_bytes(hashlib.sha256(HATCH_TAG + block_hash + label.encode("ascii")).digest(), "big")


def _pick(block_hash: bytes, label: str, options: tuple) -> Any:
    return options[_roll(block_hash, label) % len(options)]


def _stat(block_hash: bytes, name: str, bonus: int) -> int:
    # Three dice of 1 to 33, so most creatures are near the middle and extremes are rare.
    total = sum(_roll(block_hash, f"{name}{die}") % 33 + 1 for die in range(3))
    return min(MAX_STAT, total + bonus)


def hatch(block_hash_hex: str, pool: tuple[str, ...]) -> Creature:
    block_hash = bytes.fromhex(block_hash_hex)
    rarity_roll = _roll(block_hash, "rarity") % 1000
    rarity, bonus = next((name, bonus) for name, limit, bonus in RARITIES if rarity_roll < limit)
    return Creature(
        id=block_hash_hex,
        species=_pick(block_hash, "species", pool),
        rarity=rarity,
        arms=_pick(block_hash, "arms", ARMS),
        legs=_pick(block_hash, "legs", LEGS),
        eyes=_pick(block_hash, "eyes", EYES),
        eye_count=_pick(block_hash, "eye_count", EYE_COUNTS),
        mouth=_pick(block_hash, "mouth", MOUTHS),
        nose=_pick(block_hash, "nose", NOSES),
        detail=_pick(block_hash, "detail", DETAILS),
        eyebrows=_pick(block_hash, "eyebrows", EYEBROWS),
        attack=_stat(block_hash, "attack", bonus),
        defense=_stat(block_hash, "defense", bonus),
        speed=_stat(block_hash, "speed", bonus),
        health=_stat(block_hash, "health", bonus),
    )


def hatching_tables() -> dict[str, Any]:
    """Everything a verifier needs besides the code, for the published vectors."""
    return {"arms": ARMS, "legs": LEGS, "eyes": EYES, "eye_counts": EYE_COUNTS, "mouths": MOUTHS, "noses": NOSES,
            "details": DETAILS, "eyebrows": EYEBROWS, "rarities": RARITIES, "max_stat": MAX_STAT}
