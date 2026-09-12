import asyncio
import json
import logging
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.analysis import ordered_context, world_setting_comparator
from app.analysis.exceptions import ComparisonValidationError, LlmExtractionError
from app.analysis.world_setting_comparator import (
    SUBJECT_PROMPT_PATH,
    WorldSettingSubjectResolver,
)
from app.llm.openai_client import OpenAIResponsesClient
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingSubject,
    WorkerWorldSettingSubjectResolutionCandidate,
)
from app.usage import metering
from app.usage.metering import MeteredTextGenerationClient


JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
SUBJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000003")


class _OfflineEncoding:
    def encode(self, text, **kwargs):
        return list(text.encode("utf-8"))


class _Ledger:
    def __init__(self):
        self.reservations = []
        self.settlements = []

    async def reserve_ai_tokens(self, **kwargs):
        self.reservations.append(kwargs)

    async def settle_ai_tokens(self, request_id, input_tokens, cached_input_tokens,
                               output_tokens, outcome):
        self.settlements.append((request_id, input_tokens, cached_input_tokens,
                                 output_tokens, outcome))

    async def release_ai_tokens(self, request_id, outcome):
        raise AssertionError("Every mocked completed response includes usage.")


@pytest.fixture(autouse=True)
def _offline_dependencies(monkeypatch):
    monkeypatch.setattr(ordered_context.tiktoken, "get_encoding", lambda name: _OfflineEncoding())
    monkeypatch.setattr(metering, "_encoding_for_model", lambda name: _OfflineEncoding())
    monkeypatch.setattr(world_setting_comparator, "get_settings", lambda: SimpleNamespace())


def _candidate():
    return WorkerWorldSettingSubjectResolutionCandidate(
        candidate_id=JOB_ID, source_episode_id=JOB_ID, category="LOCATION",
        subject_name="백탑", evidence_spans=[{"quote": "동쪽 언덕에 백탑이 있다."}],
    )


def _subject(*, provisional=True):
    identity = ({"provisional_subject_key": f"provisional-world:{SUBJECT_ID}"}
                if provisional else {"world_setting_id": SUBJECT_ID})
    return WorkerWorldSettingSubject(
        **identity, subject_name="백탑", aliases=["동쪽 탑"],
        identity_evidence=[{"quote": "동쪽 언덕의 탑을 백탑이라고 부른다."}],
        provenance={"confirmationStatus": "PROVISIONAL" if provisional else "CONFIRMED",
                    "sourceEpisodeNo": 1},
    )


def _run_resolver(outputs, *, subject=None, legacy=False, attempts=3, subjects_empty=False):
    """Exercise the production wrappers while terminating HTTP at MockTransport."""
    requests = []
    ledger = _Ledger()
    subject = subject or _subject()

    def respond(request):
        request_body = json.loads(request.content)
        requests.append(request_body)
        index = len(requests) - 1
        assert index < len(outputs), "Unexpected extra provider attempt."
        output = outputs[index]
        return httpx.Response(200, json={
            "status": "completed",
            "output_text": output if isinstance(output, str) else json.dumps(output),
            "usage": {"input_tokens": 120, "output_tokens": 20,
                      "input_tokens_details": {"cached_tokens": 10}},
        })

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
            provider = OpenAIResponsesClient(
                api_key="test-only", model="gpt-5.6-luna", reasoning_effort="none",
                responses_api_url="https://api.openai.test/v1/responses", http_client=http_client,
            )
            client = MeteredTextGenerationClient(
                delegate=provider, ledger=ledger, analysis_job_id=JOB_ID,
                purpose="WORLD_SETTING_SUBJECT_RESOLUTION", default_model="gpt-5.6-luna",
                lease_token=LEASE_TOKEN, max_retries=0,
            )
            resolver = WorldSettingSubjectResolver(
                llm_client=client, model="gpt-5.6-luna", max_attempts=attempts,
                max_output_tokens=2000,
            )
            if legacy:
                candidate = WorkerWorldSettingCandidatePayload(
                    candidate_id=JOB_ID, work_id=JOB_ID, source_episode_id=JOB_ID,
                    category="LOCATION", subject_name="백탑", setting_name="위치",
                    extracted_value="동쪽", evidence_spans=[],
                )
                return await resolver.select_subjects(candidate, [subject])
            return await resolver.select_ordered_subjects(
                _candidate(), [] if subjects_empty else [subject],
            )

    try:
        result = asyncio.run(execute())
    except Exception as error:
        return error, requests, ledger
    return result, requests, ledger


@pytest.mark.parametrize("provisional", [False, True])
def test_ordered_world_schema_reaches_provider_and_token_reservation(provisional):
    subject = _subject(provisional=provisional)
    result, requests, ledger = _run_resolver(
        [{"selected_subject_refs": ["S1"], "ambiguous": False}], subject=subject,
    )
    assert result == ([subject], False)
    body = requests[0]
    response_format = body["text"]["format"]
    assert response_format["type"] == "json_schema"
    assert response_format["strict"] is True
    assert response_format["name"] == "ordered_world_subject_resolution"
    schema = response_format["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == {
        "selected_subject_refs", "ambiguous",
    }
    assert schema["properties"]["selected_subject_refs"]["maxItems"] == 3
    assert schema["properties"]["ambiguous"]["type"] == "boolean"
    assert body["prompt_cache_key"] == "ordered-world-subject-resolution:v4"
    assert body["model"] == "gpt-5.6-luna"
    assert body["reasoning"] == {"effort": "none"}
    assert body["store"] is False
    system = body["input"][0]["content"][0]["text"]
    user = body["input"][1]["content"][0]["text"]
    assert '"selected_subject_refs"' in system and '"ambiguous"' in system
    assert str(SUBJECT_ID) not in system + user
    assert "동쪽 언덕에 백탑이 있다." in user
    assert ledger.reservations[0]["purpose"] == "WORLD_SETTING_SUBJECT_RESOLUTION"
    assert ledger.reservations[0]["reserved_tokens"] > metering._estimate_text_token_upper_bound(
        system, user, "gpt-5.6-luna", 2000,
    )
    assert ledger.settlements[0][1:] == (120, 10, 20, "SUCCESS")


@pytest.mark.parametrize("invalid", [
    {"ambiguous": False},
    {"selected_subject_refs": []},
    {"selected_subject_refs": [], "ambiguous": None},
    {"selected_subject_refs": [], "ambiguous": False, "SECRET_UNKNOWN_KEY": "SECRET_VALUE"},
    {"selected_subject_refs": "SECRET_VALUE", "ambiguous": False},
    "SECRET_INVALID_JSON",
])
def test_ordered_world_schema_retry_preserves_schema_and_omits_rejected_values(invalid, caplog):
    with caplog.at_level(logging.WARNING):
        result, requests, ledger = _run_resolver([
            invalid, {"selected_subject_refs": [], "ambiguous": True},
        ])
    assert result == ([], True)
    assert len(requests) == 2
    assert requests[0]["text"] == requests[1]["text"]
    first = requests[0]["input"][1]["content"][0]["text"]
    second = requests[1]["input"][1]["content"][0]["text"]
    assert second.startswith(first + "\n\n")
    assert "response_format_feedback" in second
    assert "SECRET_" not in second + caplog.text
    assert [row["attempt"] for row in ledger.reservations] == [1, 2]
    assert ledger.reservations[0]["request_id"] != ledger.reservations[1]["request_id"]


@pytest.mark.parametrize("invalid", [
    {"selected_subject_refs": ["S999"], "ambiguous": False},
    {"selected_subject_refs": ["S1", "S1"], "ambiguous": False},
    {"selected_subject_refs": ["S999"], "ambiguous": True},
    {"selected_subject_refs": ["S1", "S1"], "ambiguous": True},
])
def test_ordered_world_keeps_unknown_and_duplicate_target_rejection(invalid):
    result, requests, ledger = _run_resolver([invalid] * 3)
    assert isinstance(result, ComparisonValidationError)
    assert "failed after 3 attempts" in str(result)
    assert len(requests) == len(ledger.reservations) == len(ledger.settlements) == 3


@pytest.mark.parametrize("provisional", [False, True])
def test_ordered_world_explicit_ambiguity_discards_valid_targets_without_retry(provisional):
    result, requests, ledger = _run_resolver(
        [{"selected_subject_refs": ["S1"], "ambiguous": True}],
        subject=_subject(provisional=provisional),
    )
    assert result == ([], True)
    assert len(requests) == len(ledger.reservations) == len(ledger.settlements) == 1
    assert ledger.settlements[0][-1] == "SUCCESS"


@pytest.mark.parametrize("invalid", [
    {"selected_subject_refs": ["SECRET_UNKNOWN_TARGET"], "ambiguous": False},
    {"selected_subject_refs": [str(SUBJECT_ID)], "ambiguous": False},
    {"selected_subject_refs": ["S1", "S1"], "ambiguous": False},
])
def test_ordered_world_domain_retry_recovers_valid_target_without_leaking_values(invalid, caplog):
    subject = _subject()
    with caplog.at_level(logging.WARNING):
        result, requests, ledger = _run_resolver([
            invalid, {"selected_subject_refs": ["S1"], "ambiguous": False},
        ], subject=subject)

    assert result == ([subject], False)
    assert len(requests) == len(ledger.reservations) == len(ledger.settlements) == 2
    assert requests[0]["text"] == requests[1]["text"]
    first = requests[0]["input"][1]["content"][0]["text"]
    second = requests[1]["input"][1]["content"][0]["text"]
    assert second.startswith(first + "\n\n")
    assert "response_format_feedback" in second
    assert "SECRET_UNKNOWN_TARGET" not in second + caplog.text
    assert str(SUBJECT_ID) not in second + caplog.text
    assert [row["attempt"] for row in ledger.reservations] == [1, 2]
    assert all(row[1:] == (120, 10, 20, "SUCCESS") for row in ledger.settlements)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_ordered_world_new_and_unresolved_are_distinct_explicit_results(ambiguous):
    result, _, _ = _run_resolver([{"selected_subject_refs": [], "ambiguous": ambiguous}])
    assert result == ([], ambiguous)


def test_ordered_world_missing_fields_exhaust_attempts_without_inventing_new_subject():
    result, requests, _ = _run_resolver([{}, {}], attempts=2)
    assert isinstance(result, LlmExtractionError)
    assert "failed after 2 attempts" in str(result)
    assert len(requests) == 2


def test_ordered_world_without_targets_never_calls_provider():
    result, requests, ledger = _run_resolver([], subjects_empty=True)
    assert result == ([], False)
    assert requests == ledger.reservations == []


def test_legacy_world_subject_request_remains_schema_free_and_unchanged():
    subject = _subject(provisional=False)
    result, requests, _ = _run_resolver(
        [{"selected_subject_refs": ["S1"]}], subject=subject, legacy=True,
    )
    assert result[0].world_setting_id == SUBJECT_ID
    assert requests == [{
        "model": "gpt-5.6-luna", "store": False,
        "input": [
            {"role": "system", "content": [{
                "type": "input_text", "text": SUBJECT_PROMPT_PATH.read_text(encoding="utf-8"),
            }]},
            {"role": "user", "content": [{
                "type": "input_text", "text": json.dumps({
                    "candidate": {"category": "LOCATION", "subject_name": "백탑"},
                    "subjects": [{"ref": "S1", "subject_name": "백탑"}],
                }, ensure_ascii=False),
            }]},
        ],
        "max_output_tokens": 2000,
        "prompt_cache_key": "world-setting-subject-resolution:v1",
        "reasoning": {"effort": "none"},
    }]
