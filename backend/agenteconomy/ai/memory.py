"""What an agent carries into tomorrow: mostly facts built by code, plus one short note it wrote itself.

Facts can't be manipulated by anything an agent read. The note can, so it's short and cleaned here, and P5 checks it for
injected instructions.
"""

from dataclasses import dataclass, field
from typing import Any

from ..economy.tools import DayFacts
from .tool_schemas import clean_text

DAYS_KEPT = 3
MAX_NOTE = 200
MAX_TRADES_PER_DAY = 3


@dataclass
class AgentMemory:
    days: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def remember_day(self, facts: DayFacts) -> None:
        self.days.append({
            "day": facts.day,
            "coins": [facts.coins_start, facts.coins_end],
            "net_worth": [facts.worth_start, facts.worth_end],
            "blocks_mined": facts.blocks_mined,
            "trades": list(facts.trades[-MAX_TRADES_PER_DAY:]),
            "failed_actions": facts.failed_actions,
            "dropped_transactions": facts.dropped_transactions,
            "shocks": list(facts.shocks),
        })
        del self.days[:-DAYS_KEPT]

    def set_note(self, note: Any) -> None:
        """An empty note changes nothing, so a failed write can't erase the last good one."""
        self.note = clean_text(note, MAX_NOTE) or self.note

    def for_model(self) -> dict[str, Any]:
        return {"recent_days": self.days, "your_note": self.note}

    def to_dict(self) -> dict[str, Any]:
        return {"days": self.days, "note": self.note}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentMemory:
        return cls(list(data["days"]), data["note"])
