"""Synthetic model outputs exercise orchestration, never Terra semantic quality."""

import asyncio
import json
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
    def __init__(self, judge=None, *, dropped=False, planner_question=False):
        self.calls = []
        self.judge = judge or decision
        self.dropped = dropped
        self.planner_question = planner_question

    async def count_input_tokens(self, **kwargs):
        return 80

    async def create_text_response(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        role = payload["role"]
        self.calls.append((role, payload, kwargs))
        if role == "RELATED_SELECTION":
            result = {
                "entries": [
                    {
                        "entry_ref": row["entry_ref"],
                        "selected": True,
                        "reason": "독립적으로 원고에 등장하는 인물",
                    }
                    for row in payload["catalogue"]
                ]
            }
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
    result = run(model=model)
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


def test_suspected_conflict_cannot_claim_unperformed_change_search():
    result = run(
        model=SyntheticModel(
            lambda payload: decision(payload, "SUSPECTED_CONFLICT", "CHANGE_NOT_FOUND")
        )
    )
    assert result.status == "INCOMPLETE" and result.failure_code == "INVALID_DECISION"


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
        assert (
            payload["settings"][0]["state"]["slots"]["STATUS:손"]["provenance"]["source_ref"]
            == "R1"
        )
        assert {row["source_ref"] for row in payload["source_catalogue"]} == {"R1", "R2"}
        assert identifier not in json.dumps(payload)
        return decision(payload)

    assert run(data, SyntheticModel(judge)).status == "COMPLETED"


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
            value = {"entries": []}
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
            SyntheticData(), SyntheticModel(judge), limits, search=Partial()
        ).inspect()
    )
    assert result.status == "INCOMPLETE" and result.failure_code == "VECTOR_INDEX_PARTIAL"
    assert not result.decisions and result.receipts[-1].status == "FAILED"
    assert result.receipts[-1].readiness["vector_ready_sources"] == 1
    assert "VECTOR_INDEX_PARTIAL" in result.receipts[-1].readiness["limitations"]
