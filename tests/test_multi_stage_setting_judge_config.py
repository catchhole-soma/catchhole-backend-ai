import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.llm.openai_client import OpenAIResponsesClient
from evals.multi_stage_setting import cli, semantic_outcome
from evals.multi_stage_setting.semantic_outcome import (
    OpenAISemanticOutcomeJudge,
    SemanticOutcomeCase,
)


def _transport(requests: list[dict]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "results": [
                                            {
                                                "caseId": "case-1",
                                                "valueResolved": True,
                                                "coreMeaningCovered": True,
                                                "requiredFactsCovered": True,
                                                "forbiddenFactsAbsent": True,
                                                "contradiction": False,
                                                "unsupportedDetail": False,
                                                "reason": "same meaning",
                                                "sameSetting": None,
                                                "settingReason": None,
                                                "scopeEquivalent": None,
                                                "scopeReason": None,
                                            }
                                        ],
                                    }
                                ),
                            }
                        ]
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    return httpx.MockTransport(handle)


def test_default_judge_http_model_and_effort_do_not_inherit_product_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        semantic_outcome,
        "get_settings",
        lambda: SimpleNamespace(
            llm_api_key="test-key",
            llm_model="gpt-5.6-terra",
            llm_reasoning_effort="none",
            openai_responses_api_url="https://api.openai.test/v1/responses",
        ),
    )
    requests: list[dict] = []
    judge = OpenAISemanticOutcomeJudge()

    async def run() -> None:
        await judge.client.http_client.aclose()
        async with httpx.AsyncClient(transport=_transport(requests)) as transport:
            judge.client.http_client = transport
            await judge.judge_many([SemanticOutcomeCase("case-1", "expected", "actual")])

    asyncio.run(run())

    request = requests[0]
    assert request["model"] == "gpt-5.6-sol"
    assert request["reasoning"] == {"effort": "medium"}
    assert request["store"] is False
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["prompt_cache_key"] == "multi-stage-setting-eval:semantic-outcome:v4"
    assert request["max_output_tokens"] >= 5000


def test_judge_http_overrides_leave_injected_product_client_unchanged() -> None:
    requests: list[dict] = []

    async def run() -> None:
        async with httpx.AsyncClient(transport=_transport(requests)) as transport:
            product_client = OpenAIResponsesClient(
                api_key="test-key",
                model="gpt-5.6-luna",
                responses_api_url="https://api.openai.test/v1/responses",
                reasoning_effort="none",
                http_client=transport,
            )
            judge = OpenAISemanticOutcomeJudge(
                client=product_client,
                model="gpt-5.6-terra",
                reasoning_effort="high",
            )
            await judge.judge_many([SemanticOutcomeCase("case-1", "expected", "actual")])
            assert product_client.model == "gpt-5.6-luna"
            assert product_client.reasoning_effort == "none"
            assert judge.client.http_client is transport

    asyncio.run(run())

    assert requests[0]["model"] == "gpt-5.6-terra"
    assert requests[0]["reasoning"] == {"effort": "high"}
    assert requests[0]["store"] is False


@pytest.mark.parametrize(
    ("arguments", "model", "effort"),
    [
        ([], "gpt-5.6-sol", "medium"),
        (
            ["--judge-model", "gpt-5.6-terra", "--judge-reasoning-effort", "high"],
            "gpt-5.6-terra",
            "high",
        ),
    ],
)
def test_cli_passes_independent_judge_configuration(
    monkeypatch,
    tmp_path,
    arguments: list[str],
    model: str,
    effort: str,
) -> None:
    calls: list[dict] = []
    sentinel_judge = object()

    def make_judge(**kwargs):
        calls.append(kwargs)
        return sentinel_judge

    async def evaluate(gold, predictions, *, semantic_judge):
        assert semantic_judge is sentinel_judge
        return {"reportVersion": "setting-eval-report/v3"}

    monkeypatch.setattr(cli, "OpenAISemanticOutcomeJudge", make_judge)
    monkeypatch.setattr(cli, "evaluate_multi_stage", evaluate)
    monkeypatch.setattr(
        cli,
        "load_gold_snapshot_v3",
        lambda *args, **kwargs: SimpleNamespace(fixture_hash="fixture"),
    )
    monkeypatch.setattr(cli, "load_prediction_bundle_v3", lambda *args, **kwargs: object())
    output = tmp_path / "score.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "score",
            "--gold",
            "gold.json",
            "--predictions",
            "predictions.json",
            "--semantic-judge",
            "openai",
            "--output",
            str(output),
            "--quiet",
            *arguments,
        ],
    )

    cli.main()

    assert calls == [{"model": model, "reasoning_effort": effort}]
    assert json.loads(output.read_text()) == {"reportVersion": "setting-eval-report/v3"}


def test_workflow_routes_judge_inputs_only_to_scoring() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/setting-multi-stage-score.yml"
    ).read_text()
    assert "judge_model:" in workflow
    assert "default: gpt-5.6-sol" in workflow
    assert "judge_reasoning_effort:" in workflow
    assert "default: medium" in workflow
    assert "JUDGE_MODEL: ${{ inputs.judge_model }}" in workflow
    assert "JUDGE_REASONING_EFFORT: ${{ inputs.judge_reasoning_effort }}" in workflow
    runtime, scoring = workflow.split("- name: Evaluate predictions", 1)
    assert '--judge-model "$JUDGE_MODEL"' not in runtime
    assert '--judge-reasoning-effort "$JUDGE_REASONING_EFFORT"' not in runtime
    assert '--judge-model "$JUDGE_MODEL"' in scoring
    assert '--judge-reasoning-effort "$JUDGE_REASONING_EFFORT"' in scoring
