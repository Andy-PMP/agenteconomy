"""Rare events. Coded, not tied to real-world data.

Rolled from the seed, the day and the previous day's tip: reproducible and checkable afterward, but nobody can compute
tomorrow's crash before today has settled, which keeps visitors' predictions honest. Shocks act only through values and
hash budgets. Chain rules never change, so no shock can touch a balance or a creature directly.
"""

import hashlib
from dataclasses import dataclass

from .market import FULL, Conditions

SHOCK_TAG = b"agenteconomy/shock/v1\x00"

MARKET_CRASH = "market_crash"
MINING_BOOM = "mining_boom"
MINING_DROUGHT = "mining_drought"
COLLECTOR_CRAZE = "collector_craze"

# One day in this many, on average.
ODDS = {MARKET_CRASH: 365, MINING_BOOM: 60, MINING_DROUGHT: 60, COLLECTOR_CRAZE: 45}

CRASH_VALUE_BPS = 4_000  # values fall to 40%
CRASH_RECOVERY_DAYS = 7
CRAZE_BPS = 20_000
BOOM_BPS = 20_000
DROUGHT_BPS = 5_000


@dataclass(frozen=True)
class Shock:
    kind: str
    day: int
    species: str | None = None  # the species a collector craze is about

    def to_dict(self) -> dict:
        return {"kind": self.kind, "day": self.day, "species": self.species}


def _roll(seed: str, day: int, previous_tip: str, label: str) -> int:
    data = f"{seed}|{day}|{previous_tip}|{label}".encode()
    return int.from_bytes(hashlib.sha256(SHOCK_TAG + data).digest(), "big")


def roll_shocks(seed: str, day: int, previous_tip: str, pool: tuple[str, ...]) -> list[Shock]:
    shocks = []
    for kind, odds in ODDS.items():
        if _roll(seed, day, previous_tip, kind) % odds == 0:
            species = pool[_roll(seed, day, previous_tip, f"{kind}:species") % len(pool)] \
                if kind == COLLECTOR_CRAZE else None
            shocks.append(Shock(kind, day, species))
    return shocks


def conditions_for(day: int, history: list[Shock]) -> Conditions:
    """Today's market, from every shock so far. A crash fades over a week; the others last one day."""
    value_bps = FULL
    species_bps: dict[str, int] = {}
    budget_bps = FULL
    for shock in history:
        age = day - shock.day
        if age < 0:
            continue
        if shock.kind == MARKET_CRASH and age < CRASH_RECOVERY_DAYS:
            recovered = CRASH_VALUE_BPS + (FULL - CRASH_VALUE_BPS) * age // CRASH_RECOVERY_DAYS
            value_bps = min(value_bps, recovered)
        elif age == 0:
            if shock.kind == MINING_BOOM:
                budget_bps = budget_bps * BOOM_BPS // FULL
            elif shock.kind == MINING_DROUGHT:
                budget_bps = budget_bps * DROUGHT_BPS // FULL
            elif shock.kind == COLLECTOR_CRAZE:
                species_bps[shock.species] = CRAZE_BPS
    return Conditions(value_bps, species_bps, budget_bps)
