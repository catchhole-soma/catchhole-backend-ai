"""Bounded synthetic event replay used to exercise both snapshot readers.

This is an in-memory test driver, not the Java persistence lifecycle or an RDF
reasoner. Its explicit events already contain the human-chosen operation and
relation tuple. A confirmed alias mapping is input, never inferred from names.
The driver preserves intermediate states and supplies the same cutoff-specific
snapshot to B and C; neither snapshot reader gains a history engine from this.

Only ordered PRESENT events are supported. UPDATE adds confirmation evidence;
the shared Fact contract has no label/summary to update. Retraction of a whole
curated fact is distinct from END. Historical reconstruction after a later
retraction is explicitly unsupported here. Current user deletion still applies
to earlier snapshots. There is no database, network, LLM, oracle or proof engine.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from .input import Case, Slot

Operation = Literal["ADD", "UPDATE", "END", "NO_MENTION", "RETRACT", "SUPPRESS", "ALIAS"]
OPERATIONS = {"ADD", "UPDATE", "END", "NO_MENTION", "RETRACT", "SUPPRESS", "ALIAS"}
MAX_EVENTS = 64


@dataclass(frozen=True)
class Event:
    event_ref: str
    episode_no: int
    operation: Operation
    fact_ref: str | None = None
    slot: dict | None = None
    text: str | None = None
    source_episode_no: int | None = None
    alias_ref: str | None = None
    canonical_ref: str | None = None


def _positive(value: int) -> bool:
    return type(value) is int and value > 0


def _resolve(ref: str, aliases: dict[str, str]) -> str:
    visited = set()
    while ref in aliases:
        if ref in visited:
            raise ValueError("alias cycle")
        visited.add(ref)
        ref = aliases[ref]
    return ref


def _slot(slot: dict, aliases: dict[str, str]) -> dict:
    value = deepcopy(slot)
    for field in ("sourceNodeRef", "targetNodeRef"):
        value[field] = _resolve(value[field], aliases)
    if value["sourceNodeRef"] == value["targetNodeRef"]:
        raise ValueError("alias mapping produces a self relation")
    return value


def _key(slot: dict) -> tuple:
    ends = (slot["sourceNodeRef"], slot["targetNodeRef"])
    if slot["direction"] != "DIRECTED":
        ends = tuple(sorted(ends))
    return slot["workId"], *ends, slot["axis"], slot["kind"], slot["direction"]


class Replay:
    """Replay explicit synthetic events, retaining a copy after every event.

    ``snapshot(cutoff)`` reconstructs that reader checkpoint in this driver.
    B/C only receive the resulting Case. The current deletion overlay is applied
    even when the requested checkpoint predates the deletion.
    """

    def __init__(self, case_id: str, work_id: str, nodes: dict[str, str], events: list[Event]):
        if not case_id or not work_id or not nodes or any(not k or not v for k, v in nodes.items()):
            raise ValueError("case, work and named nodes are required")
        if not events or len(events) > MAX_EVENTS:
            raise ValueError("replay requires between 1 and 64 events")
        self.case_id, self.work_id, self.nodes = case_id, work_id, dict(nodes)
        self._states: list[dict] = []
        state = {"facts": {}, "sources": [], "claims": [], "aliases": {}, "deletions": []}
        seen: set[str] = set()
        previous_episode = 0
        for number, original in enumerate(events, 1):
            event = deepcopy(original)
            if (
                not isinstance(event.event_ref, str)
                or not event.event_ref.strip()
                or event.event_ref in seen
            ):
                raise ValueError("event refs must be unique and nonempty")
            if not _positive(event.episode_no) or event.episode_no < previous_episode:
                raise ValueError("events require positive, nondecreasing episode numbers")
            if event.operation not in OPERATIONS:
                raise ValueError("unsupported operation")
            self._apply(state, event, number)
            state["episode"] = event.episode_no
            state["revision"] = number
            self._states.append(deepcopy(state))
            previous_episode = event.episode_no
            seen.add(event.event_ref)

    @property
    def history(self) -> tuple[dict, ...]:
        """Defensive copies of actual replay checkpoints, without expected output."""
        return tuple(deepcopy(self._states))

    def _apply(self, state: dict, event: Event, sequence: int) -> None:
        operation = event.operation
        if operation in {"NO_MENTION", "ALIAS", "SUPPRESS"}:
            if event.text is not None or event.source_episode_no is not None:
                raise ValueError("non-narrative operations must not fabricate narrative evidence")
        elif (
            not event.text
            or not event.text.strip()
            or (
                event.source_episode_no is not None
                and (
                    not _positive(event.source_episode_no)
                    or event.source_episode_no != event.episode_no
                )
            )
        ):
            raise ValueError("PRESENT operations require evidence at the event episode")

        if operation == "NO_MENTION":
            if any(
                value is not None
                for value in (event.fact_ref, event.slot, event.alias_ref, event.canonical_ref)
            ):
                raise ValueError("NO_MENTION has no relation or identity decision")
            return
        if operation == "ALIAS":
            if event.fact_ref is not None or event.slot is not None:
                raise ValueError("ALIAS accepts only a confirmed node mapping")
            if event.alias_ref not in self.nodes or event.canonical_ref not in self.nodes:
                raise ValueError("alias mapping references unknown nodes")
            target = _resolve(event.canonical_ref, state["aliases"])
            if target == event.alias_ref or event.alias_ref in state["aliases"]:
                raise ValueError("alias cycle or conflicting mapping")
            state["aliases"][event.alias_ref] = target
            for fact in state["facts"].values():
                _slot(fact["slot"], state["aliases"])
            return
        if event.alias_ref is not None or event.canonical_ref is not None:
            raise ValueError("only ALIAS accepts an identity mapping")

        if operation == "ADD":
            if not event.fact_ref or event.fact_ref in state["facts"] or event.slot is None:
                raise ValueError("ADD requires a new fact ref and an explicit slot")
            slot = Slot.model_validate(event.slot).to_dict()
            if slot["workId"] != self.work_id or any(
                slot[field] not in self.nodes for field in ("sourceNodeRef", "targetNodeRef")
            ):
                raise ValueError("slot references another work or an unknown node")
            canonical = _key(_slot(slot, state["aliases"]))
            if any(_key(fact["slot"]) == canonical for fact in self._project(state)):
                raise ValueError("ADD cannot replace an existing canonical slot")
            state["facts"][event.fact_ref] = {
                "factRef": event.fact_ref,
                "slot": slot,
                "status": "ACTIVE",
                "factValidity": "VALID",
                "knownFromEpisodeNo": event.episode_no,
                "lastChangedEpisodeNo": event.episode_no,
                "supportClaimRefs": [],
                "endClaimRefs": [],
                "correctionSourceRefs": [],
                "createdSequence": sequence,
                "changedSequence": sequence,
            }
        elif event.slot is not None or event.fact_ref not in state["facts"]:
            raise ValueError("operation requires an existing immutable fact slot")

        fact = state["facts"][event.fact_ref]
        canonical = _key(_slot(fact["slot"], state["aliases"]))
        group = [
            candidate
            for candidate in state["facts"].values()
            if _key(_slot(candidate["slot"], state["aliases"])) == canonical
        ]
        current = max(group, key=lambda value: value["changedSequence"])
        if operation == "SUPPRESS":
            state["deletions"].append({"slot": deepcopy(fact["slot"]), "revision": sequence})
            return
        if current["factValidity"] != "VALID":
            raise ValueError("retracted records cannot resume in this bounded driver")
        if operation == "END" and current["status"] != "ACTIVE":
            raise ValueError("END requires an active relation")
        if operation == "RETRACT" and current["status"] != "ACTIVE":
            raise ValueError("retracting an already ended record is outside this driver")

        source_ref, claim_ref = f"s-{event.event_ref}", f"c-{event.event_ref}"
        state["sources"].append(
            {
                "sourceRef": source_ref,
                "workId": self.work_id,
                "episodeNo": event.episode_no,
                "text": event.text,
                "quote": event.text,
            }
        )
        if operation == "RETRACT":
            for member in group:
                member.update(
                    status="ACTIVE",
                    factValidity="RETRACTED",
                    lastChangedEpisodeNo=event.episode_no,
                    correctionSourceRefs=[source_ref],
                    endClaimRefs=[],
                    changedSequence=sequence,
                )
            retired = {ref for member in group for ref in member["supportClaimRefs"]}
            for claim in state["claims"]:
                if claim["claimRef"] in retired:
                    claim["curationStatus"] = "RETRACTED"
            return
        state["claims"].append(
            {
                "claimRef": claim_ref,
                "sourceRef": source_ref,
                "subjectCandidateRefs": [fact["slot"]["sourceNodeRef"]],
                "objectCandidateRefs": [fact["slot"]["targetNodeRef"]],
                "statementMode": "ASSERTED",
                "curationStatus": "CONFIRMED",
                "temporalScope": "PRESENT",
                "narrativeContextId": "story-present",
            }
        )
        fact.update(
            status="ENDED" if operation == "END" else "ACTIVE",
            lastChangedEpisodeNo=event.episode_no,
            changedSequence=sequence,
        )
        if operation == "END":
            fact["endClaimRefs"] = [claim_ref]
        else:
            fact["supportClaimRefs"].append(claim_ref)
            fact["endClaimRefs"] = []

    @staticmethod
    def _project(state: dict) -> list[dict]:
        groups: dict[tuple, list[dict]] = {}
        for fact in state["facts"].values():
            groups.setdefault(_key(_slot(fact["slot"], state["aliases"])), []).append(fact)
        result = []
        for members in groups.values():
            representative = min(
                members,
                key=lambda fact: (
                    fact["slot"] != _slot(fact["slot"], state["aliases"]),
                    fact["createdSequence"],
                ),
            )
            latest = max(members, key=lambda fact: fact["changedSequence"])
            value = {
                key: deepcopy(item)
                for key, item in latest.items()
                if key not in {"createdSequence", "changedSequence"}
            }
            value.update(
                factRef=representative["factRef"],
                slot=_slot(representative["slot"], state["aliases"]),
                knownFromEpisodeNo=min(member["knownFromEpisodeNo"] for member in members),
                supportClaimRefs=sorted(
                    {ref for member in members for ref in member["supportClaimRefs"]}
                ),
            )
            result.append(value)
        return sorted(result, key=lambda fact: fact["factRef"])

    def snapshot(self, cutoff_episode_no: int) -> Case:
        if not _positive(cutoff_episode_no) or cutoff_episode_no > self._states[-1]["episode"]:
            raise ValueError("snapshot cutoff is outside the recorded history")
        current = self._states[-1]
        if any(
            fact["factValidity"] == "RETRACTED" and fact["lastChangedEpisodeNo"] > cutoff_episode_no
            for fact in current["facts"].values()
        ):
            raise ValueError("past reconstruction after a later retraction is unsupported")
        candidates = [state for state in self._states if state["episode"] <= cutoff_episode_no]
        state = (
            deepcopy(candidates[-1])
            if candidates
            else {"facts": {}, "sources": [], "claims": [], "aliases": {}, "deletions": []}
        )
        facts = self._project(state)
        for claim in state["claims"]:
            for field in ("subjectCandidateRefs", "objectCandidateRefs"):
                claim[field] = [_resolve(ref, state["aliases"]) for ref in claim[field]]
        deleted = {}
        for item in current["deletions"]:
            deleted.setdefault(_key(_slot(item["slot"], current["aliases"])), item["revision"])
        suppressions = [
            {
                "suppressionRef": f"sup-{fact['factRef']}",
                "slot": deepcopy(fact["slot"]),
                "reason": "USER_DELETE",
                "appliesTo": "ALL_EPISODES",
                "recordedRevision": deleted[_key(_slot(fact["slot"], current["aliases"]))],
            }
            for fact in facts
            if _key(_slot(fact["slot"], current["aliases"])) in deleted
        ]
        return Case.model_validate(
            {
                "caseId": f"{self.case_id}-e{cutoff_episode_no}",
                "workId": self.work_id,
                "snapshotThroughEpisodeNo": cutoff_episode_no,
                "snapshotRevision": current["revision"],
                "nodes": [
                    {"nodeRef": ref, "name": name, "workId": self.work_id}
                    for ref, name in self.nodes.items()
                ],
                "sources": state["sources"],
                "claims": state["claims"],
                "facts": facts,
                "suppressions": suppressions,
                "queries": [
                    {
                        "queryRef": f"q{cutoff_episode_no}",
                        "mode": "READER_KNOWLEDGE",
                        "cutoffEpisodeNo": cutoff_episode_no,
                        "storyContextId": None,
                    }
                ],
            }
        )
