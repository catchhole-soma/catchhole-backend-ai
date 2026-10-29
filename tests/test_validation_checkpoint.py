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
                    body = {"entries": [{"entry_ref": r["entry_ref"], "selected": False, "reason": "합성"} for r in p["catalogue"]]}
                else:
                    body = {"questions": [], "segments": [{"segment_ref": p["segment"]["ref"], "status": "NO_CHECK", "reason": "합성"}],
                            "rules": [{"entry_ref": r["ref"], "status": "CHECKED", "reason": "합성"} for r in p.get("raw_rules", [])],
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
