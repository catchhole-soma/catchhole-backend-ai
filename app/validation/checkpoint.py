"""Durable Java stage/check identities; never a provider response cache or a runtime state blob.

Grid v4 keeps no JUDGING runtime checkpoint: published PLANNER stages, MERGED_PLAN checks and decided units
are the resume points, and an unpublished question restarts at its first judge call.
"""
import json
from copy import deepcopy
from hashlib import sha256

from app.validation.contracts import IncompleteInspection


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

    async def save_stage(self, stage, ref, payload):
        await self._publish("plans", payload, stage=stage, ref=ref, failureCode=None)
        self.stages[stage, ref] = deepcopy(payload)

    async def setting_grid(self):
        """The server-prepared SETTING grid, or None if the server has none yet.

        The first semantic publication creates it, so a missing branch is reloaded once from Java.
        """
        if not isinstance(self.grid.get(self.branch), dict):
            self.grid = (await self.client.load_checkpoint(self.claim, self.gate)).get("requiredGrid") or {}
        return deepcopy(self.grid.get(self.branch))

    async def merge(self, questions):
        if self.stage("MERGED_PLAN", "PLAN") is None:
            payload = {"questions": [{"localRef": q.ref, "originalRefs": list(q.original_refs),
                        "state": {"draft": q.draft.model_dump(), "lookup_obligations": []}}
                        for q in questions]}
            response = await self._publish("plans", payload, stage="MERGED_PLAN", ref="PLAN", failureCode=None)
            self.stages["MERGED_PLAN", "PLAN"] = payload
            self.checks = {row["localRef"]: self._check(row) for row in response["checks"]}
        return self.checks

    async def decide(self, ref, decision, bundle):
        check = self.checks[ref]
        payload = {**decision.model_dump(), "evidence_bundle": bundle}
        await self._publish("units", payload, ref=ref, checkId=check["checkId"],
                            expectedCheckRevision=check["revision"],
                            checkInputDigest=check["checkInputDigest"], outcome=decision.outcome)
        check.update(outcome=decision.outcome, decision=decision.model_dump(), bundle=bundle)

    async def fail(self, ref, draft, code, cause=None, call=0):
        """Publish one question as INCOMPLETE (owner decision ③): a question-level failure ends only this question.

        The payload names the sentences that could not be checked (the question's own target spans, cited) and why
        (safe `failure_code`/`failure_cause`, the judge call it failed on). The check stays re-judgeable: the
        server never counts it as completed, so the author's retry judges it again on the same revision.
        """
        check = self.checks[ref]
        payload = {"action": "INCOMPLETE", "outcome": "INCOMPLETE", "failure_code": code, "failure_cause": cause,
                   "failed_call": call, "target_spans": [span.model_dump() for span in draft.target_spans]}
        await self._publish("units", payload, ref=ref, checkId=check["checkId"],
                            expectedCheckRevision=check["revision"],
                            checkInputDigest=check["checkInputDigest"], outcome="INCOMPLETE")
        check.update(outcome=None, decision=None, failure=payload)

    async def typo(self, ref, payload):
        await self._publish("units", payload, ref=ref, checkId=None,
                            expectedCheckRevision=None, checkInputDigest=None, outcome="COMPLETED")
        self.stages["TYPO_SEGMENT", ref] = deepcopy(payload)
