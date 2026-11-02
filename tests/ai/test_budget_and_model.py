import json

import boto3
import pytest
from ai_helpers import call

from agenteconomy.ai.budget import (
    DAILY_CAP_NANOS,
    NANOS_PER_DOLLAR,
    CapReached,
    FileLedger,
    ModelCaller,
    ModelUnavailable,
    SpendLedger,
    cost_nanos,
    worst_case_nanos,
)
from agenteconomy.ai.model import (
    MODEL_ID,
    BedrockModel,
    FakeModel,
    ModelError,
    ModelReply,
    ToolCall,
    _parse_reply,
    _tool_config,
)

TOOL = {"name": "mine", "description": "d", "input_schema": {"type": "object"}}


def reply(input_tokens=1000, output_tokens=100):
    return ModelReply((call("mine"),), "tool_use", input_tokens, output_tokens)


def caller_with(behavior, **options):
    return ModelCaller(FakeModel(behavior), options.pop("ledger", SpendLedger()), **options)


def ask(caller, agent="a"):
    return caller.call(agent, system="s", messages=[{"role": "user", "content": [{"text": "hi"}]}], tools=[TOOL],
                       max_tokens=100, purpose="turn")


def test_daily_cap_is_fifty_cents():
    assert DAILY_CAP_NANOS == NANOS_PER_DOLLAR // 2


def test_prices_compute_exactly():
    # $1.10 and $5.50 per million tokens.
    assert cost_nanos(MODEL_ID, 1_000_000, 0) == 1_100_000_000
    assert cost_nanos(MODEL_ID, 0, 1_000_000) == 5_500_000_000
    assert cost_nanos(MODEL_ID, 1, 1) == 6_600


def test_an_unpriced_model_is_an_error_not_free():
    with pytest.raises(KeyError):
        cost_nanos("some-other-model", 1, 1)


def test_worst_case_covers_all_output_and_grows_with_the_request():
    small = worst_case_nanos(MODEL_ID, 1000, 300)
    assert small >= cost_nanos(MODEL_ID, 0, 300)
    assert worst_case_nanos(MODEL_ID, 9000, 300) > small
    assert worst_case_nanos(MODEL_ID, 1000, 600) > small


def test_actual_usage_is_charged():
    caller = caller_with(lambda **_: reply(1000, 100))
    ask(caller)
    assert caller.spent_today == cost_nanos(MODEL_ID, 1000, 100)


def test_refuses_before_a_call_that_could_pass_the_cap():
    calls = []

    def behavior(**_):
        calls.append(1)
        return reply()

    caller = caller_with(behavior, cap_nanos=worst_case_nanos(MODEL_ID, 10, 100) - 1)
    with pytest.raises(CapReached):
        ask(caller)
    assert calls == []
    assert caller.refused == 1
    assert caller.spent_today == 0


@pytest.mark.parametrize("input_tokens, output_tokens", [(10, 1), (300, 50), (500, 100), (570, 100)])
def test_spending_never_passes_the_cap(input_tokens, output_tokens):
    # Usage up to the most this small request could really use: its estimate is about 576 tokens in, 100 out.
    caller = caller_with(lambda **_: reply(input_tokens, output_tokens), cap_nanos=NANOS_PER_DOLLAR // 100,
                         calls_per_agent=10_000)
    with pytest.raises(CapReached):
        while True:
            ask(caller)
    assert 0 < caller.spent_today <= caller.cap_nanos


def test_if_real_usage_ever_beat_the_estimate_calls_stop_at_once():
    # The cap holds exactly only while the pessimistic estimate does, so real runs compare the two. If it were wrong,
    # one call could overshoot, and nothing further may be spent.
    calls = []

    def behavior(**_):
        calls.append(1)
        return reply(100_000, 100)

    caller = caller_with(behavior, cap_nanos=NANOS_PER_DOLLAR // 100)
    ask(caller)
    assert caller.spent_today > caller.cap_nanos
    with pytest.raises(CapReached):
        ask(caller)
    assert len(calls) == 1


def test_agent_call_limit_is_per_agent_and_per_day():
    caller = caller_with(lambda **_: reply(), calls_per_agent=2)
    ask(caller, "a")
    ask(caller, "a")
    with pytest.raises(CapReached):
        ask(caller, "a")
    ask(caller, "b")
    caller.begin_day(1)
    ask(caller, "a")


def test_a_failed_call_retries_once_and_counts_against_the_agent_once():
    attempts = []

    def behavior(**_):
        attempts.append(1)
        if len(attempts) == 1:
            raise ModelError("throttled", may_have_billed=False)
        return reply()

    caller = caller_with(behavior)
    assert ask(caller).tool_calls
    assert len(attempts) == 2
    assert caller.errors == 1
    assert caller.calls["a"] == 1


def test_a_call_that_may_have_billed_is_charged_at_worst_case():
    def behavior(**_):
        raise ModelError("timed out", retryable=False, may_have_billed=True)

    caller = caller_with(behavior)
    with pytest.raises(ModelUnavailable):
        ask(caller)
    assert caller.spent_today > 0
    assert caller.records[-1]["cost_nanos"] == caller.spent_today


def test_a_refused_call_costs_nothing():
    def behavior(**_):
        raise ModelError("throttled", retryable=False, may_have_billed=False)

    caller = caller_with(behavior)
    with pytest.raises(ModelUnavailable):
        ask(caller)
    assert caller.spent_today == 0


def test_a_permanent_failure_is_not_retried():
    attempts = []

    def behavior(**_):
        attempts.append(1)
        raise ModelError("denied", retryable=False, may_have_billed=False)

    with pytest.raises(ModelUnavailable):
        ask(caller_with(behavior))
    assert len(attempts) == 1


def test_retries_stop_at_the_attempt_limit():
    attempts = []

    def behavior(**_):
        attempts.append(1)
        raise ModelError("down", may_have_billed=False)

    with pytest.raises(ModelUnavailable):
        ask(caller_with(behavior))
    assert len(attempts) == 2


def test_each_retry_is_rechecked_against_the_cap():
    # The first attempt's worst-case charge leaves too little for a second.
    attempts = []

    def behavior(**_):
        attempts.append(1)
        raise ModelError("timed out")

    worst = worst_case_nanos(MODEL_ID, 2 + 50 + 100, 100)
    caller = caller_with(behavior, cap_nanos=worst * 3 // 2)
    with pytest.raises((CapReached, ModelUnavailable)):
        ask(caller)
    assert len(attempts) == 1
    assert caller.spent_today <= caller.cap_nanos


def test_a_retried_day_shares_the_cap(tmp_path):
    ledger = FileLedger(tmp_path / "spend.json")
    caller = caller_with(lambda **_: reply(1000, 100), ledger=ledger)
    ask(caller)
    spent = caller.spent_today

    # A crash and a new attempt at the same day: the spending is still there.
    again = ModelCaller(FakeModel(lambda **_: reply(1000, 100)), FileLedger(tmp_path / "spend.json"))
    assert again.spent_today == spent
    ask(again)
    assert again.spent_today == 2 * spent


def test_starting_a_day_does_not_forget_its_spending():
    caller = caller_with(lambda **_: reply())
    ask(caller)
    spent = caller.spent_today
    caller.begin_day(0)
    assert caller.spent_today == spent
    assert caller.calls == {} and caller.records == []


def test_each_day_has_its_own_cap():
    caller = caller_with(lambda **_: reply())
    ask(caller)
    caller.begin_day(1)
    assert caller.spent_today == 0


def test_file_ledger_writes_every_charge_before_the_next_call(tmp_path):
    path = tmp_path / "spend.json"
    FileLedger(path).add(3, 500)
    assert json.loads(path.read_text(encoding="utf-8")) == {"3": 500}
    assert FileLedger(path).spent(3) == 500
    assert not path.with_suffix(".tmp").exists()


def test_calls_are_recorded_with_inputs_and_usage():
    caller = caller_with(lambda **_: reply(10, 5))
    ask(caller, "saver")
    [record] = caller.records
    assert record["agent"] == "saver" and record["purpose"] == "turn" and record["attempt"] == 1
    assert record["input_tokens"] == 10 and record["output_tokens"] == 5
    assert record["messages"][0]["content"][0]["text"] == "hi"
    assert record["tool_calls"] == [{"name": "mine", "input": {}}]
    assert record["cost_nanos"] == cost_nanos(MODEL_ID, 10, 5)
    json.dumps(record)


def test_a_refusal_at_the_cap_is_recorded():
    caller = caller_with(lambda **_: reply(), cap_nanos=1)
    with pytest.raises(CapReached):
        ask(caller)
    assert caller.records[-1]["outcome"] == "cost_cap"


# BedrockModel, with a stand-in for the AWS client. Tests can't build a real one.


class StubClient:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.requests = []

    def converse(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def aws_error(code):
    error = RuntimeError(code)
    error.response = {"Error": {"Code": code}}
    return error


CONVERSE_REPLY = {
    "output": {"message": {"content": [{"toolUse": {"toolUseId": "t1", "name": "mine", "input": {"rationale": "x"}}}]}},
    "stopReason": "tool_use", "usage": {"inputTokens": 12, "outputTokens": 3},
}


def test_bedrock_request_forces_a_tool_and_uses_the_right_model():
    client = StubClient(CONVERSE_REPLY)
    model = BedrockModel(client=client)
    result = model.call(system="s", messages=[{"role": "user", "content": []}], tools=[TOOL], max_tokens=50)
    [request] = client.requests
    assert request["modelId"] == MODEL_ID == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert request["toolConfig"]["toolChoice"] == {"any": {}}
    assert request["inferenceConfig"] == {"maxTokens": 50}
    assert request["system"] == [{"text": "s"}]
    assert result == ModelReply((ToolCall("t1", "mine", {"rationale": "x"}),), "tool_use", 12, 3)


def test_tool_config_shape():
    spec = _tool_config([TOOL])["tools"][0]["toolSpec"]
    assert spec == {"name": "mine", "description": "d", "inputSchema": {"json": {"type": "object"}}}


@pytest.mark.parametrize("code, retryable, billed", [
    ("ThrottlingException", True, False),
    ("ServiceUnavailableException", True, False),
    ("AccessDeniedException", False, False),
    ("ValidationException", False, False),
    ("ModelTimeoutException", True, True),
    ("ReadTimeoutError", True, True),
])
def test_bedrock_failures_are_classified(code, retryable, billed):
    model = BedrockModel(client=StubClient(error=aws_error(code)))
    with pytest.raises(ModelError) as e:
        model.call(system="s", messages=[], tools=[TOOL], max_tokens=50)
    assert e.value.retryable is retryable
    assert e.value.may_have_billed is billed


def test_reply_parsing_survives_odd_responses():
    assert _parse_reply({}) == ModelReply((), "unknown", 0, 0)
    text_only = {"output": {"message": {"content": [{"text": "no"}]}}, "stopReason": "end_turn"}
    assert _parse_reply(text_only).tool_calls == ()


def test_bedrock_settings_are_recorded():
    settings = BedrockModel(client=StubClient()).settings()
    assert settings["temperature"] == "model default"
    assert settings["tool_choice"] == "any"


def test_tests_cannot_create_a_real_client():
    with pytest.raises(AssertionError, match="spend money"):
        boto3.client("bedrock-runtime")
    with pytest.raises(ModelError) as e:
        BedrockModel().call(system="s", messages=[], tools=[TOOL], max_tokens=5)
    assert isinstance(e.value.__cause__, AssertionError)
