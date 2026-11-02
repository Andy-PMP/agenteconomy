"""The four agents, their prompts, and what a day records so it can be reconstructed later."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..chain.keys import KeyPair
from ..economy.world import Member
from .agent import ACTION_MAX_TOKENS, CALLS_PER_TURN, NOTE_MAX_TOKENS, AIAgent
from .budget import ModelCaller
from .tool_schemas import tools_hash

PROMPTS = Path(__file__).parent / "prompts"
PERSONALITIES = ("collector", "speculator", "saver", "scammer")


def system_prompt(personality: str) -> str:
    shared = (PROMPTS / "shared.md").read_text(encoding="utf-8")
    return f"{shared}\n{(PROMPTS / f'{personality}.md').read_text(encoding='utf-8')}"


@dataclass
class AIRun:
    """Plugs model accounting into a World's day."""

    caller: ModelCaller
    agents: list[AIAgent]

    def begin_day(self, day: int) -> None:
        self.caller.begin_day(day)

    def totals(self) -> dict[str, int]:
        attempts = [record for record in self.caller.records if record["outcome"] != "cost_cap"]
        return {"model_calls": len(attempts), "model_cost_nanos": self.caller.spent_today,
                "model_errors": self.caller.errors, "refused_calls": self.caller.refused}

    def day_record(self) -> dict[str, Any]:
        """Model version, settings and every input for the day."""
        return {
            "day": self.caller.day,
            "model_id": self.caller.model.model_id,
            "settings": {**self.caller.model.settings(), "action_max_tokens": ACTION_MAX_TOKENS,
                         "note_max_tokens": NOTE_MAX_TOKENS, "calls_per_turn": CALLS_PER_TURN,
                         "calls_per_agent": self.caller.calls_per_agent, "cap_nanos": self.caller.cap_nanos},
            "prompts": {agent.name: hashlib.sha256(agent.system.encode()).hexdigest() for agent in self.agents},
            "tools_hash": tools_hash(),
            "calls": self.caller.records,
            **self.totals(),
        }


def build_members(keys: dict[str, KeyPair], caller: ModelCaller) -> tuple[list[Member], AIRun]:
    """One AI agent per personality, named after it. `keys` go to the World's tool layer, never to an agent."""
    agents = [AIAgent(personality, personality, system_prompt(personality), caller) for personality in PERSONALITIES]
    return [Member(agent, keys[agent.name]) for agent in agents], AIRun(caller, agents)
