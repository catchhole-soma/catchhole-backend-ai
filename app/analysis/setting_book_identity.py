"""Validate BOOK-only typed parent partitions against one immutable source.

Provider refs stay request-local. Only this boundary translates them into persisted
identity refs; first-stage output and names alone cannot create a proof.
"""

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis.character_name_resolver import located_discovery_key
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.schemas import ExtractedEvidenceSpan
from app.domain.enums import AnalysisSourceKind, SettingCandidateKind
from app.schemas.analysis_source import WorkerAnalysisSource


def normalize_book_group_name(value: str) -> str:
    # SettingCandidateGroupNameNormalizer.toGroupKey: Java trim + ASCII \s + ROOT lower.
    # Keep punctuation and Unicode spaces; the general name resolver is broader.
    trimmed = value.strip("".join(chr(code) for code in range(33)))
    return re.sub(r"[ \t\n\x0b\f\r]+", " ", trimmed).lower()


class _TypedIdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class BookIdentitySpan(_TypedIdentityModel):
    quote: str = Field(min_length=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)

    def wire(self):
        return {"quote": self.quote, "startOffset": self.start_offset, "endOffset": self.end_offset}


class BookIdentityMember(_TypedIdentityModel):
    target_ref: str = Field(pattern=r"^[KD][1-9][0-9]*$")
    profile_evidence: BookIdentitySpan


class BookIdentityGroup(_TypedIdentityModel):
    group_ref: str = Field(pattern=r"^G[1-9][0-9]*$")
    decision: Literal["DISTINCT_IDENTITIES"]
    normalized_name: str = Field(min_length=1)
    members: list[BookIdentityMember] = Field(min_length=2)
    distinct_evidence_spans: list[BookIdentitySpan] = Field(min_length=1)


@dataclass(frozen=True)
class BookIdentityContext:
    analysis_job_id: UUID
    source: WorkerAnalysisSource
    source_text: str

    def __post_init__(self):
        if (
            self.source.source_kind != AnalysisSourceKind.SETTING_BOOK
            or self.source.content_hash != sha256(self.source_text.encode("utf-8")).hexdigest()
        ):
            raise ComparisonValidationError(
                "Identity source does not match its immutable book manifest."
            )


@dataclass(frozen=True)
class BookIdentityProof:
    context: BookIdentityContext
    group_id: str
    normalized_name: str
    members: tuple[tuple[str, BookIdentitySpan], ...]
    distinct_evidence_spans: tuple[BookIdentitySpan, ...]
    parent_target_ref: str

    def wire(self):
        source = self.context.source
        return {
            "version": 1,
            "producer": "ORDERED_CHARACTER_SUBJECT_RESOLVER",
            "identityDecision": "DISTINCT_IDENTITIES",
            "groupId": self.group_id,
            "normalizedName": self.normalized_name,
            "sourceAnalysisJobId": str(self.context.analysis_job_id),
            "settingBookId": str(source.setting_book_id),
            "revisionId": str(source.revision_id),
            "contentHash": source.content_hash,
            "referenceTimeKind": source.reference_time_kind,
            "referenceEpisodeNo": source.reference_episode_no,
            "members": [
                {"targetRef": ref, "profileEvidence": span.wire()} for ref, span in self.members
            ],
            "distinctEvidenceSpans": [span.wire() for span in self.distinct_evidence_spans],
            "parentTargetRef": self.parent_target_ref,
        }


def _persisted_ref(target):
    if target.actual_character_id is not None:
        return f"character:{target.actual_character_id}"
    return target.provisional_subject_key


def has_book_identity_collision(candidates, subjects):
    groups = {}
    for subject in subjects:
        groups.setdefault(normalize_book_group_name(subject.name), set()).add(
            _persisted_ref(subject)
        )
    for index, candidate in enumerate(candidates):
        if candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY:
            groups.setdefault(normalize_book_group_name(candidate.entity_name), set()).add(
                located_discovery_key(candidate) or ("unlocated-discovery", index)
            )
    return any(len(identities) > 1 for identities in groups.values())


def _check_span(span, text):
    if (
        not 0 <= span.start_offset < span.end_offset <= len(text)
        or text[span.start_offset : span.end_offset] != span.quote
    ):
        raise ComparisonValidationError(
            "Identity evidence is not an exact source code-point slice."
        )


def _owned_evidence(candidate, parent, text):
    """Repair only fully unlocated quotes unique inside the typed selected parent."""
    if not candidate.evidence_spans:
        raise ComparisonValidationError("Candidate has no evidence for parent ownership.")
    spans = []
    for evidence in candidate.evidence_spans:
        start, end = evidence.start_offset, evidence.end_offset
        if start is None and end is None:
            local = parent.quote.find(evidence.quote)
            if local < 0 or parent.quote.find(evidence.quote, local + 1) >= 0:
                raise ComparisonValidationError(
                    "Child quote is not unique inside its selected parent."
                )
            start = parent.start_offset + local
            end = start + len(evidence.quote)
        if start is None or end is None:
            raise ComparisonValidationError(
                "Partial child evidence offsets cannot prove ownership."
            )
        checked = BookIdentitySpan(quote=evidence.quote, start_offset=start, end_offset=end)
        _check_span(checked, text)
        if not parent.start_offset <= start < end <= parent.end_offset:
            raise ComparisonValidationError(
                "Candidate evidence is outside its selected parent profile."
            )
        spans.append(
            ExtractedEvidenceSpan(quote=evidence.quote, start_offset=start, end_offset=end)
        )
    return candidate.model_copy(update={"evidence_spans": spans})


def validate_book_identity_groups(
    *, groups, context, targets, selections, candidates, candidate_ids
):
    """Validate whole partitions; hold only settings that cannot prove parent ownership.

    Discovery ownership remains part of the strict partition boundary. Unowned
    settings return no proof and must lose their selected identity in the caller.
    """
    proofs, repaired, unowned = {}, {}, set()
    names, refs_seen = set(), set()
    for group in groups:
        name = normalize_book_group_name(group.normalized_name)
        if name != group.normalized_name or name in names or group.group_ref in refs_seen:
            raise ComparisonValidationError(
                "Identity group name or origin-local reference is invalid."
            )
        names.add(name)
        refs_seen.add(group.group_ref)
        refs = [member.target_ref for member in group.members]
        if len(set(refs)) != len(refs) or any(ref not in targets for ref in refs):
            raise ComparisonValidationError("Identity member references are duplicate or unknown.")
        expected = {
            ref
            for ref, target in targets.items()
            if any(
                normalize_book_group_name(spelling) == name
                for spelling in (target.name, *target.aliases)
            )
        }
        if set(refs) != expected or any(
            normalize_book_group_name(targets[ref].name) != name for ref in refs
        ):
            raise ComparisonValidationError(
                "Identity partition must cover the entire same-name input group."
            )
        actual = [targets[ref].actual_character_id is not None for ref in refs]
        if any(actual) and not all(actual):
            raise ComparisonValidationError(
                "Mixed actual and new identity partitions remain reviewable."
            )
        if not any(actual):
            owned = {
                f"provisional-character:{candidate_ids[index]}": candidate
                for index, candidate in enumerate(candidates.values())
                if candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY
                and normalize_book_group_name(candidate.entity_name) == name
            }
            if any(_persisted_ref(targets[ref]) not in owned for ref in refs):
                raise ComparisonValidationError(
                    "Provisional members must belong to this origin discovery group."
                )
        persisted = [_persisted_ref(targets[ref]) for ref in refs]
        if len(set(persisted)) != len(refs):
            raise ComparisonValidationError("One identity cannot occupy two parent profiles.")
        parents = {member.target_ref: member.profile_evidence for member in group.members}
        for span in (*parents.values(), *group.distinct_evidence_spans):
            _check_span(span, context.source_text)
        ordered = sorted(parents.values(), key=lambda span: span.start_offset)
        if any(left.end_offset > right.start_offset for left, right in zip(ordered, ordered[1:])):
            raise ComparisonValidationError("Identity parent profiles must be disjoint.")
        members = tuple(
            (_persisted_ref(targets[member.target_ref]), member.profile_evidence)
            for member in group.members
        )
        for ref, candidate in candidates.items():
            selected = selections[ref]
            same_name = normalize_book_group_name(candidate.entity_name) == name
            if (
                candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY
                and same_name
                and selected not in parents
            ):
                raise ComparisonValidationError(
                    "Every same-name discovery needs its own resolved parent."
                )
            if selected not in parents:
                continue
            if (
                not same_name
                and candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY
            ):
                raise ComparisonValidationError(
                    "Discovery names cannot be changed by an identity partition."
                )
            if ref in proofs or ref in unowned:
                raise ComparisonValidationError(
                    "A candidate cannot belong to multiple identity groups."
                )
            try:
                located = _owned_evidence(candidate, parents[selected], context.source_text)
            except (ComparisonValidationError, ValidationError):
                if candidate.candidate_kind != SettingCandidateKind.SETTING:
                    raise
                unowned.add(ref)
                continue
            proof = BookIdentityProof(
                context,
                group.group_ref,
                name,
                members,
                tuple(group.distinct_evidence_spans),
                _persisted_ref(targets[selected]),
            )
            repaired[ref], proofs[ref] = located, proof
    return proofs, repaired, unowned
