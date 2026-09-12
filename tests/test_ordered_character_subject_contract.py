"""Exercise the ordered subject contract through real metering and HTTP serialization."""

import asyncio
import json
import socket
import traceback
from uuid import uuid4

import httpx
import pytest

from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.exceptions import ComparisonValidationError, LlmExtractionError
from app.analysis.ordered_character_subjects import OrderedSubjectTarget, resolve_ordered_character_subjects
from app.analysis.schemas import ExtractedSettingCandidate
from app.llm.openai_client import OpenAIResponsesClient
from app.usage import metering
from app.usage.metering import MeteredTextGenerationClient


class OfflineEncoding:
    def encode(self, text, **kwargs):
        return list(text.encode("utf-8"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("These contract tests must not use network access.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    # Tokenizer model files must not trigger a download on an uncached test host.
    monkeypatch.setattr("app.analysis.ordered_context.tiktoken.get_encoding",
                        lambda unused: OfflineEncoding())
    monkeypatch.setattr(metering, "_encoding_for_model", lambda unused: OfflineEncoding())


def unknown_setting():
    return ExtractedSettingCandidate(
        source_chunk_id=uuid4(),
        candidate_kind="SETTING", entity_name="미상", raw_entity_mention="그는",
        attribute_name="level", attribute_value="12", value_type="NUMBER", value_json={"value": 12},
        evidence_spans=[{"quote": "그는 레벨 12에 도달했다."}],
    )


def result(target="K1"):
    return {"resolutions": [{"candidate_ref": "C1", "target_ref": target}]}


class Ledger:
    def __init__(self):
        self.reservations = []
        self.settlements = []

    async def reserve_ai_tokens(self, **kwargs):
        self.reservations.append(kwargs)

    async def settle_ai_tokens(self, **kwargs):
        self.settlements.append(kwargs)

    async def release_ai_tokens(self, *args, **kwargs):
        raise AssertionError("Synthetic completed responses always include usage.")


class HttpContract:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.ledger = Ledger()
        self.job_id, self.lease_token = uuid4(), uuid4()

    def resolve(self):
        return asyncio.run(self._resolve())

    async def _resolve(self):
        def respond(request):
            self.requests.append(json.loads(request.content))
            response = self.responses.pop(0)
            return httpx.Response(200, json={
                "status": "completed",
                "output_text": response if isinstance(response, str) else json.dumps(response),
                "usage": {"input_tokens": 100, "output_tokens": 20},
            })

        provider = OpenAIResponsesClient(
            api_key="synthetic-offline-key", model="gpt-5.6-luna", reasoning_effort="none",
            responses_api_url="https://offline.invalid/v1/responses",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        client = MeteredTextGenerationClient(
            delegate=provider, ledger=self.ledger, analysis_job_id=self.job_id,
            purpose="SUBJECT_RESOLUTION", default_model="gpt-5.6-luna",
            lease_token=self.lease_token, max_retries=0,
        )
        try:
            return await resolve_ordered_character_subjects(
                client=client, model="gpt-5.6-luna", max_attempts=3, max_output_tokens=2000,
                context=SubjectResolutionChunkContext("합성 인물이 등장했다.", "그는 레벨 12에 도달했다.", None),
                candidates=[unknown_setting()],
                subjects=[OrderedSubjectTarget(name="합성 인물", actual_character_id=uuid4())], episode_no=1,
            )
        finally:
            await provider.aclose()


def prompt(request, role):
    return next(item["content"][0]["text"] for item in request["input"] if item["role"] == role)


def feedback(request):
    return prompt(request, "user").split("response_format_feedback:\n", 1)[1]


def test_ordered_subject_schema_reaches_http_through_bound_and_metering():
    contract = HttpContract(result())
    resolved, discovered = contract.resolve()
    assert discovered == [] and resolved[0].binding.actual_character_id is not None
    assert resolved[0].binding.provisional_subject_key is None
    request = contract.requests[0]
    form = request["text"]["format"]
    assert form["type"] == "json_schema" and form["strict"] is True
    assert form["name"] == "ordered_character_subject_resolution"
    schema = form["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["resolutions"]
    row = schema["$defs"]["_Resolution"]
    assert row["additionalProperties"] is False
    assert set(row["required"]) == set(row["properties"]) == {"candidate_ref", "target_ref"}
    assert {item["type"] for item in row["properties"]["target_ref"]["anyOf"]} == {
        "string", "null",
    }
    assert "default" not in row["properties"]["target_ref"]
    assert '"resolutions"' in prompt(request, "system")
    assert '"candidate_ref"' in prompt(request, "system")
    assert '"target_ref":null' in prompt(request, "system")
    assert request["prompt_cache_key"] == "ordered-character-subject-resolution:v3-adjacent-only"
    assert request["model"] == "gpt-5.6-luna"
    assert request["reasoning"] == {"effort": "none"}
    assert request["store"] is False and request["max_output_tokens"] == 2000
    serialized = json.dumps(request)
    assert str(contract.job_id) not in serialized
    assert str(contract.lease_token) not in serialized
    assert str(resolved[0].binding.actual_character_id) not in serialized
    assert len(contract.ledger.reservations) == len(contract.ledger.settlements) == 1
    assert contract.ledger.reservations[0]["purpose"] == "SUBJECT_RESOLUTION"
    assert contract.ledger.settlements[0]["outcome"] == "SUCCESS"


@pytest.mark.parametrize(("malformed", "field"), [
    ({"candidate_ref": "C1", "target_ref": "D1"}, "resolutions"),
    ({"resolutions": [{"ref": "C1", "target_ref": "D1"}]}, "resolutions[].candidate_ref"),
    ({"resolutions": [{"candidate_ref": "C1"}]}, "resolutions[].target_ref"),
])
def test_missing_field_receives_safe_feedback_then_valid_response_recovers(malformed, field):
    contract = HttpContract(malformed, result())
    resolved, discovered = contract.resolve()
    assert len(resolved) == 1 and discovered == []
    assert len(contract.requests) == 2
    first, second = contract.requests
    assert prompt(second, "user").startswith(prompt(first, "user"))
    assert f'"field": "{field}"' in feedback(second)
    assert '"type": "missing"' in feedback(second)
    assert first["text"] == second["text"]
    assert prompt(first, "system") == prompt(second, "system")
    assert second["prompt_cache_key"] == first["prompt_cache_key"]
    assert len(contract.ledger.reservations) == len(contract.ledger.settlements) == 2


def test_feedback_does_not_echo_provider_values_unknown_keys_or_uuids(caplog):
    secret = f"MODEL_PRIVATE_VALUE_{uuid4()}"
    malformed = {"resolutions": [{
        "candidate_ref": {"nested": secret}, "target_ref": None, secret: secret,
    }], secret: secret}
    contract = HttpContract(malformed, result())
    contract.resolve()
    safe = feedback(contract.requests[1])
    assert secret not in safe and secret not in caplog.text
    assert "nested" not in safe
    assert '"field": "resolutions[].candidate_ref"' in safe
    assert '"type": "string_type"' in safe
    assert '"field": "resolutions[]"' in safe
    assert '"field": "response"' in safe
    assert '"type": "extra_forbidden"' in safe


def test_persistently_invalid_shape_still_fails_without_replaying_bad_values(caplog):
    secret = f"MODEL_PRIVATE_VALUE_{uuid4()}"
    malformed = {secret: secret}
    contract = HttpContract(malformed, malformed, malformed)
    with pytest.raises(LlmExtractionError) as caught:
        contract.resolve()
    assert "failed after 3 attempts" in str(caught.value)
    assert len(contract.requests) == 3
    assert secret not in caplog.text
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert secret not in prompt(contract.requests[1], "user")
    assert contract.requests[1] == contract.requests[2]
    assert prompt(contract.requests[2], "user").count("response_format_feedback:") == 1


def test_invalid_json_feedback_uses_a_fixed_kind_without_response_text():
    contract = HttpContract("PRIVATE_INVALID_JSON", result())
    contract.resolve()
    safe = feedback(contract.requests[1])
    assert '"type": "json_invalid"' in safe
    assert "PRIVATE_INVALID_JSON" not in safe


def test_explicit_null_preserves_ambiguous_identity():
    contract = HttpContract(result(None))
    resolved, discovered = contract.resolve()
    assert discovered == []
    assert resolved[0].binding.actual_character_id is None
    assert resolved[0].binding.provisional_subject_key is None
    assert resolved[0].binding.match_status == "AMBIGUOUS"
    assert len(contract.requests) == 1


@pytest.mark.parametrize("invalid", [
    {"resolutions": []},
    {"resolutions": [{"candidate_ref": "C2", "target_ref": None}]},
    {"resolutions": [result()["resolutions"][0], result()["resolutions"][0]]},
    result("K999"),
])
def test_coverage_and_unknown_identity_still_fail_closed(invalid):
    contract = HttpContract(invalid, invalid, invalid)
    with pytest.raises(ComparisonValidationError, match="failed after 3 attempts"):
        contract.resolve()
    assert len(contract.requests) == 3


def test_unknown_target_reference_is_retried_and_recovers_without_echoing_it():
    contract = HttpContract(result("K999"), result())
    resolved, discovered = contract.resolve()
    assert len(contract.requests) == 2 and discovered == []
    assert resolved[0].binding.actual_character_id is not None
    assert "subject_reference_invalid" in feedback(contract.requests[1])
    assert "K999" not in feedback(contract.requests[1])
    assert len(contract.ledger.reservations) == len(contract.ledger.settlements) == 2
