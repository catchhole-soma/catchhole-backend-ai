"""Synthetic SETTING inspection fixtures: frozen data, scripted planner/judge models and a fake frozen RAG.

Everything here is invented text; model outputs exercise orchestration, never real model quality.
"""

import asyncio
import json
from hashlib import sha256
from uuid import UUID, uuid4

from app.llm.responses import LlmTextResponse
from app.retrieval.search_contracts import ChunkEvidence, SearchReadiness, SearchResult
from app.validation.contracts import IncompleteInspection, InvocationBudget
from app.validation.data import FrozenInput, SourceDocument, SourceRead, ValidationContext
from app.validation.models import RoleRuntime
from app.validation.projection import (
    current_settings,
    model_settings,
    setting_value_leaves,
    subject_catalogue,
)

TARGET = "민수는 양손으로 검을 쥐었다.\n그는 성문 앞에서 기다렸다.\n"
PRIOR = {"R1": "민수는 왼손을 잃었다.", "R2": "민수는 의수를 얻었고 그 의수로 검을 쥘 수 있었다."}
RUNTIME = RoleRuntime.of("gpt-6.1-terra", "medium", 2000)
ROLES = {"PLANNER": RUNTIME, "JUDGE": RUNTIME}


def budget(**changes):
    values = {"model_calls": 60, "lookups": 80, "seconds": 30.0, "input_tokens": 50000, "segment_chars": 10000,
              "context_chars": 100, "search_limit": 10}
    values.update(changes)
    return InvocationBudget(**values)


def character(entry_ref, subject, name, slots=None, **state):
    return {"entryRef": entry_ref, "domain": "characters", "subjectRef": subject,
            "state": {"name": name, "aliases": [], "slots": slots or {}, **state}}


def default_entries():
    return [
        character("B1", "character:minsu", "민수", {"STATUS:손": {"factType": "STATUS", "factKey": "손",
                                                                  "factValue": "왼손 상실"}},
                  historyFacts=[{"factType": "STATUS", "factKey": "손", "factValue": "양손 사용"}]),
        {"entryRef": "B2", "domain": "worldSettings",
         "state": {"name": "성문", "targetWorldSettingId": "00000000-0000-4000-8000-000000000013",
                   "propertiesJson": {"개방": "낮"}}},
        {"entryRef": "B3", "domain": "worldHistory",
         "state": {"name": "성문", "targetWorldSettingId": "00000000-0000-4000-8000-000000000013",
                   "value": "밤에도 개방", "operation": "UPDATE"}},
    ]


class SyntheticData:
    """Job-less frozen data client: SUBJECTS/SETTINGS from the shared projection parity functions."""

    def __init__(self, target=TARGET, entries=None, prior=PRIOR):
        self.context = ValidationContext(1, uuid4(), uuid4(), "b" * 64, sha256(target.encode()).hexdigest())
        self.texts = {"T1": target, **prior}
        self.documents = {
            ref: SourceDocument(ref, "EPISODE", sha256(text.encode()).hexdigest(), "v1",
                                {"episodeId": str(uuid4()), "episodeNo": index + 1})
            for index, (ref, text) in enumerate(self.texts.items())
        }
        self.documents["T1"].metadata["episodeNo"] = 101
        self.entries = default_entries() if entries is None else entries
        self.rows = model_settings(self.entries)
        self.reads = []
        self.invalidated = None

    async def read_input(self):
        self.reads.append(("INPUT",))
        return FrozenInput(self.context, self.documents["T1"],
                           tuple(self.documents[ref] for ref in self.texts if ref != "T1"), "c" * 64, len(self.entries))

    async def read_source(self, source, start=None, end=None):
        self.reads.append(("READ_SOURCE", source.ref, start, end))
        text = self.texts[source.ref]
        start = start or 0
        end = len(text) if end is None else end
        return SourceRead(source, text[start:end], start, end, len(text))

    async def read_subjects(self):
        self.reads.append(("SUBJECTS",))
        return subject_catalogue(self.rows)

    async def read_settings(self, subject_refs):
        self.reads.append(("SETTINGS", tuple(subject_refs)))
        if self.invalidated:
            raise IncompleteInspection(self.invalidated)
        return current_settings(self.rows, subject_refs)

    async def validate(self):
        self.reads.append(("VALIDATE",))


def draft(local_ref="hand", quote="민수는 양손으로 검을 쥐었다.", subject_refs=("S1",), mentions=("민수",),
          prop="손 사용", text=TARGET, start=None):
    start = text.index(quote) if start is None else start
    return {"local_ref": local_ref,
            "target_spans": [{"source_ref": "T1", "start": start, "end": start + len(quote), "quote": quote}],
            "subject_refs": list(subject_refs), "subject_mentions": list(mentions), "property_or_rule": prop,
            "time_context": "현재", "conditions": [], "assertion_mode": "NARRATION", "check_intent": "양손 사용 가능 여부",
            "reason": "행동 전제 확인"}


def decided(payload, outcome="NO_CONFLICT", basis=None, evidence_refs=None, *, form=None, **changes):
    basis = basis or {"CONFLICT": "DIRECT_CONTRADICTION", "SUSPECTED_CONFLICT": "CHANGE_NOT_FOUND",
                      "NEEDS_CONTEXT": "AMBIGUOUS_MEANING", "NO_CONFLICT": "COMPATIBLE"}[outcome]
    receipts = [row["ref"] for row in payload["receipts"]]
    facts = [row["entry_ref"] for row in payload["settings"]]
    known = ["T1", *facts, *(row["ref"] for row in payload["evidence"])]
    refs = evidence_refs or known
    assessment = {"reason": "대상·시점·조건과 확보 자료를 확인했다", "evidence_refs": known, "receipt_refs": receipts}
    answer = {
        "action": "DECIDED", "outcome": outcome, "basis": basis,
        "headline": None if outcome == "NO_CONFLICT" else "민수의 손 상태가 기존 설정과 달라요", "reason": "합성 판정",
        "impact": None if outcome == "NO_CONFLICT" else "CAUTION",
        "impact_reason": None if outcome == "NO_CONFLICT" else "설정 이해에 영향",
        "target_spans": payload["question"]["target_spans"], "evidence_refs": refs,
        "completion": {key: dict(assessment) for key in ("comparison_context", "changes_exceptions",
                                                         "no_pending_clues", "required_reads")},
        "disagreements": [],
    }
    if (form or payload["form"]) == "OPEN":
        answer.update(search_queries=[], context_requests=[])
    # Most scripted tests exercise a supported DB decision; gap tests override this or supply no rows.
    values = [(row["entry_ref"], leaf) for row in payload["settings"] for leaf in setting_value_leaves(row)]
    answer["db_evidence_sufficient"] = bool(values)
    if outcome == "NO_CONFLICT" and values:
        answer["basis_quotes"] = [{"ref": values[0][0], "quote": values[0][1][:200]}]
    answer.update(changes)
    return answer


def follow_up(payload, *, searches=(), contexts=()):
    return {"action": "FOLLOW_UP", "outcome": None, "basis": None, "headline": None, "reason": "DB 값만으로 판단할 수 없다",
            "impact": None, "impact_reason": None, "target_spans": payload["question"]["target_spans"],
            "evidence_refs": [], "completion": None, "disagreements": [], "search_queries": list(searches),
            "context_requests": [{"start": start, "end": end} for start, end in contexts]}


class ScriptedModel:
    """PLANNER returns `plans[segment_ref]` (default: no questions); JUDGE calls `judge(payload, n)`."""

    def __init__(self, plans=None, judge=None, *, gate=None):
        self.plans = plans or {}
        self.judge = judge or (lambda payload, n: decided(payload))
        self.calls, self.counts = [], {}
        self.active = self.peak = 0
        self.gate = gate

    async def count_input_tokens(self, **kwargs):
        return 100

    async def create_text_response(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        self.calls.append((payload["role"], payload, kwargs))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.gate is not None:
                await self.gate(payload)
            await asyncio.sleep(0)
            if payload["role"] == "PLANNER":
                ref = payload["segment"]["ref"]
                questions = self.plans.get(ref, [])
                result = {"questions": questions, "segments": [
                    {"segment_ref": ref, "status": "PLANNED" if questions else "NO_CHECK", "reason": "합성 처리"}]}
            else:
                qref = payload["question_ref"]
                self.counts[qref] = self.counts.get(qref, 0) + 1
                result = self.judge(payload, self.counts[qref])
        finally:
            self.active -= 1
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return LlmTextResponse(text=text, input_token_count=100, output_token_count=10)

    def judge_calls(self, qref=None):
        return [payload for role, payload, _ in self.calls
                if role == "JUDGE" and (qref is None or payload["question_ref"] == qref)]


class FakeEmbedding:
    model, version, dimensions = "text-embedding-3-small", "v1", 1536

    def __init__(self):
        self.queries = []
        self.last_receipt = None

    async def create_embedding(self, query):
        self.queries.append(query)
        await asyncio.sleep(0)
        self.last_receipt = {"queryHash": sha256(query.encode()).hexdigest()}
        return [0.0] * self.dimensions


class FakeSearch:
    """Frozen RAG stand-in: ready unless told otherwise; each search embeds once and returns R2's chunk."""

    def __init__(self, data, *, ready=True, partial=False, fail=None, hits=None):
        self.data, self.ready, self.partial, self.fail = data, ready, partial, fail
        self.hits = hits
        self.searches = []

    def _readiness(self):
        episodes = sum(document.kind == "EPISODE" for ref, document in self.data.documents.items() if ref != "T1")
        ready = episodes if self.ready and not self.partial else episodes - 1
        return SearchReadiness(episodes, episodes, ready, 4, 4 if self.ready else 0,
                               ("VECTOR_INDEX_PARTIAL",) if self.partial else ())

    async def readiness(self, frozen):
        return self._readiness()

    async def search(self, frozen, query, embedding_client, limit):
        self.searches.append((query, limit))
        await embedding_client.create_embedding(query)
        if self.fail:
            raise IncompleteInspection(self.fail)
        refs = self.hits if self.hits is not None else ["R2"]
        chunks = tuple(self.chunk(ref) for ref in refs)
        return SearchResult(chunks, self._readiness())

    def chunk(self, ref):
        document, text = self.data.documents[ref], self.data.texts[ref]
        return ChunkEvidence(uuid4(), UUID(document.metadata["episodeId"]), document.metadata["episodeNo"], 0, text, 0,
                             len(text), sha256(text.encode()).hexdigest(), "synthetic/" + ref, "v1",
                             document.content_hash, "policy", ("VECTOR",), 0.9)
