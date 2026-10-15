"""Exact provider counts govern Sol 6.1 comparison input and reservation."""
import asyncio
import json

import httpx
import pytest

from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import LlmResponseSchema


def _provider(handler):
    return OpenAIResponsesClient(api_key="test-key", model="gpt-6.1-sol",
        responses_api_url="https://api.openai.test/v1/responses", reasoning_effort="medium",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_count_request_matches_generation_input_roles_reasoning_and_schema_and_is_cached():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 256_000})
        return httpx.Response(200, json={"status": "completed", "output_text": "{}",
            "usage": {"input_tokens": 256_000, "output_tokens": 1}})
    client = _provider(handler)
    schema = LlmResponseSchema("comparison", {"type": "object", "properties": {}, "additionalProperties": False})
    kwargs = dict(system_prompt="규칙", user_prompt="한글 근거" * 50_000, response_schema=schema)
    async def run():
        try:
            assert await client.count_input_tokens(**kwargs) == 256_000
            assert await client.count_input_tokens(**kwargs) == 256_000
            await client.create_text_response(**kwargs, max_output_tokens=128_000)
        finally:
            await client.aclose()
    asyncio.run(run())
    assert [request.url.path for request in requests] == ["/v1/responses/input_tokens", "/v1/responses"]
    count_body, generation_body = [json.loads(request.content) for request in requests]
    assert count_body == {key: generation_body[key] for key in count_body}
    assert count_body["model"] == "gpt-6.1-sol"
    assert count_body["reasoning"] == {"effort": "medium"}
    assert count_body["text"]["format"]["schema"] == schema.schema
    assert [item["role"] for item in count_body["input"]] == ["system", "user"]


def test_count_cache_identity_includes_model_full_input_reasoning_and_schema():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"input_tokens": len(requests)})
    client = _provider(handler)
    schema = LlmResponseSchema("comparison", {"type": "object"})
    async def run():
        try:
            base = dict(system_prompt="규칙", user_prompt="본문", response_schema=schema)
            assert await client.count_input_tokens(**base) == 1
            assert await client.count_input_tokens(**base) == 1
            assert await client.count_input_tokens(**{**base, "user_prompt": "본문 변경"}) == 2
            assert await client.count_input_tokens(**{**base, "system_prompt": "규칙 변경"}) == 3
            assert await client.count_input_tokens(**base, model="gpt-6.1-sol-other") == 4
            assert await client.count_input_tokens(**{**base, "response_schema": LlmResponseSchema("other", schema.schema)}) == 5
            assert await client.count_input_tokens(**{**base, "response_schema": LlmResponseSchema(schema.name, {"type": "string"})}) == 6
            assert await client.count_input_tokens(**{**base, "response_schema": LlmResponseSchema(schema.name, schema.schema, strict=False)}) == 7
            client.reasoning_effort = "high"
            assert await client.count_input_tokens(**base) == 8
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(requests) == 8


@pytest.mark.parametrize("payload", [{}, {"input_tokens": True}, {"input_tokens": -1},
    {"input_tokens": 1.5}, {"input_tokens": "256000"}, [], None])
def test_invalid_count_never_generates_or_caches_a_guess(payload):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)
    client = _provider(handler)
    async def run():
        try:
            for _ in range(2):
                with pytest.raises(Exception):
                    await client.count_input_tokens(system_prompt="규칙", user_prompt="원문")
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(requests) == 2
    assert all(request.url.path.endswith("/input_tokens") for request in requests)


def _comparison_run(count_for, refs, *, metered=False, evidence="근거"):
    from app.analysis.character_fact_comparator import CharacterFactComparator
    from app.analysis.character_fact_comparison_pipeline import execute_character_fact_comparison_batch
    from app.usage.metering import MeteredTextGenerationClient
    from tests.test_ai_token_metering import FakeLedger, ANALYSIS_JOB_ID, LEASE_TOKEN
    from tests.test_character_fact_batch import _candidate, _decision
    calls = []
    ledger = FakeLedger()
    def handler(request):
        body = json.loads(request.content)
        prompt = json.loads(body["input"][1]["content"][0]["text"])
        calls.append((request.url.path, body, prompt))
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": count_for(prompt)})
        decisions = [_decision(row["candidate_ref"], operation="ADD",
            resolved_key=f"status.상태_{row['candidate_ref']}", value=row["candidate_ref"]).model_dump(mode="json")
            for row in prompt["candidates"]]
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps({"decisions": decisions}),
            "usage": {"input_tokens": count_for(prompt), "output_tokens": 1}})
    client = _provider(handler)
    delegate = MeteredTextGenerationClient(client, ledger, ANALYSIS_JOB_ID,
        "CHARACTER_FACT_COMPARISON", "gpt-6.1-sol", LEASE_TOKEN) if metered else client
    comparator = CharacterFactComparator(llm_client=delegate, model="gpt-6.1-sol", max_attempts=1)
    async def run():
        try:
            return await execute_character_fact_comparison_batch(comparator,
                matched_character_name="서린", canonical_fact_type="STATUS",
                candidates=[_candidate(ref, f"Q{i}", evidence) for i, ref in enumerate(refs, 1)],
                snapshot_entries=[], advisory_current_context={"currentNarrativeTime": None,
                "currentSlots": [], "historyFacts": [{"operation": "REMOVE", "factKey": "status.지난_부상"}]})
        finally:
            await client.aclose()
    return asyncio.run(run()), calls, ledger


def test_exact_256000_inclusive_even_when_bytes_over_limit_and_reservation_reuses_count():
    result, calls, ledger = _comparison_run(lambda _: 256_000, ["C1", "C2"], metered=True,
        evidence="한글 근거 " * 30_000)
    assert [row.candidate_ref for row in result.decisions] == ["C1", "C2"]
    assert result.failures == []
    assert [path for path, _, _ in calls] == ["/v1/responses/input_tokens", "/v1/responses"]
    assert len(json.dumps(calls[0][1], ensure_ascii=False).encode()) > 256_000
    assert ledger.reservations[0]["reserved_tokens"] == 256_000 + 128_000
    assert ledger.settlements[0][1:] == (256_000, 0, 1, "SUCCESS")
    assert calls[1][2]["advisory_current_context"]["historyFacts"][0]["operation"] == "REMOVE"
    assert calls[1][2]["candidates"][0]["evidence_spans"][0]["quote"] == "한글 근거 " * 30_000


def test_256001_splits_and_recounts_changed_projection_without_reordering():
    result, calls, _ = _comparison_run(lambda prompt: 256_001 if len(prompt["candidates"]) > 1 else 256_000,
        ["C1", "C2"])
    generations = [prompt for path, _, prompt in calls if path == "/v1/responses"]
    assert [[row["candidate_ref"] for row in prompt["candidates"]] for prompt in generations] == [["C1"], ["C2"]]
    assert generations[0]["snapshot_entries"] == []
    assert len(generations[1]["snapshot_entries"]) == 1
    assert [row.candidate_ref for row in result.decisions] == ["C1", "C2"]
    assert result.failures == []
    counts = [prompt for path, _, prompt in calls if path.endswith("/input_tokens")]
    assert any(prompt["snapshot_entries"] for prompt in counts)
    assert all(prompt["advisory_current_context"]["historyFacts"][0]["operation"] == "REMOVE" for prompt in counts)


def test_overflow_prefix_search_is_bounded_and_full_group_fit_uses_one_count():
    refs = [f"C{i}" for i in range(1, 65)]
    result, calls, _ = _comparison_run(lambda prompt: 256_001 if len(prompt["candidates"]) > 32 else 100, refs)
    counts = [prompt for path, _, prompt in calls if path.endswith("/input_tokens")]
    generations = [prompt for path, _, prompt in calls if path == "/v1/responses"]
    assert len(counts) <= 10
    assert [[row["candidate_ref"] for row in prompt["candidates"]] for prompt in generations] == [refs[:32], refs[32:]]
    assert [row.candidate_ref for row in result.decisions] == refs
    assert result.failures == []
    _, fitting_calls, _ = _comparison_run(lambda _: 100, refs)
    assert sum(path.endswith("/input_tokens") for path, _, _ in fitting_calls) == 1


def test_count_failure_is_job_error_and_stops_generation_before_reservation():
    from app.analysis.character_fact_comparator import CharacterFactComparator
    from app.analysis.character_fact_comparison_pipeline import execute_character_fact_comparison_batch
    from app.exceptions.failure_classification import is_candidate_comparison_failure
    from app.llm.exceptions import LlmInputTokenCountError
    from app.usage.metering import MeteredTextGenerationClient
    from tests.test_ai_token_metering import FakeLedger, ANALYSIS_JOB_ID, LEASE_TOKEN
    from tests.test_character_fact_batch import _candidate
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"error": "unavailable"})
    client = _provider(handler)
    ledger = FakeLedger()
    metered = MeteredTextGenerationClient(client, ledger, ANALYSIS_JOB_ID,
        "CHARACTER_FACT_COMPARISON", "gpt-6.1-sol", LEASE_TOKEN)
    async def run():
        try:
            with pytest.raises(LlmInputTokenCountError) as raised:
                await execute_character_fact_comparison_batch(CharacterFactComparator(llm_client=metered,
                    model="gpt-6.1-sol"), matched_character_name="서린", canonical_fact_type="STATUS",
                    candidates=[_candidate("C1", "Q1", "근거")], snapshot_entries=[])
            assert not is_candidate_comparison_failure(raised.value)
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(requests) == 1 and requests[0].url.path.endswith("/input_tokens")
    assert ledger.reservations == []
    assert ledger.settlements == []


def test_schema_feedback_recounts_changed_input_and_blocks_oversized_retry():
    from app.analysis.character_fact_comparator import CharacterFactComparator, CharacterComparisonInputTooLargeError
    from tests.test_character_fact_batch import _candidate
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        count_calls = sum(path.endswith("/input_tokens") for path, _ in requests)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100 if count_calls == 1 else 256_001})
        return httpx.Response(200, json={"status": "completed", "output_text": '{"decisions": []}',
            "usage": {"input_tokens": 100, "output_tokens": 5}})
    client = _provider(handler)
    async def run():
        try:
            with pytest.raises(CharacterComparisonInputTooLargeError):
                await CharacterFactComparator(llm_client=client, model="gpt-6.1-sol", max_attempts=2).compare_batch(
                    matched_character_name="서린", canonical_fact_type="STATUS",
                    candidates=[_candidate("C1", "Q1", "근거")], snapshot_entries=[])
        finally:
            await client.aclose()
    asyncio.run(run())
    assert [path for path, _ in requests] == ["/v1/responses/input_tokens", "/v1/responses", "/v1/responses/input_tokens"]
    assert requests[0][1]["input"] != requests[2][1]["input"]


@pytest.mark.parametrize("purpose", ["SETTING_EXTRACTION", "WORLD_SETTING_COMPARISON", "SUBJECT_RESOLUTION"])
def test_unrelated_purposes_do_not_add_count_endpoint_calls(purpose, monkeypatch):
    from app.usage import metering
    from tests.test_ai_token_metering import FakeLedger, ANALYSIS_JOB_ID, LEASE_TOKEN
    monkeypatch.setattr(metering, "_estimate_text_token_upper_bound", lambda *args: 999)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "completed", "output_text": "{}",
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    client = _provider(handler)
    ledger = FakeLedger()
    async def run():
        try:
            await metering.MeteredTextGenerationClient(client, ledger, ANALYSIS_JOB_ID,
                purpose, "gpt-6.1-sol", LEASE_TOKEN).create_text_response(system_prompt="규칙", user_prompt="본문")
        finally:
            await client.aclose()
    asyncio.run(run())
    assert [request.url.path for request in requests] == ["/v1/responses"]
    assert ledger.reservations[0]["reserved_tokens"] == 999


def test_sol61_never_estimates_or_generates_when_counter_is_missing(monkeypatch):
    import app.analysis.character_fact_comparator as module
    from app.llm.exceptions import LlmInputTokenCountError
    from tests.test_character_fact_batch import FakeTextClient, _candidate
    monkeypatch.setattr(module, "_estimate_prompt_tokens", lambda *args: pytest.fail("byte/tokenizer fallback"))
    client = FakeTextClient({})
    async def run():
        with pytest.raises(LlmInputTokenCountError):
            await module.CharacterFactComparator(llm_client=client, model="gpt-6.1-sol").compare_batch(
                matched_character_name="서린", canonical_fact_type="STATUS",
                candidates=[_candidate("C1", "Q1", "근거")], snapshot_entries=[])
    asyncio.run(run())
    assert client.requests == []


@pytest.mark.parametrize("ordered", [False, True])
def test_exact_mandatory_probe_preserves_book_time_history_and_system_instructions(ordered):
    from app.analysis.character_fact_comparator import CharacterFactComparator
    from app.schemas.analysis_source import WorkerAnalysisSource
    from tests.test_setting_book_source_contract import SOURCE
    from tests.test_character_fact_batch import _candidate
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"input_tokens": 100})
    client = _provider(handler)
    candidate = _candidate("C1", "Q1", "화상").model_copy(update={
        "source": WorkerAnalysisSource.model_validate(SOURCE), "source_episode_no": None})
    advisory = {"currentNarrativeTime": None, "currentSlots": [], "historyFacts": [{"operation": "REMOVE"}]}
    comparator = CharacterFactComparator(llm_client=client, model="gpt-6.1-sol")
    async def run():
        try:
            kwargs = dict(matched_character_name="서린", canonical_fact_type="STATUS", candidates=[candidate],
                snapshot_entries=[], ordered_context=ordered, advisory_current_context=advisory)
            assert await comparator.batch_fits(**kwargs)
            assert await comparator.mandatory_context_fits(**kwargs)
        finally:
            await client.aclose()
    asyncio.run(run())
    full, mandatory = captured
    assert mandatory["input"][0] == full["input"][0]
    assert "설정집" in mandatory["input"][0]["content"][0]["text"]
    assert mandatory.get("text") == full.get("text")
    full_prompt = json.loads(full["input"][1]["content"][0]["text"])
    mandatory_prompt = json.loads(mandatory["input"][1]["content"][0]["text"])
    assert mandatory_prompt == {**full_prompt, "candidates": []}
    assert mandatory_prompt["advisory_current_context"] == advisory


def test_evaluation_usage_wrapper_forwards_exact_count_without_recording_generation():
    from evals.multi_stage_setting.runtime_adapter import UsageRecordingTextGenerationClient, RuntimeUsageCounter
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"input_tokens": 123})
    client = _provider(handler)
    usage = RuntimeUsageCounter()
    wrapped = UsageRecordingTextGenerationClient(client, usage)
    async def run():
        try:
            assert await wrapped.count_input_tokens(system_prompt="규칙", user_prompt="본문") == 123
            assert await wrapped.count_input_tokens(system_prompt="규칙", user_prompt="본문") == 123
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(requests) == 1
    assert usage.snapshot() == (0, 0, 0)


def test_count_failure_persists_batch_failure_then_stops_before_next_claim():
    from app.analysis.character_fact_comparator import CharacterFactComparator
    from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
    from app.llm.exceptions import LlmInputTokenCountError
    from tests.test_character_fact_batch import FakeBatchSpring, FakeTextClient, _batch, _context, ANALYSIS_JOB_ID, LEASE_TOKEN
    class CountingSpring(FakeBatchSpring):
        claim_count = 0
        async def claim_next_character_fact_comparison_batch(self, *args):
            self.claim_count += 1
            return await super().claim_next_character_fact_comparison_batch(*args)
    batch = _batch()
    spring = CountingSpring(batch, _context(batch))
    spring.batches.append(batch)
    client = FakeTextClient({})
    comparator = CharacterFactComparator(llm_client=client, model="gpt-6.1-sol")
    with pytest.raises(LlmInputTokenCountError):
        asyncio.run(CharacterFactComparisonPipeline(spring, comparator).process_all(ANALYSIS_JOB_ID, LEASE_TOKEN))
    assert spring.claim_count == 1
    assert len(spring.batches) == 1
    assert spring.completions == []
    assert len(spring.failures) == 1
    assert spring.failures[0][2] == "UNEXPECTED_ERROR"
    assert client.requests == []
