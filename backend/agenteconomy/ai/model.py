"""The model behind an agent: Bedrock's Converse API in production, a fake in every automated test.

Messages use Converse's shape. Forced tool use means a reply is always tool calls, never free-form text to interpret.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Nanodollars per token, so spending stays in integers. US geo profile: $1.10 and $5.50 per million tokens.
# AWS's page doesn't state these in a form we could read; see docs/WRITEUP.md. Compare with the bill after real runs.
PRICES = {MODEL_ID: (1_100, 5_500)}

# Refused before the model ran, so nothing was billed. Any other failure might have been.
_NOT_BILLED = {"ThrottlingException", "ServiceUnavailableException", "AccessDeniedException", "ValidationException",
               "ModelNotReadyException", "ResourceNotFoundException", "ServiceQuotaExceededException"}
_NOT_RETRYABLE = {"AccessDeniedException", "ValidationException", "ResourceNotFoundException"}


class ModelError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True, may_have_billed: bool = True):
        super().__init__(message)
        self.retryable = retryable
        self.may_have_billed = may_have_billed


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    input_tokens: int
    output_tokens: int


class ModelClient(Protocol):
    model_id: str

    def settings(self) -> dict[str, Any]: ...

    def call(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             max_tokens: int) -> ModelReply: ...


def _tool_config(tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tools": [{"toolSpec": {"name": tool["name"], "description": tool["description"],
                                    "inputSchema": {"json": tool["input_schema"]}}} for tool in tools],
            "toolChoice": {"any": {}}}


class BedrockModel:
    """Follows ../onepager/backend/onepager/bedrock.py. boto3's own retries are off: a timed-out call may have been
    billed, so every attempt has to go through the cost meter."""

    def __init__(self, model_id: str = MODEL_ID, region: str = "us-east-1", client=None):
        self.model_id = model_id
        self._region = region
        self._client = client

    def settings(self) -> dict[str, Any]:
        return {"provider": "bedrock-converse", "region": self._region, "temperature": "model default",
                "tool_choice": "any"}

    def _get_client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            config = Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard", "max_attempts": 1})
            self._client = boto3.client("bedrock-runtime", region_name=self._region, config=config)
        return self._client

    def call(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             max_tokens: int) -> ModelReply:
        try:
            response = self._get_client().converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=messages,
                inferenceConfig={"maxTokens": max_tokens},
                toolConfig=_tool_config(tools),
            )
        except Exception as e:  # botocore raises many types; callers only need to know how it failed
            code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
            raise ModelError(f"Bedrock call failed: {code}", retryable=code not in _NOT_RETRYABLE,
                             may_have_billed=code not in _NOT_BILLED) from e
        return _parse_reply(response)


def _parse_reply(response: dict[str, Any]) -> ModelReply:
    usage = response.get("usage", {})
    calls = []
    for block in response.get("output", {}).get("message", {}).get("content", []):
        tool_use = block.get("toolUse")
        if tool_use:
            calls.append(ToolCall(tool_use.get("toolUseId", ""), tool_use.get("name", ""), tool_use.get("input") or {}))
    return ModelReply(tuple(calls), response.get("stopReason", "unknown"), usage.get("inputTokens", 0),
                      usage.get("outputTokens", 0))


class FakeModel:
    """Costs nothing. `behavior` gets the request and returns the tool calls, a whole reply, or raises ModelError."""

    def __init__(self, behavior: Callable[..., Any], *, model_id: str = MODEL_ID):
        self.model_id = model_id
        self._behavior = behavior
        self.requests: list[dict[str, Any]] = []

    def settings(self) -> dict[str, Any]:
        return {"provider": "fake"}

    def call(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             max_tokens: int) -> ModelReply:
        request = {"system": system, "messages": messages, "tools": tools, "max_tokens": max_tokens}
        self.requests.append(request)
        result = self._behavior(**request)
        if isinstance(result, ModelReply):
            return result
        characters = len(system) + sum(len(str(message)) for message in messages)
        return ModelReply(tuple(result), "tool_use", characters // 4, 40)
