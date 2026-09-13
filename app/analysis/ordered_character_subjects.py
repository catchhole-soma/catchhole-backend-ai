"""Resolve ordered-mode character identities without inventing real Backend IDs."""

from dataclasses import dataclass, replace
import json
import logging
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis.character_name_resolver import (
    is_usable_subject_resolution_name,
    normalize_character_name,
)
from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.json_response import request_validated_model
from app.analysis.ordered_context import (
    BoundedOrderedClient,
    ORDERED_STATE_INSTRUCTIONS,
    OrderedExtractionContext,
    ordered_provenance_data,
    ordered_reference_data,
)
from app.analysis.schemas import ExtractedSettingCandidate
from app.domain.enums import AnalysisFailureCode, SettingCandidateKind, SettingCandidateMatchStatus
from app.exceptions.failure_classification import (
    COMPARISON_CANDIDATE_FAILURE_CODES, comparison_failure_code, is_candidate_comparison_failure,
)
from app.llm.protocols import LlmResponseSchema, TextGenerationClient
from app.schemas.worker import WorkerAnalysisActiveCharacterStatusPayload
from app.schemas.analysis_context import WorkerAnalysisReference

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderedSubjectTarget:
    name: str
    actual_character_id: UUID | None = None
    provisional_subject_key: str | None = None
    aliases: tuple[str, ...] = ()
    source_episode_no: int | None = None
    evidence: tuple[str, ...] = ()
    active_statuses: tuple[WorkerAnalysisActiveCharacterStatusPayload, ...] = ()
    review_source: str | None = None

    def __post_init__(self) -> None:
        if (self.actual_character_id is None) == (self.provisional_subject_key is None):
            raise ValueError("A subject must have exactly one actual or provisional identity.")


@dataclass(frozen=True)
class OrderedCandidateBinding:
    candidate_id: UUID
    actual_character_id: UUID | None = None
    provisional_subject_key: str | None = None
    match_status: SettingCandidateMatchStatus = SettingCandidateMatchStatus.AMBIGUOUS
    subject_failure_code: AnalysisFailureCode | None = None

    def __post_init__(self) -> None:
        if self.subject_failure_code is not None and (
            self.subject_failure_code not in COMPARISON_CANDIDATE_FAILURE_CODES
            or self.actual_character_id is not None or self.provisional_subject_key is not None
            or self.match_status != SettingCandidateMatchStatus.AMBIGUOUS
        ):
            raise ValueError("Subject resolution failure cannot be applied to a character identity.")


@dataclass(frozen=True)
class OrderedResolvedCandidate:
    candidate: ExtractedSettingCandidate
    binding: OrderedCandidateBinding


@dataclass
class OrderedSubjectResolutionMetrics:
    llm_call_count: int = 0
    llm_resolved_count: int = 0
    llm_unresolved_count: int = 0
    llm_failed_count: int = 0


class _Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ref: str = Field(pattern=r"^C[1-9][0-9]*$")
    target_ref: str | None = Field(pattern=r"^[KD][1-9][0-9]*$")


class _ResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolutions: list[_Resolution]


ORDERED_CHARACTER_SUBJECT_RESPONSE_SCHEMA = LlmResponseSchema(
    name="ordered_character_subject_resolution",
    schema=_ResolutionResult.model_json_schema(),
)

_RESPONSE_FORMAT_INSTRUCTIONS = """
응답은 resolutions 배열을 가진 JSON 객체 하나로만 반환하세요.
각 항목에는 candidate_ref와 target_ref를 모두 명시하고 다른 필드는 추가하지 마세요.
candidate_ref에는 입력 candidates의 ref를 그대로 쓰되, 출력 필드 이름은 ref가 아니라
candidate_ref입니다. target_ref에는 입력 targets에 있는 K* 또는 D*만 쓰세요.
구별할 수 없을 때도 target_ref를 생략하지 말고 JSON null을 명시하세요.
형식 예시: {"resolutions":[{"candidate_ref":"C1","target_ref":"D1"}]}
모호한 경우의 형식 예시: {"resolutions":[{"candidate_ref":"C1","target_ref":null}]}
예시는 출력 구조만 보여줍니다. 실제 입력의 모든 C*를 정확히 한 번씩 포함하세요.
""".strip()

_SAFE_RESPONSE_ERROR_TYPES = frozenset({
    "missing", "extra_forbidden", "string_type", "string_pattern_mismatch",
    "list_type", "model_type", "dict_type",
})


def _safe_response_field(location: tuple) -> str:
    if not location or location[0] != "resolutions":
        return "response"
    if len(location) < 2 or type(location[1]) is not int:
        return "resolutions"
    if len(location) >= 3 and location[2] in {"candidate_ref", "target_ref"}:
        return f"resolutions[].{location[2]}"
    return "resolutions[]"


def _build_subject_retry_prompt(original: str, error: Exception) -> str:
    """Append only fixed schema paths/types, never rejected values or invented keys."""
    issues: list[dict[str, str]] = []
    if isinstance(error, ValidationError):
        for item in error.errors(include_url=False, include_context=False, include_input=False):
            issue = {
                "field": _safe_response_field(tuple(item.get("loc") or ())),
                "type": (item["type"] if item.get("type") in _SAFE_RESPONSE_ERROR_TYPES
                         else "schema_invalid"),
            }
            if issue not in issues:
                issues.append(issue)
            if len(issues) == 8:
                break
    else:
        reason = ("json_invalid" if isinstance(error, json.JSONDecodeError)
                  else "subject_reference_invalid" if isinstance(error, ComparisonValidationError)
                  else "schema_invalid")
        issues.append({"field": "response", "type": reason})
    feedback = json.dumps({"issues": issues}, ensure_ascii=False)
    return (
        original + "\n\nresponse_format_feedback:\n" + feedback
        + "\n응답 형식과 입력 참조를 다시 확인하세요. resolutions의 모든 항목에 "
        "candidate_ref와 target_ref를 명시하고, 입력의 모든 C*를 정확히 한 번 반환하세요. "
        "대상은 입력 K*/D* 또는 명시적 null만 허용합니다. 실패 응답을 복사하지 마세요."
    )


def initial_ordered_subjects(context: OrderedExtractionContext) -> list[OrderedSubjectTarget]:
    return [
        OrderedSubjectTarget(
            name=item.name,
            actual_character_id=item.character_id,
            aliases=tuple(item.aliases),
            evidence=tuple(span.quote for span in item.identity_evidence),
            active_statuses=tuple(item.active_statuses),
            review_source=item.provenance.review_source if item.provenance else None,
        )
        for item in context.known_characters
    ] + [
        OrderedSubjectTarget(
            name=item.name,
            provisional_subject_key=item.provisional_subject_key,
            aliases=tuple(item.aliases),
            source_episode_no=item.source_episode_no,
            evidence=tuple(span.quote for span in item.identity_evidence),
            active_statuses=tuple(item.active_statuses),
        )
        for item in context.provisional_characters
    ]


def _names_overlap(left: str, right: str) -> bool:
    return left == right or (
        min(len(left), len(right)) >= 2 and (left in right or right in left)
    )


def _matching_targets(name: str | None, targets: dict[str, OrderedSubjectTarget]) -> set[str]:
    if not is_usable_subject_resolution_name(name):
        return set()
    normalized = normalize_character_name(name)
    return {
        ref for ref, target in targets.items()
        if any(
            is_usable_subject_resolution_name(spelling)
            and _names_overlap(normalized, normalize_character_name(spelling))
            for spelling in (target.name, *target.aliases)
        )
    }


def _rule_selection(candidate: ExtractedSettingCandidate,
                    targets: dict[str, OrderedSubjectTarget]) -> str | None:
    matches = _matching_targets(candidate.entity_name, targets)
    if len(matches) != 1:
        return None
    selected = next(iter(matches))
    if candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY:
        # A father's name in "Kenik's son Serum" is not the discovered identity.
        return selected
    raw_matches = _matching_targets(candidate.raw_entity_mention, targets)
    if raw_matches and raw_matches != {selected}:
        return None
    return selected


def _has_adjacent_context(context: SubjectResolutionChunkContext) -> bool:
    current = context.current_chunk_text.strip()
    return any(text and text.strip() and text.strip() not in current
               for text in (context.previous_chunk_text, context.next_chunk_text))


async def resolve_ordered_character_subjects(
    *,
    client: TextGenerationClient,
    model: str,
    max_attempts: int,
    max_output_tokens: int,
    context: SubjectResolutionChunkContext,
    candidates: list[ExtractedSettingCandidate],
    subjects: list[OrderedSubjectTarget],
    episode_no: int,
    unresolved_references: tuple[WorkerAnalysisReference, ...] = (),
    metrics: OrderedSubjectResolutionMetrics | None = None,
    continue_on_candidate_failure: bool = False,
) -> tuple[list[OrderedResolvedCandidate], list[OrderedSubjectTarget]]:
    """Bind names locally; only unresolved settings with additional text need an LLM.

    Name matching never drops discoveries: their original spelling and evidence are
    needed by Backend when accumulating aliases. All unbound candidates stay reviewable.
    """
    if not candidates:
        return [], []
    metrics = metrics if metrics is not None else OrderedSubjectResolutionMetrics()
    candidate_ids = [uuid4() for _ in candidates]
    targets = {f"K{index}": item for index, item in enumerate(subjects, 1)}

    # Allocate one anchor per distinct new identity before binding any settings.
    # A short spelling can share its one longer discovery, but never arbitrarily
    # chooses between two longer names. Unknown/pronoun discoveries get no anchor.
    new_names: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        if (candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY
                and is_usable_subject_resolution_name(candidate.entity_name)
                and not _matching_targets(candidate.entity_name, targets)):
            new_names.setdefault(normalize_character_name(candidate.entity_name), index)
    longest_names = {
        name for name in new_names
        if not any(name != other and len(name) >= 2 and name in other for other in new_names)
    }
    for longest in sorted(longest_names, key=new_names.get):
        group = [
            index for name, index in new_names.items()
            if {other for other in longest_names if _names_overlap(name, other)} == {longest}
        ]
        index = min(group)
        candidate = candidates[index]
        targets[f"D{index + 1}"] = OrderedSubjectTarget(
            name=candidate.entity_name,
            provisional_subject_key=f"provisional-character:{candidate_ids[index]}",
            aliases=tuple(candidates[item].entity_name for item in group if item != index),
            source_episode_no=episode_no,
            evidence=tuple(span.quote for span in candidate.evidence_spans),
        )

    # One registered short name must not absorb competing new full names from
    # this chunk. Already registered aliases are explicit identity evidence.
    # Shorter parts of a registered full name are not competing expansions.
    expansions: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.candidate_kind != SettingCandidateKind.CHARACTER_DISCOVERY:
            continue
        matches = _matching_targets(candidate.entity_name, targets)
        if len(matches) != 1:
            continue
        ref = next(iter(matches))
        name = normalize_character_name(candidate.entity_name)
        target = targets[ref]
        registered_names = {
            normalize_character_name(spelling) for spelling in (target.name, *target.aliases)
            if is_usable_subject_resolution_name(spelling)
        }
        if (ref.startswith("K")
                and not any(name in spelling for spelling in registered_names)
                and any(len(spelling) >= 2 and spelling in name for spelling in registered_names)):
            expansions.setdefault(ref, set()).add(name)
    competing_refs = {
        ref for ref, names in expansions.items()
        if any(not _names_overlap(left, right) for left in names for right in names)
    }

    selections = {
        f"C{index}": _rule_selection(candidate, targets)
        for index, candidate in enumerate(candidates, 1)
    }
    selections = {ref: selected if selected not in competing_refs else None
                  for ref, selected in selections.items()}
    fallback = [
        (f"C{index}", candidate)
        for index, candidate in enumerate(candidates, 1)
        if candidate.candidate_kind == SettingCandidateKind.SETTING
        and selections[f"C{index}"] is None
    ]
    subject_failures: dict[str, AnalysisFailureCode] = {}
    if fallback and targets and _has_adjacent_context(context):
        prompt = {
            "context": {
                "previous_chunk": context.previous_chunk_text,
                "current_chunk": context.current_chunk_text,
                "next_chunk": context.next_chunk_text,
            },
            "targets": [
                {
                    "ref": ref,
                    "name": target.name,
                    "aliases": list(target.aliases),
                    "confirmation_status": (
                        "CONFIRMED" if target.actual_character_id else "PROVISIONAL"
                    ),
                    "source_episode_no": target.source_episode_no,
                    **({"review_source": target.review_source} if target.review_source else {}),
                    "evidence": list(target.evidence),
                    "active_statuses": [
                        {"fact_key": status.fact_key, "fact_value": status.fact_value,
                         **ordered_provenance_data(status.provenance)}
                        for status in target.active_statuses
                    ],
                }
                for ref, target in targets.items()
            ],
            "candidates": [
                {"ref": ref, "candidate_kind": candidate.candidate_kind,
                 "entity_name": candidate.entity_name,
                 "raw_entity_mention": candidate.raw_entity_mention,
                 "evidence": [span.quote for span in candidate.evidence_spans]}
                for ref, candidate in fallback
            ],
            "unresolved_references": ordered_reference_data(unresolved_references),
        }
        expected_refs = {ref for ref, _ in fallback}

        def validate(result: _ResolutionResult) -> None:
            refs = [item.candidate_ref for item in result.resolutions]
            if len(refs) != len(set(refs)) or set(refs) != expected_refs:
                raise ComparisonValidationError("Ordered subject resolution coverage is invalid.")
            if any(item.target_ref is not None and item.target_ref not in targets
                   for item in result.resolutions):
                raise ComparisonValidationError("Unknown ordered subject target reference.")

        metrics.llm_call_count += 1
        try:
            response = await request_validated_model(
                client=BoundedOrderedClient(client),
                response_model=_ResolutionResult,
                system_prompt=(
                    ORDERED_STATE_INSTRUCTIONS + "\n\n"
                    "이름 규칙으로 연결하지 못한 설정 후보만 다시 판단합니다. "
                    "추가된 앞뒤 청크와 현재 청크의 실제 근거로 같은 인물을 확인하세요. "
                    "K*는 앞서 알려진 대상이며 D*는 이번 청크에서 발견한 대상입니다. "
                    "이름이 비슷하거나 목록에 한 명뿐이라는 이유로 선택하지 마세요. "
                    "대상을 특정할 수 없으면 target_ref=null로 남기세요. "
                    "주어진 후보와 대상만 사용하고, 설정과 근거를 새로 만들지 마세요.\n\n"
                    + _RESPONSE_FORMAT_INSTRUCTIONS
                ),
                user_prompt=json.dumps(prompt, ensure_ascii=False),
                model=model,
                max_output_tokens=max_output_tokens,
                max_attempts=max_attempts,
                prompt_cache_key="ordered-character-subject-resolution:v3-adjacent-only",
                operation_name="Ordered character subject resolution",
                logger=logger,
                validate_model=validate,
                retry_user_prompt_builder=_build_subject_retry_prompt,
                response_schema=ORDERED_CHARACTER_SUBJECT_RESPONSE_SCHEMA,
            )
        except Exception as exc:
            if not (continue_on_candidate_failure and is_candidate_comparison_failure(exc)):
                raise
            failure_code = comparison_failure_code(exc)
            subject_failures = {ref: failure_code for ref, _ in fallback}
            metrics.llm_failed_count += len(fallback)
            logger.warning("Character subject enhancement deferred. candidate_count=%s failure_code=%s",
                           len(fallback), failure_code)
        else:
            selections.update({item.candidate_ref: item.target_ref for item in response.resolutions})
            metrics.llm_resolved_count += sum(item.target_ref is not None for item in response.resolutions)
            metrics.llm_unresolved_count += sum(item.target_ref is None for item in response.resolutions)

    subject_updates: dict[str, OrderedSubjectTarget] = {}
    for index, candidate in enumerate(candidates, 1):
        if candidate.candidate_kind != SettingCandidateKind.CHARACTER_DISCOVERY:
            continue
        selected_ref = selections[f"C{index}"]
        if selected_ref is None:
            continue
        selected = subject_updates.get(selected_ref, targets[selected_ref])
        aliases = list(selected.aliases)
        if candidate.entity_name != selected.name and candidate.entity_name not in aliases:
            aliases.append(candidate.entity_name)
        evidence = tuple(dict.fromkeys((*selected.evidence, *(span.quote for span in candidate.evidence_spans))))
        subject_updates[selected_ref] = replace(selected, aliases=tuple(aliases), evidence=evidence)

    resolved: list[OrderedResolvedCandidate] = []
    for index, candidate in enumerate(candidates):
        selected_ref = selections[f"C{index + 1}"]
        selected = targets.get(selected_ref) if selected_ref else None
        resolved.append(
            OrderedResolvedCandidate(
                candidate=candidate,
                binding=OrderedCandidateBinding(
                    candidate_id=candidate_ids[index],
                    actual_character_id=selected.actual_character_id if selected else None,
                    provisional_subject_key=selected.provisional_subject_key if selected else None,
                    match_status=(SettingCandidateMatchStatus.MATCHED if selected
                                  else SettingCandidateMatchStatus.AMBIGUOUS),
                    subject_failure_code=subject_failures.get(f"C{index + 1}"),
                ),
            )
        )
    return resolved, list(subject_updates.values())


def merge_ordered_subjects(
    subjects: list[OrderedSubjectTarget], updates: list[OrderedSubjectTarget],
) -> None:
    """Replace enriched identities in place instead of duplicating K* targets."""
    for updated in updates:
        identity = (updated.actual_character_id, updated.provisional_subject_key)
        index = next((index for index, item in enumerate(subjects)
                      if (item.actual_character_id, item.provisional_subject_key) == identity), None)
        if index is None:
            subjects.append(updated)
        else:
            subjects[index] = updated
