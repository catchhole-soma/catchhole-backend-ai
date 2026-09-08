import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.llm.exceptions import LlmIncompleteResponseError, LlmOutputTruncatedError
from app.llm.responses import LlmTextResponse
from app.usage.metering import _estimate_text_token_upper_bound
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.semantic_outcome import (
    OpenAISemanticOutcomeJudge,
    SemanticOutcomeCase,
    WorldSettingNameContext,
)
from tests.test_multi_stage_setting_name_evaluation import _fixture


class _Client:
    def __init__(self, *, output_case_limit=None):
        self.requests = []
        self.output_case_limit = output_case_limit

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        cases = json.loads(kwargs["user_prompt"])["cases"]
        if self.output_case_limit is not None and len(cases) > self.output_case_limit:
            raise LlmOutputTruncatedError(
                "truncated",
                incomplete_reason="max_output_tokens",
                max_output_tokens=kwargs["max_output_tokens"],
                input_token_count=100,
                cached_input_token_count=20,
                output_token_count=kwargs["max_output_tokens"],
            )
        return LlmTextResponse(
            text=json.dumps({"results": [_decision(case) for case in reversed(cases)]}),
            input_token_count=10,
            cached_input_token_count=2,
            output_token_count=20,
        )


def _decision(case):
    contextual = "settingContext" in case
    return {
        "caseId": case["caseId"],
        "valueResolved": True,
        "coreMeaningCovered": True,
        "requiredFactsCovered": True,
        "forbiddenFactsAbsent": True,
        "contradiction": False,
        "unsupportedDetail": False,
        "reason": "same meaning",
        "sameSetting": True if contextual else None,
        "settingReason": "same item" if contextual else None,
        "scopeEquivalent": True if contextual else None,
        "scopeReason": "same scope" if contextual else None,
    }


def _cases(count, *, scenario_id="S1", expected="바바리안이다."):
    return [
        SemanticOutcomeCase(str(index), expected, "바바리안 종족이다.", scenario_id=scenario_id)
        for index in range(count)
    ]


def _request_cases(client):
    return [json.loads(request["user_prompt"])["cases"] for request in client.requests]


def test_default_budget_sends_more_than_eight_cases_without_changing_result_order():
    client = _Client()
    cases = _cases(50)

    result = asyncio.run(OpenAISemanticOutcomeJudge(client=client).judge_many(cases))

    assert len(client.requests) == 1
    assert len(_request_cases(client)[0]) == 50
    assert client.requests[0]["max_output_tokens"] == 32000
    assert [decision.case_id for decision in result.decisions] == [case.case_id for case in cases]
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (10, 2, 20)


def test_input_budget_splits_large_cases_with_prompt_and_schema_included():
    client = _Client()
    cases = _cases(3, expected="던전 규칙은 그대로 유지된다. " * 2000)
    judge = OpenAISemanticOutcomeJudge(client=client)

    result = asyncio.run(judge.judge_many(cases))

    assert [len(chunk) for chunk in _request_cases(client)] == [2, 1]
    assert [decision.case_id for decision in result.decisions] == ["0", "1", "2"]
    for request in client.requests:
        assert (
            _estimate_text_token_upper_bound(
                request["system_prompt"],
                request["user_prompt"],
                request["model"],
                0,
                request["response_schema"],
            )
            <= 64000
        )
        assert request["max_output_tokens"] == 32000
    assert [row["expectedValue"] for chunk in _request_cases(client) for row in chunk] == [
        case.expected_value for case in cases
    ]


def test_exact_input_boundary_counts_context_schema_and_literal_special_tokens():
    case = replace(
        _cases(1)[0],
        before_value="<|endoftext|>",
        source_values=("원래 규칙",),
        evidence_quotes=("<|im_start|> 규칙은 바뀌지 않았다.",),
        setting_context=WorldSettingNameContext(
            category="WORLD_RULE_HISTORY",
            subject_name="던전 앤 스톤",
            scope_name=None,
            expected_setting_name="사망 규칙",
            actual_setting_name="캐릭터 사망",
            related_paths=({"scope": "게임 규칙", "value": "사망 후 다시 키운다."},),
        ),
    )
    client = _Client()
    asyncio.run(OpenAISemanticOutcomeJudge(client=client).judge_many([case]))
    request = client.requests[0]
    exact_budget = _estimate_text_token_upper_bound(
        request["system_prompt"],
        request["user_prompt"],
        request["model"],
        0,
        request["response_schema"],
    )

    at_limit = _Client()
    asyncio.run(
        OpenAISemanticOutcomeJudge(client=at_limit, max_input_tokens=exact_budget).judge_many(
            [case]
        )
    )
    assert at_limit.requests[0]["user_prompt"] == request["user_prompt"]
    too_small = _Client()
    with pytest.raises(ValueError, match="exceeds max_input_tokens"):
        asyncio.run(
            OpenAISemanticOutcomeJudge(
                client=too_small, max_input_tokens=exact_budget - 1
            ).judge_many([case])
        )
    assert too_small.requests == []


def test_batches_never_mix_scenarios_even_when_all_cases_fit_the_token_budget():
    cases = [
        replace(case, scenario_id=scenario)
        for case, scenario in zip(
            _cases(5),
            ["episode:1", "episode:1", "episode:2", "episode:2", "episode:1"],
            strict=True,
        )
    ]
    client = _Client()

    result = asyncio.run(OpenAISemanticOutcomeJudge(client=client).judge_many(cases))

    assert [[row["caseId"] for row in chunk] for chunk in _request_cases(client)] == [
        ["0", "1"],
        ["2", "3"],
        ["4"],
    ]
    assert [decision.case_id for decision in result.decisions] == [case.case_id for case in cases]
    assert all("scenarioId" not in row for chunk in _request_cases(client) for row in chunk)


def test_live_evaluator_keeps_each_episode_separate_in_all_judge_phases(monkeypatch):
    gold, bundle = _fixture(prediction_value="사망하면 처음부터 캐릭터를 다시 키운다.")
    gold.scenarios.append(
        gold.scenarios[0].model_copy(
            update={
                "scenario_id": "S2",
                "episode_no": 2,
                "source_identifier": "02화.txt",
            }
        )
    )
    gold.stage1.append(
        gold.stage1[0].model_copy(
            update={
                "scenario_id": "S2",
                "episode_no": 2,
                "gold_id": "W2",
            }
        )
    )
    gold.stage2.append(
        gold.stage2[0].model_copy(
            update={
                "scenario_id": "S2",
                "episode_no": 2,
                "decision_id": "D2",
                "source_gold_ids": ["W2"],
            }
        )
    )
    gold.evaluation_scenario_ids = ["S1", "S2"]
    gold = gold.with_fixture_hash()
    second_prediction = bundle.scenarios[0].model_copy(deep=True)
    second_prediction.scenario_id = "S2"
    second_prediction.stage1[0].candidate_id = "P2"
    second_prediction.stage2[0].source_candidate_id = "P2"
    bundle.scenarios.append(second_prediction)
    bundle = bundle.model_copy(
        update={
            "fixture_hash": gold.fixture_hash,
            "evaluation_scenario_ids": ["S1", "S2"],
        }
    )
    before = bundle.model_dump(mode="json")
    client = _Client()
    judge = OpenAISemanticOutcomeJudge(client=client)
    scenario_by_case = {}
    judge_many = judge.judge_many

    async def capture_scenarios(cases):
        scenario_by_case.update({case.case_id: case.scenario_id for case in cases})
        return await judge_many(cases)

    monkeypatch.setattr(judge, "judge_many", capture_scenarios)

    report = asyncio.run(evaluate_multi_stage(gold, bundle, semantic_judge=judge))

    requested_scenarios = []
    phases = set()
    for chunk in _request_cases(client):
        scenarios = {scenario_by_case[row["caseId"]] for row in chunk}
        assert len(scenarios) == 1
        assert None not in scenarios
        requested_scenarios.extend(scenarios)
        phases.update(row["caseId"].partition(":")[0] for row in chunk)
    assert set(requested_scenarios) == {"S1", "S2"}
    assert "state" in phases
    assert report["endToEnd"]["metrics"]["afterStateF1"] == 1
    assert report["dataset"]["episodes"] == [1, 2]
    assert bundle.model_dump(mode="json") == before


def test_oversized_later_case_fails_before_any_paid_request_without_truncating_data():
    cases = _cases(2)
    cases[1] = replace(cases[1], expected_value="던전 규칙 " * 40000)
    client = _Client()

    with pytest.raises(ValueError, match="index 1 exceeds max_input_tokens"):
        asyncio.run(OpenAISemanticOutcomeJudge(client=client).judge_many(cases))

    assert client.requests == []


def test_output_truncation_splits_only_failed_batch_and_counts_failed_usage():
    client = _Client(output_case_limit=2)
    cases = _cases(5)

    result = asyncio.run(OpenAISemanticOutcomeJudge(client=client).judge_many(cases))

    assert [[row["caseId"] for row in chunk] for chunk in _request_cases(client)] == [
        ["0", "1", "2", "3", "4"],
        ["0", "1"],
        ["2", "3", "4"],
        ["2"],
        ["3", "4"],
    ]
    assert all(request["max_output_tokens"] == 32000 for request in client.requests)
    assert [decision.case_id for decision in result.decisions] == [case.case_id for case in cases]
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (
        230,
        46,
        64060,
    )


def test_singleton_output_truncation_stops_without_retry_loop():
    client = _Client(output_case_limit=0)

    with pytest.raises(LlmOutputTruncatedError):
        asyncio.run(OpenAISemanticOutcomeJudge(client=client).judge_many(_cases(1)))

    assert len(client.requests) == 1


@pytest.mark.parametrize(
    "error",
    [
        LlmIncompleteResponseError("incomplete", incomplete_reason="content_filter"),
        httpx.ConnectError("connection failed"),
    ],
)
def test_other_provider_failures_are_not_retried_as_output_truncation(error):
    client = _Client()
    client.create_text_response = AsyncMock(side_effect=error)

    with pytest.raises(type(error)):
        asyncio.run(OpenAISemanticOutcomeJudge(client=client).judge_many(_cases(20)))

    assert client.create_text_response.await_count == 1


@pytest.mark.parametrize("malformed", ["missing", "duplicate"])
def test_incomplete_case_coverage_is_still_rejected(malformed):
    cases = _cases(20)
    decisions = [_decision({"caseId": case.case_id}) for case in cases]
    if malformed == "missing":
        decisions.pop()
    else:
        decisions.append(decisions[0])
    client = _Client()
    client.create_text_response = AsyncMock(
        return_value=LlmTextResponse(text=json.dumps({"results": decisions}))
    )

    with pytest.raises(ValueError, match="caseId"):
        asyncio.run(OpenAISemanticOutcomeJudge(client=client).judge_many(cases))

    assert client.create_text_response.await_count == 1


def test_empty_cases_do_not_call_provider():
    client = _Client()
    result = asyncio.run(OpenAISemanticOutcomeJudge(client=client).judge_many([]))
    assert client.requests == []
    assert result.decisions == ()


@pytest.mark.parametrize(
    "limits",
    [
        {"max_input_tokens": 0},
        {"max_input_tokens": -1},
        {"max_output_tokens": 0},
        {"max_output_tokens": 128001},
    ],
)
def test_invalid_limits_are_rejected(limits):
    with pytest.raises(ValueError):
        OpenAISemanticOutcomeJudge(client=_Client(), **limits)
