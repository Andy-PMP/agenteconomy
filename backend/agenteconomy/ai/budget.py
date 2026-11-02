"""The hard daily cost cap, plus per-agent call limits. Code enforces these; nothing depends on the model's cooperation.

Before each attempt the worst case is reserved, and the attempt is refused if that could pass the cap. Spending lives in
a ledger outside the attempt, so a retried day shares the same cap.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import PRICES, ModelClient, ModelError, ModelReply

NANOS_PER_DOLLAR = 10**9
DAILY_CAP_NANOS = NANOS_PER_DOLLAR // 2  # $0.50
MODEL_ATTEMPTS = 2

# Deliberately pessimistic: IDs tokenize badly, and tool definitions carry some hidden overhead.
_CHARS_PER_TOKEN = 2
_OVERHEAD_TOKENS = 500


class CapReached(Exception):
    """No more model calls for this agent, or for anyone, today."""


class ModelUnavailable(Exception):
    """The model failed even after retrying."""


class SpendLedger:
    """In memory: for tests. P6 puts this in DynamoDB."""

    def __init__(self):
        self._spent: dict[str, int] = {}

    def spent(self, day: int) -> int:
        return self._spent.get(str(day), 0)

    def add(self, day: int, nanos: int) -> None:
        self._spent[str(day)] = self.spent(day) + nanos


class FileLedger(SpendLedger):
    """Survives a crash: every charge is on disk before the next call starts."""

    def __init__(self, path: Path):
        super().__init__()
        self._path = path
        if path.exists():
            self._spent = json.loads(path.read_text(encoding="utf-8"))

    def add(self, day: int, nanos: int) -> None:
        super().add(day, nanos)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._spent, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self._path)


def cost_nanos(model_id: str, input_tokens: int, output_tokens: int) -> int:
    input_price, output_price = PRICES[model_id]
    return input_tokens * input_price + output_tokens * output_price


def worst_case_nanos(model_id: str, request_characters: int, max_tokens: int) -> int:
    return cost_nanos(model_id, request_characters // _CHARS_PER_TOKEN + _OVERHEAD_TOKENS, max_tokens)


@dataclass
class ModelCaller:
    """Every model call in a day goes through here, and gets recorded, so what happened can always be reconstructed."""

    model: ModelClient
    ledger: SpendLedger
    day: int = 0
    cap_nanos: int = DAILY_CAP_NANOS
    calls_per_agent: int = 16
    calls: dict[str, int] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    errors: int = 0
    refused: int = 0

    def begin_day(self, day: int) -> None:
        self.day = day
        self.calls = {}
        self.records = []
        self.errors = 0
        self.refused = 0

    @property
    def spent_today(self) -> int:
        return self.ledger.spent(self.day)

    def call(self, agent: str, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             max_tokens: int, purpose: str) -> ModelReply:
        if self.calls.get(agent, 0) >= self.calls_per_agent:
            self.refused += 1
            raise CapReached("agent call limit")
        self.calls[agent] = self.calls.get(agent, 0) + 1

        characters = len(system) + len(json.dumps(messages)) + len(json.dumps(tools))
        worst_case = worst_case_nanos(self.model.model_id, characters, max_tokens)
        last_error = None
        for attempt in range(1, MODEL_ATTEMPTS + 1):
            if self.spent_today + worst_case > self.cap_nanos:
                self.refused += 1
                self._record(agent, purpose, messages, attempt, "cost_cap", 0)
                raise CapReached("daily cost cap")
            try:
                reply = self.model.call(system=system, messages=messages, tools=tools, max_tokens=max_tokens)
            except ModelError as e:
                self.errors += 1
                charged = worst_case if e.may_have_billed else 0
                self.ledger.add(self.day, charged)
                self._record(agent, purpose, messages, attempt, f"error: {e}", charged)
                last_error = e
                if not e.retryable:
                    break
                continue
            charged = cost_nanos(self.model.model_id, reply.input_tokens, reply.output_tokens)
            self.ledger.add(self.day, charged)
            self._record(agent, purpose, messages, attempt, reply.stop_reason, charged, reply)
            return reply
        raise ModelUnavailable(str(last_error))

    def _record(self, agent: str, purpose: str, messages: list[dict[str, Any]], attempt: int, outcome: str,
                charged: int, reply: ModelReply | None = None) -> None:
        self.records.append({
            "agent": agent, "purpose": purpose, "attempt": attempt, "outcome": outcome, "cost_nanos": charged,
            "messages": messages,
            "input_tokens": reply.input_tokens if reply else 0,
            "output_tokens": reply.output_tokens if reply else 0,
            "tool_calls": [{"name": call.name, "input": call.input} for call in reply.tool_calls] if reply else [],
        })
