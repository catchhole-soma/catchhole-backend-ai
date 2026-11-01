"""Semantic resume tests. Provider data are synthetic; no paid requests."""
import asyncio
import json
from copy import deepcopy

import pytest

from app.validation.contracts import InvocationBudget
from app.validation.service import SettingValidationService
from tests.test_setting_validation_service import SyntheticData, SyntheticModel


def limits():
    return InvocationBudget(30, 100, 30, 5000, 1000, 100, 10, 10, 10)


class SavedStages:
    """Crash boundary used to inspect persisted semantic state, not a response cache."""
    def __init__(self):
        self.stages = {}
        self.checks = {}
        self.state = None
        self.crash_stage = None

    def stage(self, stage, ref):
        return deepcopy(self.stages.get((stage, ref)))

    async def save_stage(self, stage, ref, payload):
        self.stages[stage, ref] = deepcopy(payload)
        if self.crash_stage == stage:
            self.crash_stage = None
            raise asyncio.CancelledError()

    async def merge(self, questions):
        for q in questions:
            self.checks.setdefault(q.ref, {"revision": 1, "outcome": None})
        return self.checks

    async def save_state(self, state):
        self.state = deepcopy(state)

    def load_state(self):
        return deepcopy(self.state)

    async def decide(self, ref, decision, bundle):
        self.checks[ref].update(outcome=decision.outcome, decision=decision.model_dump(), bundle=bundle)

    async def revise(self, affected, pending, questions, reason):
        for ref in affected:
            self.checks[ref]["revision"] += 1
            self.checks[ref]["outcome"] = None
        return self.checks


def test_crash_after_coverage_restores_real_plan_and_completed_no_conflict():
    async def run():
        saved, data, model = SavedStages(), SyntheticData(), SyntheticModel()
        saved.crash_stage = "COVERAGE"
        with pytest.raises(asyncio.CancelledError):
            await SettingValidationService(data, model, limits(), checkpoint=saved).inspect()
        assert ("COVERAGE", "TSEG1") in saved.stages
        first = list(model.calls)
        result = await SettingValidationService(data, model, limits(), checkpoint=saved).inspect()
        assert result.status == "COMPLETED"
        assert [role for role, *_ in model.calls[len(first):]] == ["JUDGE"]
        calls = len(model.calls)
        again = await SettingValidationService(data, model, limits(), checkpoint=saved).inspect()
        assert again.status == "COMPLETED"
        assert len(model.calls) == calls
        assert saved.checks["Q1"]["outcome"] == "NO_CONFLICT"
    asyncio.run(run())


def test_successful_target_plan_survives_unreadable_second_book():
    async def run():
        class BooksModel(SyntheticModel):
            async def create_text_response(self, **kwargs):
                response = await super().create_text_response(**kwargs)
                payload = json.loads(kwargs["user_prompt"])
                if payload.get("raw_rules"):
                    body = json.loads(response.text)
                    body["rules"] = [{"entry_ref": row["ref"], "status": "CHECKED", "reason": "합성 책"}
                                     for row in payload["raw_rules"]]
                    from dataclasses import replace
                    return replace(response, text=json.dumps(body, ensure_ascii=False))
                return response
        saved, data, model = SavedStages(), SyntheticData(), BooksModel()
        for ref in ("R1", "R2"):
            object.__setattr__(data.documents[ref], "kind", "SETTING_BOOK")
        data.failed_ref = "R2"
        saved.book_segments = lambda ref: [{"ref": "TSEG1", "start": 0, "end": len(data.texts[ref]),
                                            "contextStart": 0, "contextEnd": len(data.texts[ref])}]
        result = await SettingValidationService(data, model, limits(), checkpoint=saved).inspect()
        assert result.status == "INCOMPLETE"
        assert ("PLANNER", "TSEG1") in saved.stages
        assert ("BOOK_SOURCE", "R1") in saved.stages
        calls = len(model.calls)
        data.failed_ref = None
        resumed = await SettingValidationService(data, model, limits(), checkpoint=saved).inspect()
        assert resumed.status == "COMPLETED"
        assert all(role not in {"PLANNER", "RELATED_SELECTION"} for role, *_ in model.calls[calls:])
    asyncio.run(run())


def test_leased_data_uses_trusted_baseline_revision_and_server_projections():
    from dataclasses import replace
    from uuid import uuid4

    import httpx

    from app.validation.data import ValidationDataClient
    from tests.test_typo_checker import OfflineRun

    fixture = OfflineRun("합성 원고")
    context = replace(fixture.context, baseline_revision=3, job_id=uuid4(), lease_token=uuid4(),
                      manifest_hash="c" * 64)
    def server(request):
        assert request.headers["X-Validation-Job-Id"] == str(context.job_id)
        assert request.headers["X-Worker-Lease-Token"] == str(context.lease_token)
        response = fixture.spring(request)
        data = response.json()["data"]
        data.update(baselineRevision=3, projectionVersion=1, clueVersion=1,
                    modelSourceCatalogue=[], modelClues=[])
        return httpx.Response(200, json={"data": data})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
            client = ValidationDataClient("https://spring.invalid", "synthetic", context, http)
            frozen = await client.read_input()
            assert frozen.model_source_catalogue == []
            assert (await client.read_source(frozen.target)).model_clues == []
    asyncio.run(run())


def test_typo_resume_preserves_classification_and_calls_only_failed_segment():
    import httpx

    from app.llm.openai_client import OpenAIResponsesClient
    from app.validation.data import ValidationDataClient
    from app.validation.typo import check_typos
    from tests.test_typo_checker import OfflineRun, batch, suggestion
    from tests.test_typo_checker import limits as typo_limits

    class TypoSaved(SavedStages):
        def bind_sources(self, frozen):
            pass
        async def typo(self, ref, payload):
            self.stages["TYPO_SEGMENT", ref] = deepcopy(payload)
    async def run():
        saved = TypoSaved()
        fixture = OfflineRun("됬다. 문장.", {"TSEG1": batch(suggestions=[suggestion(0, "됬다", "됐다")]),
                                           "TSEG2": httpx.Response(503)})
        async with (httpx.AsyncClient(transport=httpx.MockTransport(fixture.spring)) as spring,
                    httpx.AsyncClient(transport=httpx.MockTransport(fixture.provider)) as provider):
            data = ValidationDataClient("https://spring.invalid", "synthetic", fixture.context, spring)
            llm = OpenAIResponsesClient("synthetic", "gpt-6.1-terra", "https://provider.invalid/responses",
                                        http_client=provider)
            first = await check_typos(data, llm, budget=typo_limits(segment_chars=4), checkpoint=saved)
            assert first.status == "INCOMPLETE"
            count = len([path for path, _ in fixture.requests if path.endswith("responses")])
            fixture.replies["TSEG2"] = batch("TSEG2")
            second = await check_typos(data, llm, budget=typo_limits(segment_chars=4), checkpoint=saved)
            assert second.status == "COMPLETED" and second.suggestions[0].category == "SPELLING"
            assert len([path for path, _ in fixture.requests if path.endswith("responses")]) == count + 1
    asyncio.run(run())


def test_large_book_without_questions_is_not_copied_into_checkpoint_and_resumes_per_rule():
    from dataclasses import replace

    from app.llm.responses import LlmTextResponse
    from app.validation.contracts import split_segments
    async def run():
        data, saved = SyntheticData(), SavedStages()
        for ref in ("R1", "R2"):
            object.__setattr__(data.documents[ref], "kind", "SETTING_BOOK")
        data.texts["R1"] = "가" * 50000  # 150,000 UTF-8 bytes exceeds the publication ceiling.
        data.failed_ref = "R2"
        saved.book_segments = lambda ref: [{"ref": p.ref, "start": p.start, "end": p.end,
            "contextStart": p.context_start, "contextEnd": p.context_end}
            for p in split_segments(data.texts[ref], core_chars=1000, context_chars=10)]
        class Empty:
            def __init__(self):
                self.calls = []
            async def count_input_tokens(self, **kwargs):
                return 80
            async def create_text_response(self, **kwargs):
                p = json.loads(kwargs["user_prompt"])
                self.calls.append(p)
                if p["role"] == "RELATED_SELECTION":
                    body = {"mentions": []}
                else:
                    rules = [r["ref"] for r in p.get("raw_rules", [])] + [r["entry_ref"] for r in p.get("settings", [])]
                    body = {"questions": [], "segments": [{"segment_ref": p["segment"]["ref"], "status": "NO_CHECK", "reason": "합성"}],
                            "rules": [{"entry_ref": ref, "status": "CHECKED", "reason": "합성"} for ref in rules],
                            "clue_checks": []}
                return LlmTextResponse(text=json.dumps(body), input_token_count=80, output_token_count=7)
        model = Empty()
        budget = replace(limits(), model_calls=1000, lookups=2000, segment_chars=1000)
        first = await SettingValidationService(data, model, budget, checkpoint=saved).inspect()
        assert first.failure_code == "SOURCE_UNREADABLE"
        assert len([key for key in saved.stages if key[0] == "BOOK_COVERAGE"]) == 50
        assert saved.state is None
        calls = len(model.calls)
        data.failed_ref = None
        second = await SettingValidationService(data, model, budget, checkpoint=saved).inspect()
        assert second.status == "COMPLETED"
        assert len(model.calls) == calls + 1  # Only R2's single outstanding raw rule.
    asyncio.run(run())


def test_planned_operations_cover_saved_plans_decided_bundles_and_runtime():
    """GH344: a VECTOR saved by an earlier attempt keeps VECTOR offered on resume."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.validation.checkpoint import ValidationCheckpoint
    from app.validation.client import ExecutionScope

    scope = ExecutionScope(contractVersion=1, reportId=uuid4(), generation=1, inputDigest="a" * 64)
    keyword = {"ref": "L1", "kind": "CHANGES_EXCEPTIONS", "operation": "KEYWORD", "purpose": "변화", "query": "q"}
    vector = {**keyword, "operation": "VECTOR"}

    def operations(stages=(), checks=(), runtime=None):
        view = {"scope": scope.model_dump(mode="json"), "stages": list(stages), "checks": list(checks),
                "branches": {"SETTING": {"version": 1, "payload": runtime}} if runtime else {}}
        return ValidationCheckpoint(None, SimpleNamespace(scope=scope), view, None).planned_operations()

    def plan(intent, successful=True, branch="SETTING"):
        return {"stage": "PLANNER", "ref": "TSEG1", "successful": successful, "branch": branch,
                "payload": {"questions": [{"local_ref": "q", "lookup_intents": [intent]}]}}

    assert operations() == set()
    assert operations([plan(keyword)]) == {"KEYWORD"}
    assert "VECTOR" in operations([plan(vector)])
    # A failed stage or another branch is not a saved plan of this branch.
    assert operations([plan(vector, successful=False), plan(vector, branch="TYPO")]) == set()
    decided = {"localRef": "Q1", "outcome": "NO_CONFLICT",
               "decision": {"evidence_bundle": {"receipts": [{"ref": "L4", "intent": vector}]}}}
    assert "VECTOR" in operations(checks=[decided])
    assert "VECTOR" in operations(runtime={"phase": "JUDGING", "runtime": {"receipts": [{"intent": vector}]}})


def test_crash_after_subject_resolution_resumes_without_a_second_resolution_call():
    from tests.test_setting_validation_service import subject_data

    async def run():
        saved, data = SavedStages(), subject_data()
        mention = {"mention": "그 여자", "subject_ref": "S2", "reason": "앞 문장의 서윤을 가리킨다"}
        model = SyntheticModel(mentions=[mention])
        saved.crash_stage = "SUBJECT_RESOLUTION"
        with pytest.raises(asyncio.CancelledError):
            await SettingValidationService(data, model, limits(), checkpoint=saved).inspect()
        assert saved.stages["SUBJECT_RESOLUTION", "TSEG1"] == {"mentions": [mention]}
        assert [role for role, *_ in model.calls] == ["PLANNER", "RELATED_SELECTION"]
        first = len(model.calls)
        model.mentions = []  # a new call would drop 서윤; the stored stage must be used instead
        result = await SettingValidationService(data, model, limits(), checkpoint=saved).inspect()
        assert result.status == "COMPLETED"
        resumed = [(role, payload) for role, payload, _ in model.calls[first:]]
        assert [role for role, _ in resumed][:1] == ["COVERAGE"]
        assert not {"PLANNER", "RELATED_SELECTION"} & {role for role, _ in resumed}
        assert resumed[0][1]["rule_refs"] == ["B1", "B2", "B3"]
    asyncio.run(run())


def test_nothing_left_to_resolve_stores_an_empty_resolution_without_a_model_call():
    async def run():
        saved, model = SavedStages(), SyntheticModel()
        result = await SettingValidationService(SyntheticData(), model, limits(), checkpoint=saved).inspect()
        assert result.status == "COMPLETED"
        # 민수, the only subject, is already matched by name: no model output could change the set.
        assert saved.stages["SUBJECT_RESOLUTION", "TSEG1"] == {"mentions": []}
        assert "RELATED_SELECTION" not in {role for role, *_ in model.calls}
    asyncio.run(run())


def test_setting_grid_is_reloaded_once_after_the_first_publication_creates_it():
    from uuid import uuid4

    from app.validation.checkpoint import ValidationCheckpoint
    from app.validation.client import ExecutionScope, ValidationClaim

    grid = {"gridVersion": 2, "segmentSubjects": [{"segmentRef": "TSEG1", "subjectRefs": ["S1"]}]}

    class Client:
        loads = 0

        async def load_checkpoint(self, claim, gate=None):
            self.loads += 1
            return {"requiredGrid": {"SETTING": grid}}

    async def run():
        claim = ValidationClaim(uuid4(), uuid4(), ExecutionScope(contractVersion=1, reportId=uuid4(),
                                                                generation=1, inputDigest="a" * 64), {})
        view = {"scope": claim.scope.model_dump(mode="json"), "stages": [], "checks": [], "branches": {},
                "requiredGrid": None}
        client = Client()
        checkpoint = ValidationCheckpoint(client, claim, view, None)
        assert await checkpoint.setting_grid() == grid
        assert await checkpoint.setting_grid() == grid
        assert client.loads == 1
        known = ValidationCheckpoint(client, claim, {**view, "requiredGrid": {"SETTING": {"gridVersion": 1}}}, None)
        assert await known.setting_grid() == {"gridVersion": 1} and client.loads == 1
    asyncio.run(run())


# --- GH349 Amendment 2: the published/stored bundle carries entry/subject refs only -------------------------


class RecordingJava:
    """Java publication boundary for the real ValidationCheckpoint: records every leased command."""

    def __init__(self):
        self.calls = []

    async def leased(self, claim, operation, gate=None, **body):
        self.calls.append((operation, deepcopy(body)))
        if operation == "checkpoint":
            return {"version": body["expectedVersion"] + 1}
        if operation == "plans" and body["stage"] == "MERGED_PLAN":
            return {"checks": [{"localRef": row["localRef"], "checkId": f"check-{row['localRef']}", "revision": 1,
                                "checkInputDigest": "d" * 64, "outcome": None, "decision": None,
                                "state": row["state"]} for row in body["payload"]["questions"]]}
        return {}

    def published(self, operation):
        return [body for name, body in self.calls if name == operation]


def execution_claim():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.validation.client import ExecutionScope

    return SimpleNamespace(scope=ExecutionScope(contractVersion=1, reportId=uuid4(), generation=1,
                                                inputDigest="a" * 64))


def real_checkpoint(java, claim, **view):
    from app.validation.checkpoint import ValidationCheckpoint

    view = {"scope": claim.scope.model_dump(mode="json"), "stages": [], "checks": [], "branches": {}} | view
    return ValidationCheckpoint(java, claim, view, None)


def test_published_decision_carries_setting_refs_only_and_fits_the_publication_cap():
    from pathlib import Path

    from app.validation.projection import compact_setting_row
    from app.validation.service import _settings
    from tests.test_setting_validation_service import heavy_data, payloads

    cap = json.loads((Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json")
                     .read_text())["execution"]["maxPublicationBytes"]
    data = heavy_data()
    data.entries[0]["state"]["historyFacts"] = [  # production's largest entry held 209 KB of history; the cap is now 1 MiB
        {"factType": "STATUS", "factKey": "손", "factValue": f"양손 사용 {n}", "supportingSource": {"quote": "옛 원문" * 20}}
        for n in range(4000)]
    full = _settings(data.entries)
    assert len(json.dumps(full[0], ensure_ascii=False).encode()) > cap  # a full-row bundle could never be published

    java, model = RecordingJava(), SyntheticModel()
    checkpoint = real_checkpoint(java, execution_claim())
    result = asyncio.run(SettingValidationService(data, model, limits(), checkpoint=checkpoint).inspect())
    assert result.status == "COMPLETED"
    (judge,) = payloads(model, "JUDGE")
    assert judge["settings"] == [compact_setting_row(full[0])] and "historyFacts" not in json.dumps(judge)
    (unit,) = java.published("units")
    bundle = unit["payload"]["evidence_bundle"]
    assert bundle["settings"] == [{"entry_ref": "B1", "subject_ref": "S1"}]
    assert bundle["input_digest"] == judge["input_digest"]
    assert checkpoint.checks["Q1"]["bundle"] == bundle
    # Every command of the run, not only the decision, stays under the Java ceiling.
    assert max(len(json.dumps(body, ensure_ascii=False).encode()) for _, body in java.calls) < cap


def test_resume_reads_only_the_refs_of_a_bundle_stored_with_full_rows():
    """A check decided by a Worker before Amendment 2 holds full rows: resume neither re-judges nor republishes it."""
    from app.validation.service import _settings

    data, java, claim = SyntheticData(), RecordingJava(), execution_claim()
    first = real_checkpoint(java, claim)
    assert asyncio.run(SettingValidationService(data, SyntheticModel(), limits(), checkpoint=first).inspect()).status \
        == "COMPLETED"
    (unit,) = java.published("units")
    old_bundle = unit["payload"]["evidence_bundle"] | {"settings": _settings(data.entries)}
    old_check = {key: value for key, value in first.checks["Q1"].items() if key != "bundle"}
    old_check["decision"] = {**old_check["decision"], "evidence_bundle": old_bundle}
    stages = [{"stage": body["stage"], "ref": body["ref"], "payload": body["payload"], "successful": True,
               "branch": "SETTING"} for body in java.published("plans")]

    resumed_java, model = RecordingJava(), SyntheticModel()
    resumed = real_checkpoint(resumed_java, claim, stages=stages, checks=[old_check],
                              branches={"SETTING": {"version": first.version, "payload": first.saved}})
    result = asyncio.run(SettingValidationService(data, model, limits(), checkpoint=resumed).inspect())
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "NO_CONFLICT"
    assert model.calls == [] and resumed_java.calls == []
    assert result.evidence_bundles["Q1"] == old_bundle | {"settings": [{"entry_ref": "B1", "subject_ref": "S1"}]}
