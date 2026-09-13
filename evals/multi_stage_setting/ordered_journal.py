"""Read-only mirror of Java's sealed v1 journal replay, with no Gold or LLM dependency.

This is a structural replay boundary. Domain validation and candidate coverage are
the Backend seal operation's responsibility; arbitrary Stage1/Stage2 predictions
must never be relabelled as sealed changes by this module.
"""

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from evals.multi_stage_setting.contracts import StrictModel


ROOTS = {"characters", "worldSettings", "references"}


class JournalChange(StrictModel):
    event_id: str = Field(min_length=1)
    path: list[str] = Field(min_length=2)
    value: Any = None
    remove: StrictBool = False
    operation: str = Field(min_length=1)
    source_candidate_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_change(self) -> "JournalChange":
        if self.path[0] not in ROOTS or any(not part for part in self.path):
            raise ValueError("Journal path must address a non-empty child of an allowed root.")
        if not self.event_id.strip() or not self.operation.strip():
            raise ValueError("Journal eventId and operation must not be blank.")
        if not self.remove and self.value is None:
            raise ValueError("A journal SET must not contain null.")
        return self


class SealedJournal(StrictModel):
    format_version: Literal[1]
    status: Literal["SEALED"]
    run_id: UUID
    work_id: UUID
    generation: int = Field(ge=1)
    sequence: int = Field(ge=0)
    episode_no: int = Field(ge=1)
    job_id: UUID
    input_state_hash: str = Field(min_length=1)
    output_state_hash: str = Field(min_length=1)
    changes: list[JournalChange]


def load_journal_json(text: str) -> Any:
    """Preserve decimal precision from the Java JSON wire format."""
    return json.loads(text, parse_float=Decimal)


def canonical_journal_json(value: Any) -> str:
    """Match Java compact JSON, sorted object keys and plain normalized decimals."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, UUID):
        return canonical_journal_json(str(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (Decimal, float)):
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            raise ValueError("Journal JSON numbers must be finite.")
        if not number:
            return "0"
        # Decimal.normalize() rounds to the active context, so strip zeroes from
        # the exact fixed-point representation instead (large values stay exact).
        plain = format(number, "f")
        return plain.rstrip("0").rstrip(".") if "." in plain else plain
    if isinstance(value, list):
        return "[" + ",".join(canonical_journal_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Journal JSON object keys must be strings.")
        # Java String.compareTo orders UTF-16 code units, including supplementary
        # Unicode characters; Python's default scalar ordering differs there.
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(
            canonical_journal_json(key) + ":" + canonical_journal_json(value[key])
            for key in keys
        ) + "}"
    raise ValueError(f"Unsupported journal JSON type: {type(value).__name__}")


def journal_state_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_journal_json(state).encode("utf-8")).hexdigest()


def replay_sealed_journals(
    initial_state: dict[str, Any],
    journals: list[SealedJournal],
    *,
    run_id: UUID,
    work_id: UUID,
    generation: int,
    before_sequence: int,
    before_episode_no: int,
) -> dict[str, Any]:
    """Replay exactly the completed predecessors or fail, without partial mutation."""
    if set(initial_state) != ROOTS or any(
        not isinstance(initial_state[root], dict) for root in ROOTS
    ):
        raise ValueError("Initial journal state must have the three object roots.")
    if before_sequence < 0 or before_episode_no < 1:
        raise ValueError("Invalid requested sequence or episode.")
    if len(journals) != before_sequence:
        raise ValueError("Every predecessor must have exactly one sealed journal.")
    state = deepcopy(initial_state)
    seen: dict[str, str] = {}
    previous_episode = 0
    seen_jobs: set[UUID] = set()
    for sequence, journal in enumerate(journals):
        # Revalidate models as callers may have used Pydantic's unchecked copy.
        journal = SealedJournal.model_validate(journal.model_dump())
        if (journal.run_id, journal.work_id, journal.generation) != (
            run_id, work_id, generation
        ):
            raise ValueError("Journal belongs to a different run, work, or generation.")
        if journal.sequence != sequence or not (
            previous_episode < journal.episode_no < before_episode_no
        ):
            raise ValueError("Journal sequence is missing, reordered, or from a future episode.")
        if journal.job_id in seen_jobs:
            raise ValueError("A job must not occur twice in a journal chain.")
        seen_jobs.add(journal.job_id)
        if journal.input_state_hash != journal_state_hash(state):
            raise ValueError("Journal inputStateHash is stale.")
        for change in journal.changes:
            canonical = canonical_journal_json(change.model_dump(by_alias=True))
            previous = seen.get(change.event_id)
            if previous is not None:
                if previous != canonical:
                    raise ValueError("An eventId was reused with different change content.")
                continue
            parent = state
            for part in change.path[:-1]:
                parent = parent.get(part)
                if not isinstance(parent, dict):
                    raise ValueError("Journal path parent must already be an object.")
            leaf = change.path[-1]
            if change.remove:
                if leaf not in parent:
                    raise ValueError("Journal REMOVE target does not exist.")
                del parent[leaf]
            else:
                parent[leaf] = deepcopy(change.value)
            seen[change.event_id] = canonical
        if journal.output_state_hash != journal_state_hash(state):
            raise ValueError("Journal outputStateHash does not match the replayed state.")
        previous_episode = journal.episode_no
    return state
