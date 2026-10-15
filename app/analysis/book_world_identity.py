"""Grounded AI identity judgments with conservative, source-owned parent boundaries.

Exact names/quotes validate grounding, not natural-language identity. Independent
named parents without a trusted alias bridge block a contradictory AI selection.
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.schemas import _to_openai_strict_json_schema
from app.analysis.setting_book_evidence import book_sections
from app.llm.protocols import LlmResponseSchema
from app.mappers.world_setting_candidate_mapper import normalize_world_setting_name


class BookWorldIdentityProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_ref: str
    relation: Literal["SAME_ENTITY", "CONTAINS", "RELATED", "DISTINCT", "UNKNOWN"]
    quote: str = Field(min_length=1)


class BookWorldSubjectSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_subject_refs: list[str] = Field(max_length=3)
    ambiguous: bool
    identity_proofs: list[BookWorldIdentityProof] = Field(max_length=3)


BOOK_WORLD_SUBJECT_RESPONSE_SCHEMA = LlmResponseSchema(
    name="book_world_subject_resolution",
    schema=_to_openai_strict_json_schema(BookWorldSubjectSelection.model_json_schema()),
)

BOOK_WORLD_IDENTITY_INSTRUCTIONS = """
이 BOOK 응답은 selected_subject_refs, ambiguous, identity_proofs를 모두 포함합니다.
identity_proofs는 {subject_ref, relation, quote} 객체 목록입니다. relation은 SAME_ENTITY,
CONTAINS, RELATED, DISTINCT, UNKNOWN 중 하나입니다. 포함/소재/관련 관계는 SAME_ENTITY가
아닙니다. 선택한 다른 이름의 대상마다 그 대상과 원문 주체가 실제 동일하다는 근거가
필요합니다. quote는 candidate 또는 그 target의 입력 evidence 연속 원문을 그대로 복사하고
양쪽 실제 이름/그 target에 공급된 별칭을 포함하세요. 다른 대상의 별칭을 빌리지 마세요.
서로 독립된 주체 제목의 항목을 관계가 있다는 이유로 합치지 마세요. 두 이름이 문서 어딘가에
나오거나 표식을 SAME_ENTITY라고 쓰는 것만으로 충분하지 않습니다. 제목 문맥과 충돌하면
새 주체인지 또는 모호한 연결인지 원문으로 다시 판단하세요. 공급된 별칭에 없는 다른 이름을
선택할 때만 proof가 필수이며, 같은 이름도 강제 연결하지 않습니다.
신규: {"selected_subject_refs":[],"ambiguous":false,"identity_proofs":[]}
모호: {"selected_subject_refs":[],"ambiguous":true,"identity_proofs":[]}
같은 이름의 기존 대상: {"selected_subject_refs":["S1"],"ambiguous":false,"identity_proofs":[]}
""".strip()


class BookWorldIdentityError(ComparisonValidationError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _named_parents(spans, names) -> set[str]:
    keys = {normalize_world_setting_name(name) for name in names}
    parents = set()
    for span in spans:
        for section in book_sections(span.quote):
            title = section.subject_title()
            if title is not None and normalize_world_setting_name(title) in keys:
                parents.add(normalize_world_setting_name(title))
    return parents


def _mentions(text: str, name: str) -> list[tuple[int, int]]:
    result = []
    start = text.find(name)
    while start >= 0:
        result.append((start, start + len(name)))
        start = text.find(name, start + 1)
    return result


def _two_distinct_mentions(quote: str, candidate_name: str, target_names) -> bool:
    quote = normalize_world_setting_name(quote)
    candidate_name = normalize_world_setting_name(candidate_name)
    for name in {normalize_world_setting_name(name) for name in target_names} - {candidate_name}:
        for first in _mentions(quote, candidate_name):
            for second in _mentions(quote, name):
                if first[1] <= second[0] or second[1] <= first[0]:
                    return True
    return False


def validate_book_world_selection(selection, candidate, references) -> None:
    refs = selection.selected_subject_refs
    if len(refs) != len(set(refs)) or any(ref not in references for ref in refs):
        raise BookWorldIdentityError("BOOK_IDENTITY_REFERENCE_INVALID")
    proofs = {proof.subject_ref: proof for proof in selection.identity_proofs}
    if len(proofs) != len(selection.identity_proofs) or any(ref not in refs for ref in proofs):
        raise BookWorldIdentityError("BOOK_IDENTITY_PROOF_INVALID")
    if selection.ambiguous:
        return  # Valid refs are discarded by the caller; no identity is established.
    candidate_key = normalize_world_setting_name(candidate.subject_name)
    candidate_parents = _named_parents(candidate.evidence_spans, [candidate.subject_name])
    for ref in refs:
        subject = references[ref]
        names = [subject.subject_name, *subject.aliases]
        supplied_keys = {normalize_world_setting_name(name) for name in names}
        proof = proofs.get(ref)
        supplied_evidence = [span.quote for span in candidate.evidence_spans + subject.identity_evidence]
        if proof is not None and (proof.relation != "SAME_ENTITY" or not proof.quote.strip()
                or not any(proof.quote in quote for quote in supplied_evidence)):
            raise BookWorldIdentityError("BOOK_IDENTITY_PROOF_INVALID")
        if candidate_key in supplied_keys:
            # Proof omission is allowed; an explicit contradictory proof is not.
            continue
        if proof is None or not _two_distinct_mentions(proof.quote, candidate.subject_name, names):
            raise BookWorldIdentityError("BOOK_IDENTITY_PROOF_INVALID")
        target_parents = _named_parents(subject.identity_evidence, names)
        if candidate_parents and target_parents and candidate_parents.isdisjoint(target_parents):
            # This is intentionally conservative. It rejects a grounded but
            # self-tagged containment quote; it is not an alias-word dictionary.
            raise BookWorldIdentityError("BOOK_PARENT_IDENTITY_CONFLICT")


def book_world_identity_retry_prompt(original: str, error: Exception) -> str:
    payload = json.loads(original)
    payload["validation_feedback"] = {
        "reason_code": error.code if isinstance(error, BookWorldIdentityError) else "BOOK_IDENTITY_RESPONSE_INVALID",
        "correction": (
            "입력의 독립 주체 제목과 선택한 target의 실제 이름/공급 별칭을 다시 확인하세요. "
            "소재/포함/관련 관계로 연결하지 말고 동일 주체의 실제 연속 원문으로 target별 proof를 "
            "작성하세요. 근거 없는 선택을 유지하지 말고 명확한 신규 주체 또는 모호한 연결로 "
            "구분하세요. selected_subject_refs/ambiguous/identity_proofs 전체를 반환하세요."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)
