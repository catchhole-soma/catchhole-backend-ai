"""World token overflow is reviewable; frozen-input violations remain fatal."""

import asyncio
import logging
import socket
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analysis import ordered_context, world_setting_comparator
from app.analysis.exceptions import OrderedInputContextError
from app.analysis.ordered_context import ensure_ordered_prompt_fits
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.domain.enums import AnalysisFailureCode
from app.exceptions.failure_classification import (
    analysis_failure_code,
    comparison_failure_code,
    is_candidate_comparison_failure,
)
from app.llm.responses import LlmTextResponse
from app.schemas.analysis_context import AnalysisStateProvenance, WorkerAnalysisContext
from tests.test_world_setting_batch_pipeline import FakeBatchSpringApi, _batch


@pytest.fixture(autouse=True)
def offline_dependencies(monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("Input-limit tests must not open network connections.")

    class Encoding:
        def encode(self, text, **kwargs):
            return list(text.encode("utf-8"))

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(ordered_context.tiktoken, "get_encoding", lambda name: Encoding())
    monkeypatch.setattr(world_setting_comparator, "get_settings", lambda: SimpleNamespace())


@pytest.mark.parametrize("allow_recovery", [False, True], ids=["schema-retry", "recovery"])
@pytest.mark.parametrize("ordered", [False, True], ids=["confirmed", "ordered"])
@pytest.mark.parametrize("frozen_error", [False, True], ids=["token-budget", "frozen-input"])
@pytest.mark.parametrize("overflow_on_retry", [False, True], ids=["initial", "recovery-boundary"])
def test_world_token_overflow_holds_only_the_batch_and_preserves_frozen_error_boundary(
    overflow_on_retry, frozen_error, ordered, allow_recovery, monkeypatch, caplog,
):
    context = WorkerAnalysisContext(
        run_id=uuid4(), generation=1, input_state_hash="a" * 64,
        source_hash="b" * 64, format_version=1,
    ) if ordered else None
    first = _batch().model_copy(update={"analysis_context": context})
    second = first.model_copy(update={"comparison_batch_id": uuid4()})

    class Spring(FakeBatchSpringApi):
        async def get_world_setting_comparison_batch_context(
            self, *args, provisional_subject_keys=(),
        ):
            response = await super().get_world_setting_comparison_batch_context(*args)
            targets = [target.model_copy(update={
                "properties": [prop.model_copy(update={
                    "provenance": AnalysisStateProvenance(confirmation_status="CONFIRMED"),
                }) for prop in target.properties],
            }) for target in response.targets]
            return response.model_copy(update={
                "analysis_context": context, "context_token": "c" * 64, "targets": targets,
            })

    class Client:
        def __init__(self):
            self.requests = []

        async def create_text_response(self, **kwargs):
            self.requests.append(kwargs)
            # The real validator rejects this and builds the real retry feedback.
            return LlmTextResponse(text='{"decisions": []}')

    prompt_checks = []
    fixed_limit = None

    def check_fixed_limit(system_prompt, user_prompt, max_tokens=64000, response_schema=None):
        nonlocal fixed_limit
        prompt_checks.append(user_prompt)
        if frozen_error and (not overflow_on_retry or len(prompt_checks) > 1):
            raise OrderedInputContextError("frozen_input_changed")
        if fixed_limit is None:
            first_size = ensure_ordered_prompt_fits(
                system_prompt, user_prompt, response_schema=response_schema,
            )
            fixed_limit = first_size if overflow_on_retry else first_size - 1
        elif overflow_on_retry:
            # Recovery sends a bounded fresh request without old retry feedback.
            # Make this request exceed the budget without calling the provider.
            fixed_limit = 0
        return ensure_ordered_prompt_fits(
            system_prompt, user_prompt, fixed_limit, response_schema=response_schema,
        )

    monkeypatch.setattr(ordered_context, "ensure_ordered_prompt_fits", check_fixed_limit)
    client = Client()
    comparator = WorldSettingComparator(
        llm_client=client, model="offline-test-model", max_attempts=3,
        max_output_tokens=3000, batch_max_output_tokens=16000,
    )
    spring = Spring([first, second])
    pipeline = WorldSettingComparisonPipeline(spring, object(), comparator)
    compare_batch = pipeline._compare_batch_with_fresh_context

    async def compare_with_retry_mode(*args, **kwargs):
        # process_all normally enables recovery. Exercise the direct comparator's
        # schema retry too, with the same persistence and next-batch boundary.
        kwargs["allow_recovery"] = allow_recovery
        return await compare_batch(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_compare_batch_with_fresh_context", compare_with_retry_mode)

    if frozen_error:
        with caplog.at_level(logging.WARNING), pytest.raises(OrderedInputContextError) as caught:
            asyncio.run(pipeline.process_all(
                uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=allow_recovery,
            ))
        assert str(caught.value) == "frozen_input_changed"
        assert analysis_failure_code(caught.value) is AnalysisFailureCode.UNEXPECTED_ERROR
        assert comparison_failure_code(caught.value) is AnalysisFailureCode.UNEXPECTED_ERROR
        assert not is_candidate_comparison_failure(caught.value)
        assert spring.claim_count == 1
        assert spring.batches == [second]
        assert spring.completions == []
        assert len(spring.failures) == 1
    else:
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(pipeline.process_all(
                uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=allow_recovery,
            ))
        assert result.completed_count == 4
        assert result.failed_count == 0
        assert spring.claim_count == 3
        assert spring.batches == []
        assert spring.failures == []
        assert len(spring.completions) == 2
        for completion in spring.completions:
            assert not completion.failures
            assert [d.source_candidate_refs for d in completion.decisions] == [["C1"], ["C2"]]
            for decision, original in zip(completion.decisions, first.candidates, strict=True):
                assert decision.suggested_operation == "REVIEW_REQUIRED"
                assert decision.comparison_review_reason == "BATCH_LIMIT_EXCEEDED"
                assert decision.proposed_scope_name == original.scope_name
                assert decision.proposed_setting_name == original.setting_name
                assert decision.proposed_value == original.extracted_value
    assert len(client.requests) == int(overflow_on_retry)
    assert len(prompt_checks) == 1 + int(overflow_on_retry) + int(not frozen_error)
    if overflow_on_retry:
        assert ("validation_feedback" in prompt_checks[1]) is (not allow_recovery)
    assert first.candidates[0].evidence_spans[0].quote not in caplog.text
