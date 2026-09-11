import asyncio
import json

import httpx
import pytest

from app.analysis.setting_extractor import CharacterSettingSchemaHint
from app.llm.exceptions import LlmIncompleteResponseError
from app.llm.openai_client import OpenAIResponsesClient
from evals.multi_stage_setting import report_cli, runtime_cli
from evals.multi_stage_setting.contracts import ScenarioPrediction
from evals.multi_stage_setting.processing import ProcessingTrace
from evals.multi_stage_setting.provider_diagnostics import (
    provider_failure_details,
    sanitize_provider_details,
)
from evals.multi_stage_setting.runtime_adapter import (
    RuntimeComponents,
    RuntimeUsageCounter,
    UsageRecordingTextGenerationClient,
    run_multi_stage_predictions,
)
from tests.test_multi_stage_setting_new_character_evaluation import _gold


async def _interrupted(status=429, payload=None):
    if payload is None:
        payload = {
            "error": {
                "code": "rate_limit_exceeded",
                "type": "tokens",
                "param": "max_output_tokens",
                "message": "SECRET manuscript and key",
            }
        }

    async def respond(request):
        return httpx.Response(
            status,
            json=payload,
            request=request,
            headers={"x-request-id": "req_test123", "set-cookie": "SECRET"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        usage = RuntimeUsageCounter()
        client = UsageRecordingTextGenerationClient(
            OpenAIResponsesClient(
                api_key="SECRET_KEY",
                model="gpt-5.6-terra",
                responses_api_url="https://example.test/v1/responses",
                http_client=http_client,
            ),
            usage,
        )

        class Extractor:
            async def extract_from_chunk(self, **kwargs):
                await client.create_text_response(
                    "SECRET_SYSTEM",
                    "SECRET_MANUSCRIPT",
                    model="gpt-5.6-sol",
                    prompt_cache_key="setting-extraction:v10",
                )

        expected = httpx.HTTPStatusError if status != 200 else LlmIncompleteResponseError
        with pytest.raises(expected) as caught:
            await run_multi_stage_predictions(
                _gold(),
                mode="FIXED",
                domains={"CHARACTER"},
                components=RuntimeComponents(
                    character_extractor=Extractor(),
                    character_subject_resolver=object(),
                    character_comparator=object(),
                    world_comparator=object(),
                    usage=usage,
                ),
                character_schema_hints=(
                    CharacterSettingSchemaHint(
                        schema_key="profile.height",
                        display_name="키",
                        aliases=(),
                        attribute_pattern=None,
                        value_type="NUMBER",
                    ),
                ),
            )
        return caught.value, usage


@pytest.mark.parametrize(
    "status,code,error_type",
    [
        (400, "invalid_json_schema", "invalid_request_error"),
        (401, "invalid_api_key", "authentication_error"),
        (429, "rate_limit_exceeded", "tokens"),
        (500, "server_error", "server_error"),
    ],
)
def test_http_failure_before_first_candidate_retains_actual_call_metadata(status, code, error_type):
    exc, _ = asyncio.run(
        _interrupted(
            status,
            {
                "error": {
                    "code": code,
                    "type": error_type,
                    "param": "text.format.schema",
                    "message": "SECRET",
                }
            },
        )
    )
    scenario = exc.prediction_bundle.scenarios[0]
    assert scenario.stage1 == [] and scenario.processing == []
    assert scenario.pipeline_status == "EXECUTION_ABORTED"
    failure = scenario.execution_failure
    assert failure.episode_no == 1
    assert failure.stage == "CHARACTER_STAGE1"
    assert failure.failure_code == "LLM_PROVIDER_ERROR"
    assert failure.provider.model == "gpt-5.6-sol"
    assert failure.provider.purpose == "CHARACTER_EXTRACTION"
    assert failure.provider.http_status == status
    assert failure.provider.provider_error_code == code
    assert failure.provider.provider_error_type == error_type
    assert failure.provider.parameter == "text.format.schema"
    assert failure.provider.request_id == "req_test123"
    assert "SECRET" not in scenario.model_dump_json()


@pytest.mark.parametrize(
    "response_status,reason",
    [
        ("incomplete", "content_filter"),
        ("failed", None),
    ],
)
def test_http_200_incomplete_response_keeps_reason_request_id_and_usage(response_status, reason):
    exc, usage = asyncio.run(
        _interrupted(
            200,
            {
                "status": response_status,
                "incomplete_details": {"reason": reason},
                "error": {"code": "server_error", "message": "SECRET"},
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        )
    )
    provider = exc.prediction_bundle.scenarios[0].execution_failure.provider
    assert provider.http_status == 200
    assert provider.response_status == response_status
    assert provider.incomplete_reason == reason
    assert provider.request_id == "req_test123"
    assert usage.input_tokens == 10 and usage.output_tokens == 3


def test_cli_and_public_artifacts_show_failure_even_without_candidates(
    tmp_path, monkeypatch, capsys
):
    exc, _ = asyncio.run(_interrupted())

    async def fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr(runtime_cli, "load_gold_snapshot_v3", lambda *a, **kw: _gold())
    monkeypatch.setattr(runtime_cli, "create_default_runtime_components", lambda **kw: None)
    monkeypatch.setattr(runtime_cli, "run_multi_stage_predictions", fail)
    predictions = tmp_path / "predictions.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "runtime_cli",
            "--gold",
            "unused.json",
            "--mode",
            "FIXED",
            "--domains",
            "CHARACTER",
            "--source-root",
            str(tmp_path),
            "--output",
            str(predictions),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        runtime_cli.main()
    console = str(caught.value)
    assert '"httpStatus": 429' in console
    assert "rate_limit_exceeded" in console and "req_test123" in console
    assert '"episodeNo": 1' in console and "CHARACTER_STAGE1" in console
    assert "SECRET" not in console

    summary = tmp_path / "summary.md"
    monkeypatch.setattr(
        "sys.argv",
        [
            "report_cli",
            "--predictions",
            str(predictions),
            "--markdown-output",
            str(summary),
        ],
    )
    report_cli.main()
    public = json.loads((tmp_path / "diagnostics.json").read_text())["scenarios"][0]
    assert public["episodeNo"] == 1 and public["processing"] == []
    assert public["executionFailure"]["provider"]["httpStatus"] == 429
    rendered = summary.read_text()
    for value in (
        "1화",
        "CHARACTER_STAGE1",
        "gpt-5.6-sol",
        "429",
        "rate_limit_exceeded",
        "req_test123",
    ):
        assert value in rendered
    assert "SECRET" not in rendered
    assert "SECRET" not in (tmp_path / "diagnostics.json").read_text()
    capsys.readouterr()


def test_public_failure_metadata_is_resanitized():
    report = {
        "dataset": {"scorable": False},
        "scenarios": [
            {
                "scenarioId": "S1",
                "episodeNo": 1,
                "executionFailure": {
                    "stage": "CHARACTER_STAGE1",
                    "failureCode": "LLM_PROVIDER_ERROR",
                    "episodeNo": 1,
                    "message": "SECRET",
                    "provider": {
                        "httpStatus": 400,
                        "model": "SECRET",
                        "providerErrorCode": "SECRET",
                        "providerErrorType": "SECRET",
                        "parameter": "SECRET",
                        "requestId": "sk-SECRET",
                        "responseStatus": "SECRET",
                        "incompleteReason": "SECRET",
                        "message": "SECRET",
                        "headers": {"x": "SECRET"},
                    },
                },
            }
        ],
    }
    public = report_cli.build_public_diagnostics(report)
    assert public[0]["executionFailure"]["provider"]["httpStatus"] == 400
    assert "SECRET" not in json.dumps(public)
    assert "SECRET" not in report_cli.render_markdown_summary(report)


def test_non_json_response_and_wrapped_exception_do_not_leak_body_or_url():
    response = httpx.Response(
        502,
        text="SECRET html",
        headers={"x-request-id": "req_test123"},
        request=httpx.Request("POST", "https://example.test/SECRET"),
    )
    exc = httpx.HTTPStatusError("SECRET", request=response.request, response=response)
    wrapped = RuntimeError("SECRET")
    wrapped.__cause__ = exc
    details = provider_failure_details(wrapped)
    assert details == {"http_status": 502, "request_id": "req_test123"}
    assert "SECRET" not in json.dumps(details)
    assert sanitize_provider_details({"httpStatus": True}) == {}


def test_network_failure_still_reports_phase_and_selected_model():
    class Client:
        model = "gpt-5.6-terra"

        async def create_text_response(self, **kwargs):
            raise httpx.ReadTimeout("SECRET")

    wrapped = UsageRecordingTextGenerationClient(Client(), RuntimeUsageCounter())
    with pytest.raises(httpx.ReadTimeout) as caught:
        asyncio.run(
            wrapped.create_text_response(
                "SECRET",
                "SECRET",
                prompt_cache_key="subject-resolution:v1",
            )
        )
    trace = ProcessingTrace(episode_no=2)
    trace.start([], "CHARACTER_STAGE1")
    failure = trace.aborted("S2", caught.value).execution_failure
    assert failure.failure_code == "LLM_NETWORK_ERROR"
    assert failure.episode_no == 2
    assert failure.provider.model == "gpt-5.6-terra"
    assert failure.provider.purpose == "CHARACTER_SUBJECT_RESOLUTION"
    assert failure.provider.http_status is None
    assert "SECRET" not in failure.model_dump_json()
    assert "executionFailure" not in ScenarioPrediction(scenario_id="legacy").model_dump(
        by_alias=True
    )
