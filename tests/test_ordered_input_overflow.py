"""Mandatory ordered context limits must stop automatic runs before later batches."""

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


@pytest.mark.parametrize("overflow_on_retry", [False, True], ids=["initial", "retry-feedback"])
def test_automatic_world_input_overflow_stops_before_next_batch(
    overflow_on_retry, monkeypatch, caplog,
):
    context = WorkerAnalysisContext(
        run_id=uuid4(), generation=1, input_state_hash="a" * 64,
        source_hash="b" * 64, format_version=1,
    )
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
        if fixed_limit is None:
            first_size = ensure_ordered_prompt_fits(
                system_prompt, user_prompt, response_schema=response_schema,
            )
            fixed_limit = first_size if overflow_on_retry else first_size - 1
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

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(OrderedInputContextError) as caught,
    ):
        asyncio.run(pipeline.process_all(
            uuid4(), uuid4(), analysis_context=context, continue_on_candidate_failure=True,
        ))

    assert str(caught.value) == "ordered_required_context_exceeds_input_limit"
    assert analysis_failure_code(caught.value) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert comparison_failure_code(caught.value) is AnalysisFailureCode.UNEXPECTED_ERROR
    assert not is_candidate_comparison_failure(caught.value)
    assert len(client.requests) == int(overflow_on_retry)
    assert len(prompt_checks) == 1 + int(overflow_on_retry)
    if overflow_on_retry:
        assert len(prompt_checks[1]) > len(prompt_checks[0])
    assert spring.claim_count == 1
    assert spring.batches == [second]
    assert spring.completions == []
    assert spring.failures == [(
        first.comparison_batch_id, "ordered_required_context_exceeds_input_limit",
        "UNEXPECTED_ERROR", None, None,
    )]
    assert first.candidates[0].evidence_spans[0].quote not in caplog.text
