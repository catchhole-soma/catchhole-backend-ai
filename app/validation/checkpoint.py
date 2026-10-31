"""Typed semantic state plus durable Java check identities; never a provider response cache."""
import json
from copy import deepcopy
from hashlib import sha256
from typing import Literal

from pydantic import Field

from app.validation.contracts import Contract, IncompleteInspection, LookupIntent


class QuestionRuntime(Contract):
    evidence: list[dict]
    receipts: list[dict]
    own_receipts: dict[str, list[dict]]
    pending: dict[str, list[tuple[LookupIntent, bool]]]
    target_context_reads: dict[str, list[dict]]
    baseline_selections: dict[str, list[str]]
    revisions: dict[str, int]


class SemanticState(Contract):
    checkpointVersion: int = Field(default=1, ge=1, le=1, strict=True)
    inputDigest: str = Field(pattern=r"^[a-f0-9]{64}$")
    phase: Literal["PLANNING", "JUDGING"]
    planning: dict = Field(default_factory=dict)
    runtime: QuestionRuntime | None = None


def lookup_operations(*payloads):
    """Lookup operations named anywhere in saved semantic payloads (plans, obligations, receipts, queue)."""
    found, stack = set(), list(payloads)
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("operation"), str):
                found.add(node["operation"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def payload_key(kind, payload):
    # This is retransmission identity only, never a server scope/check CAS digest.
    return kind + ":" + sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()


class ValidationCheckpoint:
    def __init__(self, client, claim, view, gate, branch="SETTING"):
        self.client, self.claim, self.gate, self.branch = client, claim, gate, branch
        if view.get("scope") != claim.scope.model_dump(mode="json"):
            raise IncompleteInspection("CHECKPOINT_SCOPE_CHANGED")
        self.stages = {(row["stage"], row["ref"]): row["payload"] for row in view["stages"]
                       if row["successful"] and row["branch"] == branch}
        self.checks = {row["localRef"]: self._check(row) for row in view["checks"]}
        saved = view["branches"].get(branch, {})
        self.version, self.saved = saved.get("version", 0), saved.get("payload")
        self.sources = {}
        self.grid = view.get("requiredGrid") or {}

    @staticmethod
    def _check(row):
        result = deepcopy(row)
        if result.get("outcome") == "INCOMPLETE":
            result.update(outcome=None, decision=None)
        elif result.get("decision"):
            result["bundle"] = result["decision"].pop("evidence_bundle")
        return result

    def bind_sources(self, frozen):
        self.sources = {source.ref: source for source in (frozen.target, *frozen.sources)}

    def citations(self, payload):
        found = {}
        def visit(node, key=None):
            if isinstance(node, list):
                for value in node:
                    visit(value, key)
            elif isinstance(node, dict):
                if key == "settings":
                    return  # Canonical domain state is not source text.
                if isinstance(node.get("quote"), str) or key in {"target_context", "additional_target_context"}:
                    ref = node.get("source_ref", "T1")
                    quote = node.get("quote", node.get("text"))
                    start = node.get("start")
                    if quote is not None and type(start) is int:
                        source = self.sources[ref]
                        row = {"sourceRef": ref, "sourceHash": source.content_hash,
                               "sourceVersion": source.source_version, "start": start,
                               "end": node.get("end", start + len(quote)), "quote": quote}
                        found[ref, start, row["end"], quote] = row
                for name, value in node.items():
                    visit(value, name)
        visit(payload)
        return list(found.values())

    async def _publish(self, route, payload, **fields):
        body = {"branch": self.branch, "payload": payload,
                "citations": self.citations(payload), **fields}
        return await self.client.leased(self.claim, route, gate=self.gate,
                                       idempotencyKey=payload_key(route, body), **body)

    def stage(self, stage, ref):
        return deepcopy(self.stages.get((stage, ref)))

    def planned_operations(self):
        """GH344: what an earlier attempt already saved; a resumed VECTOR plan or receipt stays valid."""
        return lookup_operations(list(self.stages.values()), list(self.checks.values()), self.saved)

    async def save_stage(self, stage, ref, payload):
        await self._publish("plans", payload, stage=stage, ref=ref, failureCode=None)
        self.stages[stage, ref] = deepcopy(payload)
        if stage == "BOOK_SOURCE":
            self.grid = (await self.client.load_checkpoint(self.claim, self.gate))["requiredGrid"]

    def book_segments(self, source_ref):
        book = next(row for row in self.grid["SETTING"]["books"] if row["sourceRef"] == source_ref)
        if "segments" not in book:
            raise IncompleteInspection("BOOK_NOT_COMPLETE")
        return book["segments"]

    async def merge(self, questions):
        if self.stage("MERGED_PLAN", "PLAN") is None:
            payload = {"questions": [{"localRef": q.ref, "originalRefs": list(q.original_refs),
                        "state": {"draft": q.draft.model_dump(),
                                  "lookup_obligations": [i.model_dump() for i in q.draft.lookup_intents]}}
                        for q in questions]}
            response = await self._publish("plans", payload, stage="MERGED_PLAN", ref="PLAN", failureCode=None)
            self.stages["MERGED_PLAN", "PLAN"] = payload
            self.checks = {row["localRef"]: self._check(row) for row in response["checks"]}
        return self.checks

    def load_state(self):
        if not self.saved:
            return None
        wire = deepcopy(self.saved)
        for data in (wire.get("runtime"),):
            if data is not None:
                data["target_context_reads"] = {row["question_ref"]: row["additional_target_context"]
                                                for row in data.pop("targetContexts")}
                receipts = {row["ref"]: row for row in data["receipts"]}
                data["own_receipts"] = {ref: [receipts[key] for key in refs]
                                         for ref, refs in data.pop("own_receipt_refs").items()}
                wire["planning"]["evidence"] = deepcopy(data["evidence"])
        state = SemanticState.model_validate(wire)
        if state.inputDigest != self.claim.scope.inputDigest:
            raise IncompleteInspection("CHECKPOINT_SCOPE_CHANGED")
        result = state.model_dump(exclude={"checkpointVersion", "inputDigest"})
        planning = result["planning"]
        evidence = {row["ref"]: row for row in planning.get("evidence", [])}
        for rows in planning.get("book_rules", {}).values():
            for row in rows:
                row.update(evidence[row["evidence_ref"]] | {"ref": row["ref"]})
        return result

    async def save_state(self, state):
        value = deepcopy(state)
        if value["phase"] == "PLANNING":
            value = {"phase": "PLANNING", "planning": {k: v for k, v in value.items() if k != "phase"}}
        value.update(checkpointVersion=1, inputDigest=self.claim.scope.inputDigest)
        value = SemanticState.model_validate(value).model_dump(mode="json")
        for rows in value["planning"].get("book_rules", {}).values():
            for row in rows:
                row.pop("quote", None)
        if value["runtime"] is not None:
            runtime = value["runtime"]
            value["planning"].pop("evidence", None)
            runtime["own_receipt_refs"] = {ref: [row["ref"] for row in rows]
                                             for ref, rows in runtime.pop("own_receipts").items()}
            runtime["targetContexts"] = [{"question_ref": ref, "additional_target_context": rows}
                                          for ref, rows in runtime.pop("target_context_reads").items()]
        response = await self._publish("checkpoint", value, expectedVersion=self.version)
        self.version = response["version"]
        self.saved = value

    async def decide(self, ref, decision, bundle):
        check = self.checks[ref]
        payload = {**decision.model_dump(), "evidence_bundle": bundle}
        await self._publish("units", payload, ref=ref, checkId=check["checkId"],
                            expectedCheckRevision=check["revision"],
                            checkInputDigest=check["checkInputDigest"], outcome=decision.outcome)
        check.update(outcome=decision.outcome, decision=decision.model_dump(), bundle=bundle)

    async def revise(self, affected, pending, questions, reason):
        changes = [{"checkId": self.checks[ref]["checkId"], "expectedRevision": self.checks[ref]["revision"],
                    "expectedInputDigest": self.checks[ref]["checkInputDigest"],
                    "state": {"draft": questions[ref].draft.model_dump(),
                              "lookup_obligations": self.checks[ref]["state"]["lookup_obligations"] +
                                  [i.model_dump() for i in pending[ref]]}}
                   for ref in sorted(affected)]
        body = {"reason": reason, "changes": changes, "citations": self.citations(changes)}
        response = await self.client.leased(self.claim, "revisions", gate=self.gate,
                                            idempotencyKey=payload_key("revisions", body), **body)
        current = await self.client.load_checkpoint(self.claim, self.gate)
        self.version = current["branches"][self.branch]["version"]
        self.saved = current["branches"][self.branch]["payload"]
        for row in response["checks"]:
            self.checks[row["localRef"]] = self._check(row)
        return self.checks

    async def typo(self, ref, payload):
        await self._publish("units", payload, ref=ref, checkId=None,
                            expectedCheckRevision=None, checkInputDigest=None, outcome="COMPLETED")
        self.stages["TYPO_SEGMENT", ref] = deepcopy(payload)
