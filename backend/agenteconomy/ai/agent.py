"""An AI agent, behind the same interface as a scripted one: it reads a View and returns one Action.

It holds no key, node or tool layer, so all the model can ever do is ask for an Action. Nothing stops a day: if the
model fails or a limit is reached, the agent just passes.
"""

import json
import random
from typing import Any

from ..economy.agents import ScriptedAgent
from ..economy.tools import Action, ActionResult, DayFacts, View
from .budget import CapReached, ModelCaller, ModelUnavailable
from .memory import AgentMemory
from .model import ModelReply
from .summary import digest
from .tool_schemas import (
    ACTION_TOOLS,
    NOTE,
    NOTE_TOOL,
    PASS,
    READ_TOOLS,
    ToolInputError,
    answer_read,
    is_action,
    parse_action,
)

CALLS_PER_TURN = 2
ACTION_MAX_TOKENS = 300
NOTE_MAX_TOKENS = 200  # a real note hit 120, came back empty, and would have wiped the one before it


class AIAgent(ScriptedAgent):
    def __init__(self, name: str, personality: str, system: str, caller: ModelCaller):
        super().__init__(name)
        self.personality = personality
        self.system = system
        self.memory = AgentMemory()
        self._caller = caller
        self._today: list[str] = []
        self._decisions: list[dict[str, Any]] = []
        self._turn = 0

    def begin_day(self, day: int) -> None:
        self._today = []
        self._turn = 0

    def decide(self, view: View, rng: random.Random) -> Action | None:
        self._turn += 1
        turn_digest = digest(view, today=self._today, memory=self.memory.for_model())
        messages = [{"role": "user", "content": [{"text": json.dumps(turn_digest, sort_keys=True)}]}]
        for call_number in range(1, CALLS_PER_TURN + 1):
            # The last call must act, so a turn can't end on an unused read.
            actions = [tool for tool in ACTION_TOOLS if tool["name"] != "mine" or view.mines_left != 0]
            tools = actions if call_number == CALLS_PER_TURN else READ_TOOLS + actions
            try:
                reply = self._caller.call(self.name, system=self.system, messages=messages, tools=tools,
                                          max_tokens=ACTION_MAX_TOKENS, purpose="turn")
            except CapReached as e:
                return self._pass(f"limit: {e}")
            except ModelUnavailable as e:
                return self._pass(f"model unavailable: {e}")
            if not reply.tool_calls:
                return self._pass(f"no tool call: {reply.stop_reason}")

            chosen, results = self._handle(reply, view)
            if chosen is not None:
                action, rationale, tool = chosen
                self._decisions.append({"turn": self._turn, "tool": tool, "rationale": rationale, "calls": call_number})
                return action
            messages = messages + [_assistant_message(reply), {"role": "user", "content": results}]
        return self._pass("out of calls this turn")

    def _handle(self, reply: ModelReply, view: View) -> tuple[tuple[Action | None, str, str] | None, list[dict]]:
        """The first valid action in a reply is the decision. Otherwise, answer every call so the model can go on."""
        results = []
        for call in reply.tool_calls:
            try:
                if is_action(call.name):
                    action, rationale = parse_action(call.name, call.input, view)
                    return (action, rationale, call.name), []
                results.append(_result(call.id, answer_read(call.name, call.input, view), "success"))
            except ToolInputError as e:
                results.append(_result(call.id, {"error": str(e)}, "error"))
        return None, results

    def _pass(self, reason: str) -> None:
        self._decisions.append({"turn": self._turn, "tool": PASS, "rationale": None, "reason": reason})
        return None

    def drain_decisions(self) -> list[dict[str, Any]]:
        decisions, self._decisions = self._decisions, []
        return decisions

    def after_action(self, action: Action, result: ActionResult) -> None:
        if result.ok:
            found = " and found a block" if result.block else ""
            self._today.append(f"{type(action).__name__}: ok{found}")
        else:
            self._today.append(f"{type(action).__name__}: failed ({result.code})")

    def end_day(self, facts: DayFacts) -> None:
        """Facts first, so they're kept even if the note can't be written."""
        self.memory.remember_day(facts)
        prompt = {"today": self.memory.days[-1], "your_previous_note": self.memory.note,
                  "instruction": "Write a note of at most 25 words to help you tomorrow. Use the write_note tool."}
        messages = [{"role": "user", "content": [{"text": json.dumps(prompt, sort_keys=True)}]}]
        try:
            reply = self._caller.call(self.name, system=self.system, messages=messages, tools=[NOTE_TOOL],
                                      max_tokens=NOTE_MAX_TOKENS, purpose="note")
        except (CapReached, ModelUnavailable):
            return
        for call in reply.tool_calls:
            if call.name == NOTE and isinstance(call.input, dict):
                self.memory.set_note(call.input.get("note"))
                return

    def memory_dict(self) -> dict[str, Any]:
        return self.memory.to_dict()

    def load_memory(self, data: dict[str, Any]) -> None:
        self.memory = AgentMemory.from_dict(data)


def _assistant_message(reply: ModelReply) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"toolUse": {"toolUseId": call.id, "name": call.name, "input": call.input}}
                                             for call in reply.tool_calls]}


def _result(tool_use_id: str, content: Any, status: str) -> dict[str, Any]:
    return {"toolResult": {"toolUseId": tool_use_id, "content": [{"json": _as_json_object(content)}], "status": status}}


def _as_json_object(content: Any) -> dict[str, Any]:
    return content if isinstance(content, dict) else {"result": content}
