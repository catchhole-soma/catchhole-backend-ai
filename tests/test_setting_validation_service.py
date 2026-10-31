"""Synthetic model outputs exercise orchestration, never Terra semantic quality."""

import asyncio
import json
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from app.llm.responses import LlmTextResponse
from app.validation.contracts import IncompleteInspection, InvocationBudget
from app.validation.data import FrozenInput, SourceDocument, SourceRead, ValidationContext

TARGET = "민수는 양손으로 검을 쥐었다."


def planned_question():
    return {
        "local_ref": "hand",
        "target_spans": [{"source_ref": "T1", "start": 0, "end": 16, "quote": TARGET}],
        "subject_refs": ["S1"],
        "subject_mentions": ["민수"],
        "property_or_rule": "손 사용",
        "time_context": "현재",
        "conditions": ["검을 쥠"],
        "assertion_mode": "NARRATION",
        "check_intent": "양손 사용 가능 여부",
        "reason": "행동 전제 확인",
        "lookup_intents": [],
    }


def decision(payload, outcome="NO_CONFLICT", basis="COMPATIBLE", evidence_refs=None):
    receipts = [r["ref"] for r in payload["receipts"]]
    refs = evidence_refs or ["T1", "B1"] + [row["ref"] for row in payload["evidence"]]
    assessment = {
        "reason": "대상·시점·조건과 확보 자료를 확인했다",
        "evidence_refs": refs,
        "receipt_refs": receipts,
    }
    return {
        "action": "DECIDED",
        "outcome": outcome,
        "basis": basis,
        "reason": "합성 판정",
        "impact": None if outcome == "NO_CONFLICT" else "CAUTION",
        "impact_reason": None if outcome == "NO_CONFLICT" else "설정 이해에 영향",
        "target_spans": payload["question"]["target_spans"],
        "evidence_refs": refs,
        "completion": {
            key: assessment
            for key in (
                "comparison_context",
                "changes_exceptions",
                "no_pending_clues",
                "required_reads",
            )
        },
        "follow_ups": [],
        "affected_question_refs": [],
        "disagreements": [],
    }


class SyntheticData:
    def __init__(self):
        self.context = ValidationContext(
            1, uuid4(), uuid4(), "b" * 64, sha256(TARGET.encode()).hexdigest()
        )
        self.texts = {
            "T1": TARGET,
            "R1": "민수는 왼손을 잃었다.",
            "R2": "민수는 의수를 얻었고 그 의수로 검을 쥘 수 있었다.",
        }
        self.documents = {
            ref: SourceDocument(
                ref,
                "EPISODE",
                sha256(text.encode()).hexdigest(),
                "v1",
                {
                    "episodeId": str(uuid4()),
                    "episodeNo": index + 1,
                    "contentS3Key": "synthetic/" + ref,
                },
            )
            for index, (ref, text) in enumerate(self.texts.items())
        }
        self.documents["T1"].metadata["episodeNo"] = 101
        self.entries = [
            {
                "entryRef": "B1",
                "domain": "characters",
                "subjectRef": "character:existing",
                "state": {
                    "name": "민수",
                    "aliases": [],
                    "slots": {"STATUS:손": {"factValue": "왼손 상실"}},
                },
            }
        ]
        self.failed_ref = None
        self.invalidated = False

    async def read_input(self):
        return FrozenInput(
            self.context,
            self.documents["T1"],
            tuple(self.documents[ref] for ref in ("R1", "R2")),
            "c" * 64,
            len(self.entries),
        )

    async def read_entries_page(self, offset, limit):
        return (
            self.entries[offset : offset + limit],
            min(len(self.entries), offset + limit),
            offset + limit < len(self.entries),
        )

    async def read_source(self, source, start=None, end=None):
        if source.ref == self.failed_ref:
            raise IncompleteInspection("SOURCE_UNREADABLE")
        text = self.texts[source.ref]
        start = start or 0
        end = len(text) if end is None else end
        return SourceRead(source, text[start:end], start, end, len(text))

    async def validate(self):
        if self.invalidated:
            raise IncompleteInspection("INPUT_CHANGED")


class SyntheticModel:
    def __init__(self, judge=None, *, dropped=False, planner_question=False, mentions=()):
        self.calls = []
        self.judge = judge or decision
        self.dropped = dropped
        self.planner_question = planner_question
        self.mentions = list(mentions)

    async def count_input_tokens(self, **kwargs):
        return 80

    async def create_text_response(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        role = payload["role"]
        self.calls.append((role, payload, kwargs))
        if role == "RELATED_SELECTION":
            # GH349 SUBJECT_RESOLUTION: only the scripted non-literal mentions.
            result = {"mentions": self.mentions}
        elif role in {"PLANNER", "COVERAGE"}:
            questions = [planned_question()] if role == "COVERAGE" or self.planner_question else []
            result = {
                "questions": questions,
                "segments": []
                if self.dropped
                else [
                    {
                        "segment_ref": payload["segment"]["ref"],
                        "status": "PLANNED" if questions else "NO_CHECK",
                        "reason": "합성 처리",
                    }
                ],
                "rules": [
                    {"entry_ref": row["entry_ref"], "status": "CHECKED", "reason": "신체 제약 확인"}
                    for row in payload.get("settings", [])
                ],
            }
        else:
            result = self.judge(payload)
        return LlmTextResponse(
            text=json.dumps(result, ensure_ascii=False), input_token_count=80, output_token_count=30
        )


def run(data=None, model=None, **budget_values):
    from app.validation.service import SettingValidationService

    limits = {
        "model_calls": 30,
        "lookups": 50,
        "seconds": 30,
        "input_tokens": 5000,
        "output_tokens": 1000,
        "segment_chars": 100,
        "context_chars": 10,
        "catalogue_page_size": 10,
        "search_limit": 10,
    }
    limits.update(budget_values)
    return asyncio.run(
        SettingValidationService(
            data or SyntheticData(), model or SyntheticModel(), InvocationBudget(**limits)
        ).inspect()
    )


def test_independent_coverage_restores_hand_question_and_reads_unrecorded_prosthetic():
    def judge(payload):
        if not payload["evidence"]:
            value = decision(payload)
            value.update(
                action="FOLLOW_UP",
                outcome=None,
                basis=None,
                completion=None,
                follow_ups=[
                    {
                        "ref": "prosthetic",
                        "kind": "CHANGES_EXCEPTIONS",
                        "operation": "KEYWORD",
                        "purpose": "의수 획득과 사용 조건·시점 확인",
                        "query": "민수 의수",
                    }
                ],
            )
            return value
        return decision(payload)

    model = SyntheticModel(judge)
    data = SyntheticData()
    # An unmentioned second subject keeps the independent SUBJECT_RESOLUTION call (GH349).
    data.entries.append({"entryRef": "B2", "domain": "characters", "subjectRef": "character:other",
                         "state": {"name": "서윤", "aliases": [], "slots": {}}})
    result = run(data, model)
    assert result.status == "COMPLETED"
    assert len(result.questions) == 1
    assert result.questions[0].original_refs == ("COVERAGE:TSEG1:hand",)
    assert result.decisions["Q1"].outcome == "NO_CONFLICT"
    assert any("의수" in row["quote"] for row in result.evidence_bundles["Q1"]["evidence"])
    assert any(
        receipt.intent.operation == "KEYWORD" and receipt.evidence_refs
        for receipt in result.receipts
    )
    related = next(payload for role, payload, kwargs in model.calls if role == "RELATED_SELECTION")
    coverage = next(payload for role, payload, kwargs in model.calls if role == "COVERAGE")
    assert "questions" not in related and "questions" not in coverage
    assert {kwargs["model"] for role, payload, kwargs in model.calls} == {"gpt-6.1-terra"}
    assert [kwargs["call_options"].reasoning_effort for role, payload, kwargs in model.calls][
        :3
    ] == ["medium", "high", "high"]


@pytest.mark.parametrize(
    "outcome,basis",
    [
        ("CONFLICT", "DIRECT_CONTRADICTION"),
        ("SUSPECTED_CONFLICT", "CHANGE_NOT_FOUND"),
        ("NEEDS_CONTEXT", "AMBIGUOUS_MEANING"),
    ],
)
def test_semantic_outcomes_remain_distinct(outcome, basis):
    def judge(payload):
        result = decision(payload, outcome, basis)
        if outcome == "SUSPECTED_CONFLICT" and len(payload["receipts"]) == 2:
            result.update(
                action="FOLLOW_UP",
                outcome=None,
                basis=None,
                completion=None,
                follow_ups=[
                    {
                        "ref": "changes",
                        "kind": "CHANGES_EXCEPTIONS",
                        "operation": "KEYWORD",
                        "purpose": "정상 변화 검색",
                        "query": "회복",
                    }
                ],
            )
        return result

    result = run(model=SyntheticModel(judge))
    assert result.status == "COMPLETED"
    assert result.decisions["Q1"].outcome == outcome


def test_required_read_failure_dropped_segment_and_budget_never_become_ambiguity():
    def judge(payload):
        value = decision(payload)
        value.update(
            action="FOLLOW_UP",
            outcome=None,
            basis=None,
            completion=None,
            follow_ups=[
                {
                    "ref": "direct",
                    "kind": "COMPARISON_CONTEXT",
                    "operation": "DIRECT",
                    "purpose": "필수 근거 읽기",
                    "source_ref": "R1",
                    "start": 0,
                    "end": 5,
                }
            ],
        )
        return value

    data = SyntheticData()
    data.failed_ref = "R1"
    failed = run(data, SyntheticModel(judge))
    assert failed.receipts[-1].status == "FAILED"
    assert failed.receipts[-1].failure_code == "SOURCE_UNREADABLE"
    for result in (failed, run(model=SyntheticModel(dropped=True)), run(model_calls=1)):
        assert result.status == "INCOMPLETE"
        assert not result.decisions


def test_db_only_completion_skips_unnecessary_search_but_rejects_fake_receipts():
    result = run()
    assert result.status == "COMPLETED"
    assert all(
        receipt.intent.operation in {"BASELINE", "TARGET_CONTEXT"} for receipt in result.receipts
    )

    def forged(payload):
        result = decision(payload)
        result["completion"]["required_reads"]["receipt_refs"] = ["fake"]
        return result

    assert run(model=SyntheticModel(forged)).failure_code == "INVALID_DECISION"


def test_db_raw_disagreement_cannot_infer_correction_from_human_or_protection():
    def judge(payload):
        if not payload["evidence"]:
            value = decision(payload)
            value.update(
                action="FOLLOW_UP",
                outcome=None,
                basis=None,
                completion=None,
                follow_ups=[
                    {
                        "ref": "prior",
                        "kind": "COMPARISON_CONTEXT",
                        "operation": "DIRECT",
                        "purpose": "DB 출처 확인",
                        "source_ref": "R1",
                        "start": 0,
                        "end": 12,
                    }
                ],
            )
            return value
        value = decision(payload, "CONFLICT", "DIRECT_CONTRADICTION")
        value["disagreements"] = [
            {
                "fact_ref": "B1",
                "evidence_ref": "E1",
                "reason": "DB와 원문이 다름",
                "correction_ref": None,
            }
        ]
        return value

    data = SyntheticData()
    data.entries[0]["state"]["reviewSource"] = "HUMAN"
    data.entries[0]["state"]["currentValueProtected"] = True
    result = run(data, SyntheticModel(judge))
    assert result.status == "COMPLETED"
    assert result.decisions["Q1"].outcome == "NEEDS_CONTEXT"
    assert {"B1", "E1"} <= set(result.decisions["Q1"].evidence_refs)


def test_target_only_contradiction_and_source_invalidation_are_rejected():
    assert (
        run(
            model=SyntheticModel(
                lambda payload: decision(payload, "CONFLICT", "DIRECT_CONTRADICTION", ["T1"])
            )
        ).status
        == "INCOMPLETE"
    )
    data = SyntheticData()
    data.invalidated = True
    assert run(data).failure_code == "INPUT_CHANGED"


def test_mechanical_clues_preserve_korean_quantity_date_and_exact_locations():
    from app.validation.clues import collect_clues

    text = "민수는 세 명과 10월 3일 월요일에 만났다. 이틀 뒤 민수가 왔다."
    clues = collect_clues(text, names=["민수"])
    assert {"세 명", "10월", "3일", "월요일", "이틀"} <= {clue["span"]["quote"] for clue in clues}
    assert len([clue for clue in clues if clue["kind"] == "REPEATED_NAME"]) == 2
    assert all(
        text[row["span"]["start"] : row["span"]["end"]] == row["span"]["quote"] for row in clues
    )


@pytest.mark.parametrize("applicable", [True, False])
def test_explicit_author_correction_is_used_only_in_its_exact_scope(applicable):
    data = SyntheticData()
    data.entries[0]["state"]["explicitAuthorCorrection"] = {
        "kind": "AUTHOR_CORRECTION",
        "ref": "AC1",
        "subject_ref": "S1",
        "property_or_rule": "손 사용",
        "time_context": "현재" if applicable else "회상",
        "conditions": ["검을 쥠"],
    }

    def judge(payload):
        if not payload["evidence"]:
            value = decision(payload)
            value.update(
                action="FOLLOW_UP",
                outcome=None,
                basis=None,
                completion=None,
                follow_ups=[
                    {
                        "ref": "prior",
                        "kind": "COMPARISON_CONTEXT",
                        "operation": "DIRECT",
                        "purpose": "기존 근거 확인",
                        "source_ref": "R1",
                        "start": 0,
                        "end": 12,
                    }
                ],
            )
            return value
        value = decision(payload)
        value["disagreements"] = [
            {
                "fact_ref": "B1",
                "evidence_ref": "E1",
                "reason": "정정 이전 값",
                "correction_ref": "AC1",
            }
        ]
        return value

    result = run(data, SyntheticModel(judge))
    assert result.status == "COMPLETED"
    assert result.decisions["Q1"].outcome == ("NO_CONFLICT" if applicable else "NEEDS_CONTEXT")


def test_late_source_change_discards_stale_publishable_decisions():
    data = SyntheticData()

    async def changed():
        raise IncompleteInspection("SOURCE_PURGED")

    data.validate = changed
    result = run(data)
    assert result.status == "INCOMPLETE" and result.failure_code == "SOURCE_PURGED"
    assert not result.decisions and not result.questions


def test_legitimate_zero_questions_has_complete_coverage():
    class NoQuestions(SyntheticModel):
        async def create_text_response(self, **kwargs):
            response = await super().create_text_response(**kwargs)
            if json.loads(kwargs["user_prompt"])["role"] == "COVERAGE":
                result = json.loads(response.text)
                result["questions"] = []
                result["segments"][0]["status"] = "NO_CHECK"
                return LlmTextResponse(text=json.dumps(result))
            return response

    result = run(model=NoQuestions())
    assert result.status == "COMPLETED" and not result.questions and not result.decisions
    assert all(
        row["status"] == "NO_CHECK"
        for row in result.coverage
        if row["role"] in {"PLANNER", "COVERAGE"}
    )


def test_new_clue_reopens_already_decided_question_with_new_digest():
    class TwoQuestions(SyntheticModel):
        async def create_text_response(self, **kwargs):
            response = await super().create_text_response(**kwargs)
            if json.loads(kwargs["user_prompt"])["role"] == "COVERAGE":
                value = json.loads(response.text)
                second = dict(
                    value["questions"][0], local_ref="timing", property_or_rule="사용 시점"
                )
                value["questions"].append(second)
                return LlmTextResponse(text=json.dumps(value))
            return response

    seen = []

    def judge(payload):
        seen.append((payload["question_ref"], payload["check_revision"], payload["input_digest"]))
        result = decision(payload)
        if payload["question_ref"] == "Q2" and payload["check_revision"] == 1:
            result.update(
                action="FOLLOW_UP",
                outcome=None,
                basis=None,
                completion=None,
                affected_question_refs=["Q1"],
                follow_ups=[
                    {
                        "ref": "clue",
                        "kind": "NEW_CLUE",
                        "operation": "KEYWORD",
                        "purpose": "사용 시점의 새 단서 확인",
                        "query": "의수",
                    }
                ],
            )
        return result

    result = run(model=TwoQuestions(judge))
    assert result.status == "COMPLETED" and set(result.decisions) == {"Q1", "Q2"}
    q1 = [(revision, digest) for ref, revision, digest in seen if ref == "Q1"]
    assert len(q1) == 2 and q1[0][0] == 1 and q1[1][0] == 2 and q1[0][1] != q1[1][1]


def test_unindexed_raw_match_is_ranked_with_indexed_hits_before_limit():
    from types import SimpleNamespace
    from uuid import UUID

    from app.validation.contracts import BudgetState, LookupIntent
    from app.validation.corpus import ValidationCorpus

    data = SyntheticData()

    class Indexed:
        async def search(self, frozen, intent, embedding, limit):
            text = data.texts["R1"]
            hit = SimpleNamespace(
                episode_id=UUID(data.documents["R1"].metadata["episodeId"]),
                start_offset=0,
                end_offset=len(text),
                text=text,
            )
            return SimpleNamespace(chunks=[hit], limited=True), {"R1"}

    async def inspect():
        limits = InvocationBudget(10, 20, 10, 5000, 1000, 100, 10, 10, 1)
        corpus = ValidationCorpus(
            data, await data.read_input(), BudgetState(limits), search=Indexed()
        )
        refs, _limited = await corpus.lookup(
            LookupIntent(
                ref="find",
                kind="CHANGES_EXCEPTIONS",
                operation="KEYWORD",
                purpose="의수",
                query="민수 의수",
            )
        )
        assert corpus.evidence[refs[0]]["source_ref"] == "R2"

    asyncio.run(inspect())


def test_target_context_followup_delivers_requested_far_context():
    from dataclasses import replace

    data = SyntheticData()
    value = TARGET + " " * 100 + "어릴 적이었다."
    data.texts["T1"] = value
    digest = sha256(value.encode()).hexdigest()
    data.context = replace(data.context, target_hash=digest)
    data.documents["T1"] = replace(data.documents["T1"], content_hash=digest)

    def judge(payload):
        contexts = payload.get("additional_target_context", [])
        if not contexts:
            result = decision(payload)
            result.update(
                action="FOLLOW_UP",
                outcome=None,
                basis=None,
                completion=None,
                follow_ups=[
                    {
                        "ref": "far",
                        "kind": "COMPARISON_CONTEXT",
                        "operation": "TARGET_CONTEXT",
                        "purpose": "회상 시점 확인",
                        "source_ref": "T1",
                        "start": len(value) - 8,
                        "end": len(value),
                    }
                ],
            )
            return result
        assert contexts[0]["quote"] == "어릴 적이었다."
        return decision(payload)

    result = run(data, SyntheticModel(judge), segment_chars=500)
    assert result.status == "COMPLETED"


def suspected(payload, *, cite_search=True):
    """SUSPECTED_CONFLICT; without ``cite_search`` changes_exceptions cites only the comparison reads."""
    value = decision(payload, "SUSPECTED_CONFLICT", "CHANGE_NOT_FOUND")
    if not cite_search:
        value["completion"]["changes_exceptions"] = {
            "reason": "변화·예외 기록을 찾지 못했다",
            "evidence_refs": ["T1", "B1"],
            "receipt_refs": [row["ref"] for row in payload["receipts"]
                             if row["intent"]["kind"] == "COMPARISON_CONTEXT"],
        }
    return value


class DurableCheckpoint:
    """What the Backend keeps across attempts: stages, check revisions/obligations and the runtime state."""

    def __init__(self):
        self.stages, self.checks, self.state = {}, {}, None
        self.revised, self.decided = [], []
        self.crash_after_revise = False

    def stage(self, stage, ref):
        return deepcopy(self.stages.get((stage, ref)))

    async def save_stage(self, stage, ref, payload):
        self.stages[stage, ref] = deepcopy(payload)

    async def merge(self, questions):
        for q in questions:
            self.checks.setdefault(q.ref, {"revision": 1, "outcome": None, "state": {
                "lookup_obligations": [intent.model_dump() for intent in q.draft.lookup_intents]}})
        return deepcopy(self.checks)

    def load_state(self):
        return deepcopy(self.state)

    async def save_state(self, state):
        self.state = deepcopy(state)

    async def decide(self, ref, value, bundle):
        self.decided.append((ref, value, bundle))
        self.checks[ref].update(outcome=value.outcome, decision=value.model_dump(), bundle=bundle)

    async def revise(self, affected, pending, questions, reason):
        added = {ref: [intent.model_dump() for intent in pending[ref]] for ref in affected}
        self.revised.append((sorted(affected), reason, added))
        for ref in affected:
            check = self.checks[ref]
            check.update(revision=check["revision"] + 1, outcome=None)
            check["state"]["lookup_obligations"] += added[ref]
        if self.crash_after_revise:
            self.crash_after_revise = False
            raise asyncio.CancelledError()  # the revision is durable, the runtime write after it is lost
        return deepcopy(self.checks)


def inspect_with(data, model, checkpoint):
    from app.validation.service import SettingValidationService

    limits = InvocationBudget(30, 100, 30, 5000, 1000, 100, 10, 10, 10)
    return asyncio.run(SettingValidationService(data, model, limits, checkpoint=checkpoint).inspect())


def judge_calls(model):
    return [payload for role, payload, kwargs in model.calls if role == "JUDGE"]


def worker_searches(receipts):
    from app.validation.service import WORKER_CHANGE_SEARCH_PURPOSE

    return [row for row in receipts if row.intent.purpose == WORKER_CHANGE_SEARCH_PURPOSE]


def assert_backend_publishable(value, bundle, obligations):
    """The ValidationDecisionValidator rules this decision must pass unchanged (Backend is not modified)."""
    receipts = {row["ref"]: row for row in bundle["receipts"]}
    assert all(row["status"] == "SUCCEEDED" and row["failure_code"] is None for row in receipts.values())
    assert all(any(row["intent"] == obligation for row in receipts.values()) for obligation in obligations)
    facts = {row["entry_ref"] for row in bundle["settings"]}
    outside = {row["ref"] for row in bundle["evidence"] if row["source_ref"] != "T1"}
    known = {"T1"} | facts | {row["ref"] for row in bundle["evidence"]}
    completion = value.completion
    for name in ("comparison_context", "changes_exceptions", "no_pending_clues", "required_reads"):
        assessment = getattr(completion, name)
        assert assessment.evidence_refs and set(assessment.evidence_refs) <= known
        assert assessment.receipt_refs and set(assessment.receipt_refs) <= set(receipts)
    assert set(completion.required_reads.receipt_refs) == set(receipts)
    assert any(receipts[ref]["intent"]["kind"] == "CHANGES_EXCEPTIONS"
               and receipts[ref]["intent"]["operation"] in {"KEYWORD", "VECTOR"}
               for ref in completion.changes_exceptions.receipt_refs)
    assert set(value.evidence_refs) & (facts | outside)


def test_suspected_conflict_without_change_search_gets_one_worker_keyword_search_and_is_asked_again():
    from app.validation.service import WORKER_CHANGE_SEARCH_PURPOSE

    data, model, checkpoint = SyntheticData(), SyntheticModel(suspected), DurableCheckpoint()
    result = inspect_with(data, model, checkpoint)
    assert (result.status, result.failure_code) == ("COMPLETED", None)
    assert result.decisions["Q1"].outcome == "SUSPECTED_CONFLICT"
    assert result.normalizations["CHANGE_SEARCH_INSERTED"] == 1
    assert "CHANGE_SEARCH_RECEIPT_FILLED" not in result.normalizations  # the second answer cited it
    inserted = {"ref": "Q1-CHANGES_EXCEPTIONS", "kind": "CHANGES_EXCEPTIONS", "operation": "KEYWORD",
                "purpose": WORKER_CHANGE_SEARCH_PURPOSE, "query": "민수 손 사용", "source_ref": None,
                "start": None, "end": None, "evidence_refs": []}
    assert checkpoint.revised == [(["Q1"], "NEW_CLUE", {"Q1": [inserted]})]
    [search] = worker_searches(result.receipts)
    assert search.status == "SUCCEEDED" and search.intent.model_dump() == inserted and search.evidence_refs
    first, second = judge_calls(model)
    assert (first["check_revision"], second["check_revision"]) == (1, 2)
    assert search.ref not in {row["ref"] for row in first["receipts"]}
    assert search.ref in {row["ref"] for row in second["receipts"]}
    assert set(search.evidence_refs) <= {row["ref"] for row in second["evidence"]}
    assert search.ref in result.decisions["Q1"].completion.changes_exceptions.receipt_refs
    [(ref, published, bundle)] = checkpoint.decided
    # The published bundle is the second JUDGE input (as JSON), carrying the inserted receipt, with the
    # model's setting rows reduced to their refs (GH349 Amendment 2).
    refs = [{"entry_ref": row["entry_ref"], "subject_ref": row["subject_ref"]} for row in second["settings"]]
    assert ref == "Q1" and json.loads(json.dumps(bundle)) == {
        key: item for key, item in second.items() if key != "role"} | {"settings": refs}
    assert_backend_publishable(published, bundle, checkpoint.checks["Q1"]["state"]["lookup_obligations"])


def test_repeated_suspected_conflict_without_citation_is_filled_with_the_real_search_receipt():
    model, checkpoint = SyntheticModel(lambda payload: suspected(payload, cite_search=False)), DurableCheckpoint()
    result = inspect_with(SyntheticData(), model, checkpoint)
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "SUSPECTED_CONFLICT"
    assert result.normalizations["CHANGE_SEARCH_INSERTED"] == 1
    assert result.normalizations["CHANGE_SEARCH_RECEIPT_FILLED"] == 1
    assert len(judge_calls(model)) == 2 and len(checkpoint.revised) == 1
    [search] = worker_searches(result.receipts)
    changes = result.decisions["Q1"].completion.changes_exceptions
    assert changes.receipt_refs == ["L1", "L2", search.ref]  # the model's citations stay, the search is added
    assert changes.evidence_refs == ["T1", "B1", *search.evidence_refs]
    [(_ref, published, bundle)] = checkpoint.decided
    assert_backend_publishable(published, bundle, checkpoint.checks["Q1"]["state"]["lookup_obligations"])


class PlannedChangeSearch(SyntheticModel):
    async def create_text_response(self, **kwargs):
        response = await super().create_text_response(**kwargs)
        if json.loads(kwargs["user_prompt"])["role"] == "COVERAGE":
            value = json.loads(response.text)
            value["questions"][0]["lookup_intents"] = [{
                "ref": "change", "kind": "CHANGES_EXCEPTIONS", "operation": "KEYWORD",
                "purpose": "손 상태 변화 확인", "query": "민수 의수"}]
            return LlmTextResponse(text=json.dumps(value, ensure_ascii=False))
        return response


def follow_up_then_suspected(payload):
    if any(row["intent"]["operation"] == "KEYWORD" for row in payload["receipts"]):
        return suspected(payload, cite_search=False)
    value = decision(payload)
    value.update(action="FOLLOW_UP", outcome=None, basis=None, completion=None, follow_ups=[{
        "ref": "change", "kind": "CHANGES_EXCEPTIONS", "operation": "KEYWORD",
        "purpose": "손 상태 변화 확인", "query": "민수 의수"}])
    return value


@pytest.mark.parametrize("model_type,judge,judge_count", [
    (PlannedChangeSearch, lambda payload: suspected(payload, cite_search=False), 1),
    (SyntheticModel, follow_up_then_suspected, 2),
])
def test_model_issued_change_search_is_never_duplicated(model_type, judge, judge_count):
    model = model_type(judge)
    result = run(model=model)
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "SUSPECTED_CONFLICT"
    assert "CHANGE_SEARCH_INSERTED" not in result.normalizations and not worker_searches(result.receipts)
    assert len(judge_calls(model)) == judge_count
    # The model's own successful search is the cited one; nothing is invented.
    [search] = [row for row in result.receipts if row.intent.operation == "KEYWORD"]
    assert search.ref in result.decisions["Q1"].completion.changes_exceptions.receipt_refs
    assert result.normalizations["CHANGE_SEARCH_RECEIPT_FILLED"] == 1


def test_failed_worker_change_search_is_issued_once_and_resumes_without_a_second_insertion():
    data, checkpoint = SyntheticData(), DurableCheckpoint()
    data.failed_ref = "R1"  # only the KEYWORD search reads R1
    model = SyntheticModel(suspected)
    failed = inspect_with(data, model, checkpoint)
    assert (failed.status, failed.failure_code, failed.failure_stage) == (
        "INCOMPLETE", "SOURCE_UNREADABLE", "LOOKUP:Q1:KEYWORD")
    assert failed.normalizations["CHANGE_SEARCH_INSERTED"] == 1 and len(judge_calls(model)) == 1
    [search] = worker_searches(failed.receipts)
    assert search.status == "FAILED" and search.failure_code == "SOURCE_UNREADABLE"
    # Same input again: the durable obligation is retried, never issued a second time.
    again = inspect_with(data, model, checkpoint)
    assert (again.status, again.failure_code) == ("INCOMPLETE", "SOURCE_UNREADABLE")
    assert "CHANGE_SEARCH_INSERTED" not in again.normalizations and len(judge_calls(model)) == 1
    data.failed_ref = None
    resumed = inspect_with(data, model, checkpoint)
    assert resumed.status == "COMPLETED" and resumed.decisions["Q1"].outcome == "SUSPECTED_CONFLICT"
    assert "CHANGE_SEARCH_INSERTED" not in resumed.normalizations
    assert len(checkpoint.revised) == 1 and len(judge_calls(model)) == 2
    assert [row.status for row in worker_searches(resumed.receipts)] == ["FAILED", "FAILED", "SUCCEEDED"]


def test_crash_after_the_durable_revision_resumes_the_inserted_obligation_once():
    data, checkpoint = SyntheticData(), DurableCheckpoint()
    checkpoint.crash_after_revise = True
    model = SyntheticModel(suspected)
    with pytest.raises(asyncio.CancelledError):
        inspect_with(data, model, checkpoint)
    assert checkpoint.checks["Q1"]["revision"] == 2
    assert checkpoint.state["runtime"]["revisions"]["Q1"] == 1  # the runtime write after revise was lost
    resumed = inspect_with(data, model, checkpoint)
    assert resumed.status == "COMPLETED" and resumed.decisions["Q1"].outcome == "SUSPECTED_CONFLICT"
    assert "CHANGE_SEARCH_INSERTED" not in resumed.normalizations
    assert len(checkpoint.revised) == 1 and len(judge_calls(model)) == 2
    assert [row.status for row in worker_searches(resumed.receipts)] == ["SUCCEEDED"]
    assert judge_calls(model)[-1]["check_revision"] == 2


def test_suspected_conflict_still_needs_a_real_search_when_none_can_be_derived():
    class Unsearchable(SyntheticModel):
        async def create_text_response(self, **kwargs):
            response = await super().create_text_response(**kwargs)
            if json.loads(kwargs["user_prompt"])["role"] == "COVERAGE":
                value = json.loads(response.text)
                value["questions"][0].update(subject_mentions=[], property_or_rule=" ")
                return LlmTextResponse(text=json.dumps(value, ensure_ascii=False))
            return response

    result = run(model=Unsearchable(suspected))
    assert (result.status, result.failure_code, result.failure_cause) == (
        "INCOMPLETE", "INVALID_DECISION", "INVALID_DECISION_CHANGE_SEARCH_MISSING")
    assert "CHANGE_SEARCH_INSERTED" not in result.normalizations and not worker_searches(result.receipts)


def test_worker_change_search_query_uses_mentions_then_property_and_stays_searchable():
    from app.validation.contracts import IntentRefs, LookupIntent, QuestionDraft, validate_intents
    from app.validation.service import change_search_query

    base = QuestionDraft.model_validate(planned_question())
    assert change_search_query(base) == "민수 손 사용"
    mentions = base.model_copy(update={"subject_mentions": [" 서윤 ", "등대지기 서윤", "서윤", ""],
                                       "property_or_rule": "나이"})
    assert change_search_query(mentions) == "서윤 등대지기 서윤 나이"
    assert change_search_query(base.model_copy(update={"subject_mentions": []})) == "손 사용"
    long = base.model_copy(update={"subject_mentions": ["가" * 995], "property_or_rule": "나이 변화"})
    assert change_search_query(long) == ("가" * 995 + " 나이 변")
    cut_at_space = base.model_copy(update={"subject_mentions": ["가" * 999], "property_or_rule": "나이"})
    assert change_search_query(cut_at_space) == "가" * 999
    assert change_search_query(base.model_copy(update={"subject_mentions": [" "], "property_or_rule": " "})) == ""
    for draft in (base, long, cut_at_space):
        query = change_search_query(draft)
        assert len(query) <= 1000
        validate_intents([LookupIntent(ref="Q1-CHANGES_EXCEPTIONS", kind="CHANGES_EXCEPTIONS",
                                       operation="KEYWORD", purpose="검색", query=query)],
                         IntentRefs(search_operations=frozenset({"KEYWORD"})))


def test_frozen_provenance_uses_known_source_alias_without_server_ids():
    data = SyntheticData()
    identifier = data.documents["R1"].metadata["episodeId"]
    data.entries[0]["state"]["slots"]["STATUS:손"]["provenance"] = {
        "sourceKind": "EPISODE",
        "sourceEpisodeId": identifier,
        "sourceHash": data.documents["R1"].content_hash,
        "sourceCandidateIds": [str(uuid4())],
    }

    def judge(payload):
        assert {row["source_ref"] for row in payload["source_catalogue"]} == {"R1", "R2"}
        assert identifier not in json.dumps(payload)
        if len(payload["receipts"]) == 2:  # GH349 amendments: compact first; provenance only on BASELINE request
            value = decision(payload)
            value.update(action="FOLLOW_UP", outcome=None, basis=None, completion=None, follow_ups=[
                {"ref": "origin", "kind": "COMPARISON_CONTEXT", "operation": "BASELINE",
                 "purpose": "손 상태의 출처 회차 확인", "evidence_refs": ["B1"]}])
            return value
        return decision(payload)

    model = SyntheticModel(judge)
    result = run(data, model)
    assert result.status == "COMPLETED"
    compact, full = payloads(model, "JUDGE")
    assert compact["settings"][0]["state"]["slots"]["STATUS:손"] == {"factValue": "왼손 상실"}
    assert full["settings"][0]["state"]["slots"]["STATUS:손"]["provenance"]["source_ref"] == "R1"
    bundle = result.evidence_bundles["Q1"]
    assert bundle["settings"] == [{"entry_ref": "B1", "subject_ref": "S1"}]
    assert identifier not in json.dumps(bundle)


def test_merged_initial_obligations_reuse_one_read_and_keep_both_receipts():
    class DuplicatePlans(SyntheticModel):
        async def create_text_response(self, **kwargs):
            response = await super().create_text_response(**kwargs)
            role = json.loads(kwargs["user_prompt"])["role"]
            if role in {"PLANNER", "COVERAGE"}:
                value = json.loads(response.text)
                value["questions"][0]["lookup_intents"] = [
                    {
                        "ref": role + "-lookup",
                        "kind": "CHANGES_EXCEPTIONS",
                        "operation": "KEYWORD",
                        "purpose": "의수 사용 조건 확인",
                        "query": "의수",
                    }
                ]
                return LlmTextResponse(text=json.dumps(value))
            return response

    result = run(model=DuplicatePlans(planner_question=True))
    assert result.status == "COMPLETED" and len(result.questions) == 1
    assert len(result.questions[0].original_refs) == 2
    reads = [receipt for receipt in result.receipts if receipt.intent.operation == "KEYWORD"]
    assert len(reads) == 2 and reads[1].reused_from == reads[0].ref
    assert reads[1].evidence_refs == reads[0].evidence_refs


class BookOnlyData(SyntheticData):
    def __init__(self):
        from dataclasses import replace

        super().__init__()
        self.documents["T1"] = replace(
            self.documents["T1"], metadata={**self.documents["T1"].metadata, "episodeNo": 1}
        )
        self.entries = []
        self.texts["R1"] = "민수는 왼손이 없다."
        self.documents["R1"] = SourceDocument(
            "R1",
            "SETTING_BOOK",
            sha256(self.texts["R1"].encode()).hexdigest(),
            "v1",
            {
                "settingBookId": str(uuid4()),
                "revisionId": str(uuid4()),
                "revisionNo": 1,
                "referenceTimeKind": "WORK_START",
                "referenceEpisodeNo": None,
            },
        )
        self.source_reads = []

    async def read_input(self):
        return FrozenInput(self.context, self.documents["T1"], (self.documents["R1"],), "c" * 64, 0)

    async def read_source(self, source, start=None, end=None):
        self.source_reads.append(source.ref)
        return await super().read_source(source, start, end)


class BookReviewModel(SyntheticModel):
    async def create_text_response(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        role = payload["role"]
        self.calls.append((role, payload, kwargs))
        if role in {"PLANNER", "COVERAGE"}:
            refs = [row["entry_ref"] for row in payload.get("settings", [])]
            refs.extend(row["ref"] for row in payload.get("raw_rules", []))
            value = {
                "questions": [],
                "segments": [
                    {
                        "segment_ref": payload["segment"]["ref"],
                        "status": "NO_CHECK",
                        "reason": "합성 독립 처리",
                    }
                ],
                "rules": [
                    {
                        "entry_ref": ref,
                        "status": "NOT_APPLICABLE",
                        "reason": "읽은 규칙의 적용 범위 확인",
                    }
                    for ref in refs
                ],
            }
        else:
            value = {"mentions": []}
        return LlmTextResponse(text=json.dumps(value, ensure_ascii=False))


def test_zero_question_first_episode_independently_reads_unanalyzed_book_raw():
    data, model = BookOnlyData(), BookReviewModel()
    result = run(data, model)
    assert result.status == "COMPLETED"
    assert "R1" in data.source_reads
    book_inputs = [
        rule["quote"]
        for role, payload, kwargs in model.calls
        if role == "COVERAGE"
        for rule in payload.get("raw_rules", [])
    ]
    assert "민수는 왼손이 없다." in book_inputs
    assert any(row["role"] == "BOOK_COVERAGE" for row in result.coverage)
    # GH349: an empty catalogue has nothing to resolve, so SUBJECT_RESOLUTION needs no model call.
    assert all(role != "RELATED_SELECTION" for role, *_ in model.calls)
    assert {"role": "SUBJECT_RESOLUTION", "segment_ref": "TSEG1", "mechanical_subject_refs": [],
            "resolved_subject_refs": [], "mentions": 0} in result.coverage


def test_required_unanalyzed_book_read_failure_is_not_zero_findings():
    data = BookOnlyData()
    data.failed_ref = "R1"
    result = run(data, BookReviewModel())
    assert result.status == "INCOMPLETE" and result.failure_code == "SOURCE_UNREADABLE"


def test_search_io_is_charged_and_stops_at_invocation_deadline():
    from types import SimpleNamespace

    from app.validation.contracts import BudgetState, LookupIntent
    from app.validation.corpus import ValidationCorpus

    class Slow:
        async def search(self, *args):
            await asyncio.sleep(0.2)
            return SimpleNamespace(chunks=[], limited=False), set()

    async def inspect():
        data = SyntheticData()
        limits = InvocationBudget(10, 20, 0.01, 5000, 1000, 100, 10, 10, 1)
        state = BudgetState(limits)
        corpus = ValidationCorpus(data, await data.read_input(), state, search=Slow())
        started = asyncio.get_running_loop().time()
        with pytest.raises(IncompleteInspection, match="BUDGET_EXHAUSTED"):
            await corpus.lookup(
                LookupIntent(
                    ref="slow",
                    kind="CHANGES_EXCEPTIONS",
                    operation="KEYWORD",
                    purpose="조회",
                    query="의수",
                )
            )
        assert asyncio.get_running_loop().time() - started < 0.15
        assert state.lookups == 1

    asyncio.run(inspect())


def test_required_vector_search_preserves_missing_eligible_episode_readiness():
    from types import SimpleNamespace

    from app.retrieval.search_contracts import SearchReadiness
    from app.validation.contracts import BudgetState, LookupIntent
    from app.validation.corpus import ValidationCorpus

    class Partial:
        async def search(self, *args):
            return SimpleNamespace(
                chunks=[],
                limited=False,
                readiness=SearchReadiness(2, 2, 1, 2, 1, ("VECTOR_INDEX_PARTIAL",)),
            ), set()

    async def inspect():
        data = SyntheticData()
        state = BudgetState(InvocationBudget(10, 20, 10, 5000, 1000, 100, 10, 10, 1))
        corpus = ValidationCorpus(data, await data.read_input(), state, search=Partial())
        with pytest.raises(IncompleteInspection, match="VECTOR_INDEX_PARTIAL"):
            await corpus.lookup(
                LookupIntent(
                    ref="vector",
                    kind="CHANGES_EXCEPTIONS",
                    operation="VECTOR",
                    purpose="의미 조회",
                    query="의수",
                )
            )
        assert corpus.last_readiness["vector_ready_sources"] == 1
        assert corpus.last_readiness["source_count"] == 2

    asyncio.run(inspect())


def test_book_coverage_failure_or_budget_never_becomes_completed_zero():
    class MissingRule(BookReviewModel):
        async def create_text_response(self, **kwargs):
            response = await super().create_text_response(**kwargs)
            if json.loads(kwargs["user_prompt"]).get("raw_rules"):
                value = json.loads(response.text)
                value["rules"] = []
                return LlmTextResponse(text=json.dumps(value))
            return response

    for result in (
        run(BookOnlyData(), MissingRule()),
        run(BookOnlyData(), BookReviewModel(), model_calls=2),
    ):
        assert result.status == "INCOMPLETE"
        assert any(
            row["status"] == "UNPROCESSED"
            for row in result.coverage
            if row["role"] == "BOOK_SOURCE"
        )


def test_independent_book_rule_adds_question_with_real_book_evidence():
    class BookQuestion(BookReviewModel):
        async def create_text_response(self, **kwargs):
            payload = json.loads(kwargs["user_prompt"])
            if payload["role"] == "JUDGE":
                return LlmTextResponse(
                    text=json.dumps(
                        decision(
                            payload,
                            "CONFLICT",
                            "DIRECT_CONTRADICTION",
                            ["T1"] + [row["ref"] for row in payload["evidence"]],
                        )
                    )
                )
            response = await super().create_text_response(**kwargs)
            if payload.get("raw_rules"):
                value = json.loads(response.text)
                q = planned_question()
                q["subject_refs"] = []
                value["questions"] = [q]
                value["segments"][0]["status"] = "PLANNED"
                return LlmTextResponse(text=json.dumps(value))
            return response

    result = run(BookOnlyData(), BookQuestion())
    assert result.status == "COMPLETED" and result.decisions["Q1"].outcome == "CONFLICT"
    assert result.evidence_bundles["Q1"]["evidence"][0]["source_kind"] == "SETTING_BOOK"


def test_book_vector_index_is_not_required_and_raw_fallback_is_explicit():
    from app.validation.contracts import BudgetState, LookupIntent
    from app.validation.corpus import ValidationCorpus

    async def inspect():
        data = BookOnlyData()
        state = BudgetState(InvocationBudget(10, 20, 10, 5000, 1000, 100, 10, 10, 1))
        corpus = ValidationCorpus(data, await data.read_input(), state)
        refs, _limited = await corpus.lookup(
            LookupIntent(
                ref="book",
                kind="CHANGES_EXCEPTIONS",
                operation="VECTOR",
                purpose="책 근거 조회",
                query="왼손",
            )
        )
        assert refs and corpus.evidence[refs[0]]["source_kind"] == "SETTING_BOOK"
        assert corpus.last_readiness["book_vector_index_required"] is False
        assert corpus.last_readiness["vector_status"] == "NOT_APPLICABLE"
        assert corpus.last_readiness["executed_methods"] == ("KEYWORD",)

    asyncio.run(inspect())


@pytest.mark.parametrize("operation,vector_sources", [("VECTOR", 2), ("KEYWORD", 1)])
def test_complete_vector_and_keyword_with_optional_vector_gap_are_valid(operation, vector_sources):
    from types import SimpleNamespace

    from app.retrieval.search_contracts import SearchReadiness
    from app.validation.contracts import BudgetState, LookupIntent
    from app.validation.corpus import ValidationCorpus

    class Ready:
        async def search(self, *args):
            return SimpleNamespace(
                chunks=[],
                limited=False,
                readiness=SearchReadiness(
                    2,
                    2,
                    vector_sources,
                    2,
                    vector_sources,
                    () if vector_sources == 2 else ("VECTOR_INDEX_PARTIAL",),
                ),
            ), {"R1", "R2"}

    async def inspect():
        data = SyntheticData()
        state = BudgetState(InvocationBudget(10, 1, 10, 5000, 1000, 100, 10, 10, 1))
        corpus = ValidationCorpus(data, await data.read_input(), state, search=Ready())
        intent = LookupIntent(
            ref="ready",
            kind="CHANGES_EXCEPTIONS",
            operation=operation,
            purpose="조회",
            query="없는표현",
        )
        refs, limited = await corpus.lookup(intent)
        assert refs == () and not limited and state.lookups == 1
        assert corpus.last_readiness["vector_ready_sources"] == vector_sources
        with pytest.raises(IncompleteInspection, match="BUDGET_EXHAUSTED"):
            await corpus.lookup(intent)

    asyncio.run(inspect())


def test_partial_vector_pipeline_keeps_failed_receipt_and_never_decides_suspected():
    from types import SimpleNamespace

    from app.retrieval.search_contracts import SearchReadiness
    from app.validation.service import SettingValidationService

    class Partial:
        async def readiness(self, frozen):
            # GH344: ready when VECTOR is offered; the index turns partial before the lookup runs.
            return SearchReadiness(2, 2, 2, 2, 2)

        async def search(self, *args):
            return SimpleNamespace(
                chunks=[],
                limited=False,
                readiness=SearchReadiness(2, 2, 1, 2, 1, ("VECTOR_INDEX_PARTIAL",)),
            ), {"R1", "R2"}

    def judge(payload):
        value = decision(payload, "SUSPECTED_CONFLICT", "CHANGE_NOT_FOUND")
        value.update(
            action="FOLLOW_UP",
            outcome=None,
            basis=None,
            completion=None,
            follow_ups=[
                {
                    "ref": "required-vector",
                    "kind": "CHANGES_EXCEPTIONS",
                    "operation": "VECTOR",
                    "purpose": "필수 의미 검색",
                    "query": "의수",
                }
            ],
        )
        return value

    limits = InvocationBudget(30, 50, 30, 5000, 1000, 100, 10, 10, 10)
    result = asyncio.run(
        SettingValidationService(
            SyntheticData(), SyntheticModel(judge), limits, search=Partial(), embedding_client=object()
        ).inspect()
    )
    assert result.status == "INCOMPLETE" and result.failure_code == "VECTOR_INDEX_PARTIAL"
    assert not result.decisions and result.receipts[-1].status == "FAILED"
    assert result.receipts[-1].readiness["vector_ready_sources"] == 1
    assert "VECTOR_INDEX_PARTIAL" in result.receipts[-1].readiness["limitations"]


# --- GH349 grid v2: subject matching + SUBJECT_RESOLUTION instead of paging the whole catalogue ------------


def subject_data(data_type=None):
    """민수(S1, two entries) is named in TARGET; 서윤(S2) and the 북부 왕국 world setting(S3) are not."""
    data = (data_type or SyntheticData)()
    data.entries[0]["state"]["aliases"] = ["민수 씨"]
    data.entries += [
        {"entryRef": "B2", "domain": "characters", "subjectRef": "character:existing",
         "state": {"name": "민수", "aliases": ["민수 씨", "검사"], "slots": {"STATUS:검": {"factValue": "양손검"}}}},
        {"entryRef": "B3", "domain": "characters", "subjectRef": "character:seoyun",
         "state": {"name": "서윤", "aliases": ["등대지기"], "slots": {}}},
        {"entryRef": "B4", "domain": "worldSettings", "subjectRef": None,
         "state": {"targetWorldSettingId": "kingdom", "name": "북부 왕국", "aliases": [], "propertiesJson": {}}},
    ]
    return data


def payloads(model, role):
    return [payload for called, payload, _ in model.calls if called == role]


def test_subject_resolution_gets_deduplicated_subjects_and_mechanical_matches_only():
    model = SyntheticModel()
    result = run(subject_data(), model)
    assert result.status == "COMPLETED"
    subjects = [
        {"subject_ref": "S1", "domain": "characters", "name": "민수", "aliases": ["민수 씨", "검사"]},
        {"subject_ref": "S2", "domain": "characters", "name": "서윤", "aliases": ["등대지기"]},
        {"subject_ref": "S3", "domain": "worldSettings", "name": "북부 왕국", "aliases": []},
    ]
    (resolution,) = payloads(model, "RELATED_SELECTION")  # 4 entries, 3 subjects -> 3 rows
    assert resolution["subject_catalogue"] == subjects and resolution["mechanical_subject_refs"] == ["S1"]
    assert set(resolution) == {"role", "segment", "text", "text_start", "target_source_ref", "source_catalogue",
                               "subject_catalogue", "mechanical_subject_refs"}
    (planner,), (coverage,) = payloads(model, "PLANNER"), payloads(model, "COVERAGE")
    assert planner["subject_catalogue"] == coverage["subject_catalogue"] == subjects
    assert coverage["rule_refs"] == [row["entry_ref"] for row in coverage["settings"]] == ["B1", "B2"]
    _, _, kwargs = next(call for call in model.calls if call[0] == "RELATED_SELECTION")
    assert kwargs["call_options"].reasoning_effort == "high"
    assert kwargs["response_schema"].name == "validation_related_selection_v2"
    assert kwargs["response_schema"].schema["$defs"]["MentionResolution"]["properties"]["subject_ref"] == {
        "anyOf": [{"type": "string", "enum": ["S1", "S2", "S3"]}, {"type": "null"}]}
    assert [row for row in result.coverage if row["role"] == "SUBJECT_RESOLUTION"] == [
        {"role": "SUBJECT_RESOLUTION", "segment_ref": "TSEG1", "mechanical_subject_refs": ["S1"],
         "resolved_subject_refs": [], "mentions": 0}]


@pytest.mark.parametrize("mentions,rule_refs,resolved", [
    ([{"mention": "그 여자", "subject_ref": "S2", "reason": "앞 문장의 서윤을 가리킨다"}], ["B1", "B2", "B3"], ["S2"]),
    ([{"mention": "노인", "subject_ref": None, "reason": "카탈로그에 없는 인물"}], ["B1", "B2"], []),
    ([{"mention": "왕국", "subject_ref": "S3", "reason": "북부 왕국의 줄임말"},
      {"mention": "그", "subject_ref": "S1", "reason": "이름이 이미 나온 민수"}], ["B1", "B2", "B4"], ["S1", "S3"]),
])
def test_resolved_mentions_add_only_their_subjects_entries_in_catalogue_order(mentions, rule_refs, resolved):
    model = SyntheticModel(mentions=mentions)
    result = run(subject_data(), model)
    assert result.status == "COMPLETED"
    (coverage,) = payloads(model, "COVERAGE")
    assert coverage["rule_refs"] == [row["entry_ref"] for row in coverage["settings"]] == rule_refs
    record = next(row for row in result.coverage if row["role"] == "SUBJECT_RESOLUTION")
    assert record["resolved_subject_refs"] == resolved and record["mentions"] == len(mentions)


@pytest.mark.parametrize("mentions,cause", [
    ([{"mention": "그 여자", "subject_ref": "S9", "reason": "없는 대상"}], "SUBJECT_RESOLUTION_UNKNOWN_SUBJECT"),
    ([{"mention": "그", "subject_ref": "S2", "reason": "서윤"},
      {"mention": "그", "subject_ref": None, "reason": "다른 인물"}], "SUBJECT_RESOLUTION_DUPLICATE_MENTION"),
    ([{"mention": "  ", "subject_ref": "S2", "reason": "서윤"}], "SUBJECT_RESOLUTION_BLANK"),
    ([{"mention": "그 여자", "subject_ref": "S2", "reason": " "}], "SUBJECT_RESOLUTION_BLANK"),
])
def test_invalid_subject_resolution_stops_before_coverage(mentions, cause):
    model = SyntheticModel(mentions=mentions)
    result = run(subject_data(), model)
    assert (result.status, result.failure_code, result.failure_cause, result.failure_stage) == (
        "INCOMPLETE", "SUBJECT_RESOLUTION_INVALID", cause, "SUBJECT_RESOLUTION:TSEG1")
    assert not payloads(model, "COVERAGE") and not result.decisions


def test_no_name_match_and_no_resolved_mention_gives_coverage_no_settings():
    data = SyntheticData()
    data.entries = [{"entryRef": "B1", "domain": "characters", "subjectRef": "character:seoyun",
                     "state": {"name": "서윤", "aliases": [], "slots": {"STATUS:손": {"factValue": "왼손 상실"}}}}]
    model = SyntheticModel()
    result = run(data, model)
    assert result.status == "COMPLETED"
    (resolution,), (coverage,) = payloads(model, "RELATED_SELECTION"), payloads(model, "COVERAGE")
    assert resolution["mechanical_subject_refs"] == []
    assert coverage["settings"] == [] and coverage["rule_refs"] == []


def test_large_catalogue_needs_one_resolution_call_per_segment_instead_of_one_per_page():
    data = SyntheticData()
    names = {"character:existing": "민수", "character:seoyun": "서윤", "character:keeper": "등대지기"}
    data.entries = [{"entryRef": f"B{index + 1}", "domain": "characters", "subjectRef": subject,
                     "state": {"name": names[subject], "aliases": [],
                               "slots": {f"PROFILE:항목{index + 1}": {"factValue": "값"}}}}
                    for index, subject in enumerate(list(names) * 40)]
    model = SyntheticModel()
    result = run(data, model, catalogue_page_size=50)
    assert result.status == "COMPLETED"
    # Grid v1 called RELATED_SELECTION once per catalogue page: ceil(120 / 50) = 3 per segment.
    (resolution,) = payloads(model, "RELATED_SELECTION")
    assert [row["subject_ref"] for row in resolution["subject_catalogue"]] == ["S1", "S2", "S3"]
    (coverage,) = payloads(model, "COVERAGE")
    assert coverage["rule_refs"] == [f"B{index}" for index in range(1, 121, 3)]  # all 40 of 민수's entries


def test_local_mechanical_matches_use_exact_code_point_context_and_numeric_order():
    from app.validation.contracts import split_segments
    from app.validation.service import mechanical_subject_refs

    target = "😀서윤과 민수"
    settings = [{"subject_ref": "S10", "name": "민수", "aliases": []},
                {"subject_ref": "S2", "name": None, "aliases": ["서윤"]},
                {"subject_ref": "S3", "name": None, "aliases": []},
                {"subject_ref": "S4", "name": "", "aliases": [""]},
                {"subject_ref": "S5", "name": "서 윤", "aliases": []}]
    split = split_segments(target, core_chars=4, context_chars=2)
    assert mechanical_subject_refs(settings, target, split) == {"TSEG1": ["S2"], "TSEG2": ["S10"]}
    whole = split_segments(target, core_chars=100, context_chars=0)
    assert mechanical_subject_refs(settings, target, whole) == {"TSEG1": ["S2", "S10"]}


class ExecutionData(SyntheticData):
    """Leased execution: server projections, server clues and the server grid instead of local fallbacks."""

    execution = True

    @property
    def model_settings(self):
        from app.validation.service import _settings

        return _settings(self.entries)

    async def read_input(self):
        from dataclasses import replace

        frozen = await super().read_input()
        return replace(frozen, model_source_catalogue=[{"source_ref": source.ref, "source_kind": source.kind}
                                                       for source in frozen.sources])

    async def read_source(self, source, start=None, end=None):
        from dataclasses import replace

        value = await super().read_source(source, start, end)
        return replace(value, model_clues=[]) if source.ref == "T1" and start is None and end is None else value


def run_with_grid(grid, model):
    from app.validation.service import SettingValidationService
    from tests.test_validation_checkpoint import SavedStages

    class GridCheckpoint(SavedStages):
        async def setting_grid(self):
            return deepcopy(grid)

    limits = InvocationBudget(30, 50, 30, 5000, 1000, 100, 10, 10, 10)
    return asyncio.run(SettingValidationService(subject_data(ExecutionData), model, limits,
                                                checkpoint=GridCheckpoint()).inspect())


@pytest.mark.parametrize("version", [2, 3])  # v3 = v2 shape + 1-character names excluded (GH349 amendment)
def test_execution_trusts_the_server_grid_segment_subjects(version):
    model = SyntheticModel()
    # Locally 민수(S1) would match; the server grid is authoritative in execution.
    grid = {"gridVersion": version, "segmentSubjects": [{"segmentRef": "TSEG1", "subjectRefs": ["S2"]}]}
    result = run_with_grid(grid, model)
    assert result.status == "COMPLETED"
    (resolution,), (coverage,) = payloads(model, "RELATED_SELECTION"), payloads(model, "COVERAGE")
    assert resolution["mechanical_subject_refs"] == ["S2"] and coverage["rule_refs"] == ["B3"]


@pytest.mark.parametrize("grid,code", [
    (None, "GRID_VERSION_UNSUPPORTED"),
    ({"gridVersion": 1, "units": [{"stage": "RELATED_SELECTION", "ref": "TSEG1:PAGE1"}]}, "GRID_VERSION_UNSUPPORTED"),
    ({"gridVersion": 2}, "GRID_VERSION_UNSUPPORTED"),
    ({"gridVersion": 3}, "GRID_VERSION_UNSUPPORTED"),
    ({"gridVersion": 4, "segmentSubjects": [{"segmentRef": "TSEG1", "subjectRefs": ["S2"]}]}, "GRID_VERSION_UNSUPPORTED"),
    ({"gridVersion": "3", "segmentSubjects": [{"segmentRef": "TSEG1", "subjectRefs": ["S2"]}]},
     "GRID_VERSION_UNSUPPORTED"),
    ({"gridVersion": 3, "segmentSubjects": [{"segmentRef": "TSEG1", "subjectRefs": ["S9"]}]}, "INVALID_GRID_CONTRACT"),
    ({"gridVersion": 2, "segmentSubjects": [{"segmentRef": "TSEG1", "subjectRefs": ["S9"]}]}, "INVALID_GRID_CONTRACT"),
    ({"gridVersion": 2, "segmentSubjects": [{"segmentRef": "TSEG2", "subjectRefs": []}]}, "INVALID_GRID_CONTRACT"),
    ({"gridVersion": 2, "segmentSubjects": [{"segmentRef": "TSEG1", "subjectRefs": ["S1", "S1"]}]},
     "INVALID_GRID_CONTRACT"),
])
def test_execution_without_a_v2_grid_stops_before_subject_resolution(grid, code):
    model = SyntheticModel()
    result = run_with_grid(grid, model)
    assert (result.status, result.failure_code, result.failure_stage) == ("INCOMPLETE", code, "SUBJECT_RESOLUTION:TSEG1")
    assert [role for role, *_ in model.calls] == ["PLANNER"]


# --- GH349 amendment: 1-character names are not mechanically matched (grid v3) ------------------------------


def test_one_character_names_are_not_mechanically_matched_but_stay_in_the_catalogue():
    from app.validation.contracts import split_segments
    from app.validation.service import mechanical_subject_refs

    target = "😀민수는 양손으로 검을 쥐었다."
    settings = [{"subject_ref": "S1", "name": "민수", "aliases": []},
                {"subject_ref": "S2", "name": "검", "aliases": ["손"]},
                {"subject_ref": "S3", "name": None, "aliases": ["쥐", "양손"]},
                {"subject_ref": "S4", "name": "😀", "aliases": []}]  # one code point (two UTF-16 units)
    segments = split_segments(target, core_chars=100, context_chars=0)
    assert mechanical_subject_refs(settings, target, segments) == {"TSEG1": ["S1", "S3"]}

    data = SyntheticData()
    data.entries.append({"entryRef": "B2", "domain": "characters", "subjectRef": "character:sword",
                         "state": {"name": "검", "aliases": [], "slots": {}}})
    model = SyntheticModel()
    assert run(data, model).status == "COMPLETED"
    (resolution,) = payloads(model, "RELATED_SELECTION")  # 검(S2) is left for SUBJECT_RESOLUTION
    assert resolution["mechanical_subject_refs"] == ["S1"]
    assert [row["name"] for row in resolution["subject_catalogue"]] == ["민수", "검"]


# --- GH349 amendment: compact model rows and the COVERAGE input valve -------------------------------------


def heavy_data():
    """B1 carries what production rows carry besides values: slot provenance/evidence, history, identity evidence."""
    data = SyntheticData()
    state = data.entries[0]["state"]
    state["slots"]["STATUS:손"].update(
        factType="STATUS", factKey="손", valueJson={"value": "왼손 상실"},
        provenance={"confirmationStatus": "CONFIRMED", "sourceEpisodeNo": 1},
        supportingSources=[{"supportingSource": {"quote": "민수는 왼손을 잃었다.", "start": 0, "end": 11}}])
    state.update(snapshotVersion=3, identityEvidence=[{"quote": "민수"}],
                 historyFacts=[{"factType": "STATUS", "factKey": "손", "factValue": "양손 사용",
                                "supportingSource": {"quote": "옛 원문"}}])
    return data


def test_coverage_and_judge_read_compact_rows_while_the_bundle_carries_refs_only():
    from app.validation.projection import compact_setting_row
    from app.validation.service import _settings

    data, model = heavy_data(), SyntheticModel()
    result = run(data, model)
    assert result.status == "COMPLETED"
    canonical = _settings(data.entries)
    compact = [compact_setting_row(row) for row in canonical]
    assert compact[0]["state"] == {"name": "민수", "aliases": [],
                                   "slots": {"STATUS:손": {"factValue": "왼손 상실", "factType": "STATUS", "factKey": "손"}}}
    (coverage,), (judge,) = payloads(model, "COVERAGE"), payloads(model, "JUDGE")
    assert coverage["settings"] == judge["settings"] == compact and coverage["rule_refs"] == ["B1"]
    for payload in (coverage, judge):
        text = json.dumps(payload["settings"], ensure_ascii=False)
        assert "왼손 상실" in text and not any(key in text for key in (
            "historyFacts", "supportingSources", "supportingSource", "provenance", "identityEvidence", "valueJson"))
    # GH349 Amendment 2: the Backend checks only the bundle's ref set; the digest covers the refs-only bundle.
    bundle = result.evidence_bundles["Q1"]
    assert bundle["settings"] == [{"entry_ref": "B1", "subject_ref": "S1"}]
    digested = {key: value for key, value in bundle.items() if key != "input_digest"}
    assert bundle["input_digest"] == judge["input_digest"] == sha256(
        json.dumps(digested, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def test_entry_requested_by_a_baseline_lookup_reaches_the_next_judge_in_full():
    from app.validation.service import _settings

    def judge(payload):
        if len(payload["receipts"]) == 2:  # only the Worker's own BASELINE/TARGET_CONTEXT receipts so far
            value = decision(payload)
            value.update(action="FOLLOW_UP", outcome=None, basis=None, completion=None, follow_ups=[
                {"ref": "history", "kind": "COMPARISON_CONTEXT", "operation": "BASELINE",
                 "purpose": "손 상태의 이전 이력 확인", "evidence_refs": ["B1"]}])
            return value
        return decision(payload)

    data, model = heavy_data(), SyntheticModel(judge)
    result = run(data, model)
    assert result.status == "COMPLETED"
    first, second = payloads(model, "JUDGE")
    assert "historyFacts" not in first["settings"][0]["state"]
    assert second["settings"] == _settings(data.entries)  # the full canonical row, history included
    assert second["settings"][0]["state"]["historyFacts"][0]["factValue"] == "양손 사용"
    assert result.evidence_bundles["Q1"]["settings"] == [{"entry_ref": "B1", "subject_ref": "S1"}]


def valve_data(count=401):
    data = SyntheticData()
    data.entries = [{"entryRef": f"B{n}", "domain": "characters", "subjectRef": "character:existing",
                     "state": {"name": "민수", "aliases": [], "slots": {f"PROFILE:항목{n}": {"factValue": "값"}}}}
                    for n in range(1, count + 1)]
    return data


def test_coverage_valve_sends_400_rows_and_reports_the_rest_not_applicable(caplog):
    from app.validation.contracts import QuestionBatch, split_segments, validate_batch
    from app.validation.service import COVERAGE_TRUNCATED_REASON, MAX_COVERAGE_SETTINGS

    model = SyntheticModel()
    with caplog.at_level("WARNING", logger="app.validation.service"):
        result = run(valve_data(), model, catalogue_page_size=100)
    assert result.status == "COMPLETED"
    (coverage,) = payloads(model, "COVERAGE")
    assert MAX_COVERAGE_SETTINGS == 400
    assert len(coverage["settings"]) == len(coverage["rule_refs"]) == 400 and coverage["rule_refs"][-1] == "B400"
    _, _, kwargs = next(call for call in model.calls if call[0] == "COVERAGE")
    assert "B401" not in json.dumps(kwargs["response_schema"].schema)
    record = next(row for row in result.coverage if row["role"] == "COVERAGE")
    assert [row["entry_ref"] for row in record["rules"]] == [f"B{n}" for n in range(1, 402)]
    assert record["rules"][-1] == {"entry_ref": "B401", "status": "NOT_APPLICABLE", "reason": COVERAGE_TRUNCATED_REASON}
    assert result.normalizations["COVERAGE_SETTINGS_TRUNCATED"] == 1
    assert "coverage settings truncated total=401 kept=400 segment=TSEG1" in caplog.text
    # The stored batch covers every relevant entry, as the Worker and Backend rule-set checks require.
    batch = QuestionBatch.model_validate({"questions": [], "rules": record["rules"],
                                          "segments": [{"segment_ref": "TSEG1", "status": "NO_CHECK", "reason": "합성"}]})
    validate_batch(batch, split_segments(TARGET, core_chars=100, context_chars=10), {"S1"},
                   [f"B{n}" for n in range(1, 402)], TARGET)


def test_resumed_truncated_coverage_is_not_called_again_or_extended_twice():
    from app.validation.service import SettingValidationService
    from tests.test_validation_checkpoint import SavedStages

    async def inspect(saved, model):
        limits = InvocationBudget(30, 100, 30, 5000, 1000, 100, 10, 100, 10)
        return await SettingValidationService(valve_data(), model, limits, checkpoint=saved).inspect()

    saved, model = SavedStages(), SyntheticModel()
    saved.crash_stage = "COVERAGE"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inspect(saved, model))
    assert len(saved.stages["COVERAGE", "TSEG1"]["rules"]) == 401
    calls = len(model.calls)
    result = asyncio.run(inspect(saved, model))
    assert result.status == "COMPLETED"
    assert [role for role, *_ in model.calls[calls:]] == ["JUDGE"]
    record = next(row for row in result.coverage if row["role"] == "COVERAGE")
    assert len(record["rules"]) == 401
