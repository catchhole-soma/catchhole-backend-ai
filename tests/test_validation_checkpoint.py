"""Grid v4 resume and publication through the real ValidationCheckpoint. Provider data are synthetic."""
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.validation.checkpoint import ValidationCheckpoint
from app.validation.client import ExecutionScope
from app.validation.contracts import segment_wire, split_setting_segments
from app.validation.projection import compact_setting_row, model_settings
from app.validation.service import SettingValidationService
from tests.support.validation_synthetic import (
    ROLES,
    TARGET,
    ScriptedModel,
    SyntheticData,
    budget,
    draft,
)


class RecordingJava:
    """Java publication boundary for the real ValidationCheckpoint: records every leased command."""

    def __init__(self, segments, *, crash_on=None):
        self.calls, self.loads, self.crash_on = [], 0, crash_on
        self.grid = {"gridVersion": 4, "segments": [segment_wire(segment) for segment in segments]}

    async def leased(self, claim, operation, gate=None, **body):
        self.calls.append((operation, deepcopy(body)))
        if self.crash_on and self.crash_on(operation, body):
            raise asyncio.CancelledError()
        if operation == "plans" and body["stage"] == "MERGED_PLAN":
            return {"checks": [{"localRef": row["localRef"], "checkId": f"check-{row['localRef']}", "revision": 1,
                                "checkInputDigest": "d" * 64, "outcome": None, "decision": None,
                                "state": row["state"]} for row in body["payload"]["questions"]]}
        return {}

    async def load_checkpoint(self, claim, gate=None):
        self.loads += 1
        return {"requiredGrid": {"SETTING": deepcopy(self.grid)}}

    def published(self, operation):
        return [body for name, body in self.calls if name == operation]


def execution_claim():
    return SimpleNamespace(scope=ExecutionScope(contractVersion=1, reportId=uuid4(), generation=1,
                                                inputDigest="a" * 64))


def real_checkpoint(java, claim, **view):
    view = {"scope": claim.scope.model_dump(mode="json"), "stages": [], "checks": [], "branches": {}} | view
    return ValidationCheckpoint(java, claim, view, None)


def run(data, model, checkpoint, **limits):
    return asyncio.run(SettingValidationService(data, model, budget(**limits), ROLES, checkpoint=checkpoint).inspect())


def stages_of(java):
    return [{"stage": body["stage"], "ref": body["ref"], "payload": body["payload"], "successful": True,
             "branch": "SETTING"} for body in java.published("plans")]


def test_crash_after_planning_resumes_judging_only_and_a_decided_run_makes_no_calls():
    segments = split_setting_segments(TARGET, unit_chars=10000, context_chars=100)
    data, claim = SyntheticData(), execution_claim()
    crashing = RecordingJava(segments, crash_on=lambda operation, body: operation == "units")
    with pytest.raises(asyncio.CancelledError):
        run(data, ScriptedModel({"TSEG1": [draft()]}), real_checkpoint(crashing, claim))
    merged = next(body for body in crashing.published("plans") if body["stage"] == "MERGED_PLAN")
    (question,) = merged["payload"]["questions"]
    assert question["state"]["lookup_obligations"] == [] and "lookup_intents" not in question["state"]["draft"]
    assert question["originalRefs"] == ["PLANNER:TSEG1:hand"]

    checks = [{"localRef": "Q1", "checkId": "check-Q1", "revision": 1, "checkInputDigest": "d" * 64,
               "outcome": None, "decision": None, "state": question["state"]}]
    java, model = RecordingJava(segments), ScriptedModel({"TSEG1": [draft()]})
    checkpoint = real_checkpoint(java, claim, stages=stages_of(crashing), checks=checks,
                                 requiredGrid={"SETTING": crashing.grid})
    result = run(data, model, checkpoint)
    assert result.status == "COMPLETED"
    assert [role for role, _, _ in model.calls] == ["JUDGE"] and java.published("plans") == []
    (unit,) = java.published("units")
    assert (unit["ref"], unit["checkId"], unit["outcome"]) == ("Q1", "check-Q1", "NO_CONFLICT")

    decided = {**checks[0], "outcome": "NO_CONFLICT",
               "decision": {**result.decisions["Q1"].model_dump(), "evidence_bundle": unit["payload"]["evidence_bundle"]}}
    again_java, again_model = RecordingJava(segments), ScriptedModel({"TSEG1": [draft()]})
    again = run(data, again_model, real_checkpoint(again_java, claim, stages=stages_of(crashing), checks=[decided],
                                                   requiredGrid={"SETTING": crashing.grid}))
    assert again.status == "COMPLETED" and again_model.calls == [] and again_java.calls == []
    assert again.evidence_bundles["Q1"] == unit["payload"]["evidence_bundle"]


def test_published_decision_carries_setting_refs_only_and_fits_the_publication_cap():
    cap = json.loads((Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json")
                     .read_text())["execution"]["maxPublicationBytes"]
    data = SyntheticData()
    data.entries[0]["state"]["historyFacts"] = [  # a production entry held 209 KB of history; the cap is 1 MiB
        {"factType": "STATUS", "factKey": "손", "factValue": f"양손 사용 {n}", "supportingSource": {"quote": "옛 원문" * 20}}
        for n in range(4000)]
    data.rows = model_settings(data.entries)
    assert len(json.dumps(data.rows[0], ensure_ascii=False).encode()) > cap  # a full-row bundle could never be published

    segments = split_setting_segments(TARGET, unit_chars=10000, context_chars=100)
    java, model = RecordingJava(segments), ScriptedModel({"TSEG1": [draft()]})
    checkpoint = real_checkpoint(java, execution_claim())
    result = run(data, model, checkpoint)
    assert result.status == "COMPLETED"
    (judge,) = model.judge_calls()
    assert judge["settings"] == [compact_setting_row(data.rows[0])] and "historyFacts" not in json.dumps(judge)
    (unit,) = java.published("units")
    bundle = unit["payload"]["evidence_bundle"]
    assert bundle["settings"] == [{"entry_ref": "B1", "subject_ref": "S1"}]
    assert bundle["input_digest"] == judge["input_digest"]
    assert set(bundle) == {"question_ref", "check_revision", "question", "target_context", "additional_target_context",
                           "settings", "evidence", "receipts", "source_catalogue", "input_digest"}
    assert unit["payload"]["follow_ups"] == [] and unit["payload"]["affected_question_refs"] == []
    assert checkpoint.checks["Q1"]["bundle"] == bundle
    # The grid was loaded once after the first PLANNER publication created it.
    assert java.loads == 1
    # Every command of the run stays under the Java ceiling, and no runtime checkpoint or revision is written.
    assert max(len(json.dumps(body, ensure_ascii=False).encode()) for _, body in java.calls) < cap
    assert {name for name, _ in java.calls} == {"plans", "units"}


def test_setting_grid_is_reloaded_once_after_the_first_publication_creates_it():
    segments = split_setting_segments(TARGET, unit_chars=10000, context_chars=100)
    java, claim = RecordingJava(segments), execution_claim()

    async def check():
        checkpoint = real_checkpoint(java, claim, requiredGrid=None)
        assert await checkpoint.setting_grid() == java.grid
        assert await checkpoint.setting_grid() == java.grid
        assert java.loads == 1
        known = real_checkpoint(java, claim, requiredGrid={"SETTING": {"gridVersion": 3}})
        assert await known.setting_grid() == {"gridVersion": 3} and java.loads == 1

    asyncio.run(check())


def test_leased_data_uses_trusted_baseline_revision_and_projection_version_two():
    from dataclasses import replace

    import httpx

    from app.validation.data import ValidationDataClient
    from tests.test_typo_checker import OfflineRun

    fixture = OfflineRun("합성 원고")
    context = replace(fixture.context, baseline_revision=3, job_id=uuid4(), lease_token=uuid4(),
                      manifest_hash="c" * 64)

    def server(version):
        def respond(request):
            assert request.headers["X-Validation-Job-Id"] == str(context.job_id)
            assert request.headers["X-Worker-Lease-Token"] == str(context.lease_token)
            data = fixture.spring(request).json()["data"]
            data.update(baselineRevision=3, projectionVersion=version, modelSourceCatalogue=[])
            return httpx.Response(200, json={"data": data})
        return respond

    async def read(version):
        async with httpx.AsyncClient(transport=httpx.MockTransport(server(version))) as http:
            client = ValidationDataClient("https://spring.invalid", "synthetic", context, http)
            frozen = await client.read_input()
            return frozen, await client.read_source(frozen.target)

    frozen, source = asyncio.run(read(2))
    assert frozen.model_source_catalogue == [] and source.text == "합성 원고"
    from app.validation.contracts import IncompleteInspection

    with pytest.raises(IncompleteInspection, match="INPUT_CHANGED"):
        asyncio.run(read(1))


def test_typo_resume_preserves_classification_and_calls_only_failed_segment():
    import httpx

    from app.llm.openai_client import OpenAIResponsesClient
    from app.validation.data import ValidationDataClient
    from app.validation.typo import check_typos
    from tests.test_typo_checker import TYPO_ROLE, OfflineRun, batch, suggestion
    from tests.test_typo_checker import limits as typo_limits

    class TypoSaved:
        def __init__(self):
            self.stages = {}

        def bind_sources(self, frozen):
            pass

        def stage(self, stage, ref):
            return deepcopy(self.stages.get((stage, ref)))

        async def typo(self, ref, payload):
            self.stages["TYPO_SEGMENT", ref] = deepcopy(payload)

    async def check():
        saved = TypoSaved()
        fixture = OfflineRun("됬다. 문장.", {"TSEG1": batch(suggestions=[suggestion(0, "됬다", "됐다")]),
                                           "TSEG2": httpx.Response(503)})
        async with (httpx.AsyncClient(transport=httpx.MockTransport(fixture.spring)) as spring,
                    httpx.AsyncClient(transport=httpx.MockTransport(fixture.provider)) as provider):
            data = ValidationDataClient("https://spring.invalid", "synthetic", fixture.context, spring)
            llm = OpenAIResponsesClient("synthetic", "gpt-6.1-terra", "https://provider.invalid/responses",
                                        http_client=provider)
            first = await check_typos(data, llm, budget=typo_limits(segment_chars=4), role=TYPO_ROLE, checkpoint=saved)
            assert first.status == "INCOMPLETE"
            count = len([path for path, _ in fixture.requests if path.endswith("responses")])
            fixture.replies["TSEG2"] = batch("TSEG2")
            second = await check_typos(data, llm, budget=typo_limits(segment_chars=4), role=TYPO_ROLE, checkpoint=saved)
            assert second.status == "COMPLETED" and second.suggestions[0].category == "SPELLING"
            assert len([path for path, _ in fixture.requests if path.endswith("responses")]) == count + 1

    asyncio.run(check())


def test_a_question_level_failure_publishes_one_incomplete_unit_that_the_next_run_judges_again():
    """Owner decision ③ through the real checkpoint: the failed question's spans are cited, the rest is judged."""
    segments = split_setting_segments(TARGET, unit_chars=10000, context_chars=100)
    data, claim = SyntheticData(), execution_claim()
    java = RecordingJava(segments)
    result = run(data, ScriptedModel({"TSEG1": [draft()]}, lambda payload, n: "{not json"), real_checkpoint(java, claim))
    assert (result.status, result.failure_code) == ("INCOMPLETE", "SETTING_QUESTIONS_INCOMPLETE")
    (unit,) = java.published("units")
    assert (unit["ref"], unit["checkId"], unit["expectedCheckRevision"], unit["outcome"]) == ("Q1", "check-Q1", 1,
                                                                                             "INCOMPLETE")
    spans = result.questions[0].draft.target_spans
    assert unit["payload"] == {"action": "INCOMPLETE", "outcome": "INCOMPLETE", "failure_code": "MODEL_CONTRACT_INVALID",
                               "failure_cause": "MODEL_CONTRACT_JSON_INVALID", "failed_call": 1,
                               "target_spans": [span.model_dump() for span in spans]}
    assert [(row["sourceRef"], row["start"], row["end"], row["quote"]) for row in unit["citations"]] == [
        ("T1", span.start, span.end, span.quote) for span in spans]

    merged = next(body for body in java.published("plans") if body["stage"] == "MERGED_PLAN")
    incomplete = [{"localRef": "Q1", "checkId": "check-Q1", "revision": 1, "checkInputDigest": "d" * 64,
                   "outcome": "INCOMPLETE", "decision": unit["payload"],
                   "state": merged["payload"]["questions"][0]["state"]}]
    retry_java, retry_model = RecordingJava(segments), ScriptedModel({"TSEG1": [draft()]})
    retried = run(data, retry_model, real_checkpoint(retry_java, claim, stages=stages_of(java), checks=incomplete,
                                                     requiredGrid={"SETTING": java.grid}))
    assert retried.status == "COMPLETED" and [role for role, _, _ in retry_model.calls] == ["JUDGE"]
    (decided,) = retry_java.published("units")
    assert (decided["ref"], decided["expectedCheckRevision"], decided["outcome"]) == ("Q1", 1, "NO_CONFLICT")
