"""Fixed comparison replies exercise grounding/guards, not model semantics."""

import asyncio
import json
import socket
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.analysis import ordered_context, world_setting_comparator
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
)
from tests.test_ordered_analysis_runtime import RecordingClient


GENERAL_QUOTE = "돌물문의 야간 통행 금지는 계속 적용합니다. 단, 수로관리청 야간 점검반은 이 규칙의 예외입니다."
GENERAL_VALUE = "야간 통행이 금지되며, 수로관리청 야간 점검반에는 예외가 적용된다. 다른 주민의 통행은 허가되지 않는다."
NARROW_VALUE = "붉은 유리 패찰을 소지한 수로관리청 야간 점검반만 야간에 통행할 수 있다."
PROOF_FIELD = "explicit_root_scope_evidence_index"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("This fixed-response regression must not use the network.")

    class Encoding:
        def encode(self, text, **kwargs):
            return list(text.encode("utf-8"))

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(ordered_context.tiktoken, "get_encoding", lambda name: Encoding())
    monkeypatch.setattr(world_setting_comparator, "get_settings", lambda: SimpleNamespace())


def _source(book=False):
    return WorkerAnalysisSource.model_validate({
        "sourceKind": "SETTING_BOOK", "settingBookId": UUID(int=10),
        "settingBookName": "규칙집", "revisionId": UUID(int=11), "revisionNo": 1,
    } if book else {"sourceKind": "EPISODE", "episodeId": UUID(int=12), "episodeNo": 1})


def _candidate(**changes):
    return WorkerWorldSettingComparisonBatchCandidate.model_validate({
        "candidateRef": "C1", "candidateId": UUID(int=1), "subjectName": "돌물문",
        "scopeName": None, "settingName": "야간 통행 규정", "extractedValue": GENERAL_VALUE,
        "temporalScope": "PRESENT", "rawExtractionJson": {"epistemic_status": "ASSERTED"},
        "evidenceSpans": [
            {"quote": GENERAL_QUOTE, "startOffset": 3987, "endOffset": 4041},
            {"quote": "북쪽 발판이나 다른 주민의 통행은 허가하지 않습니다.",
             "startOffset": 4081, "endOffset": 4110},
        ],
        **changes,
    })


def _target(*, provisional=False, protected_paths=()):
    return WorkerWorldSettingComparisonTarget(
        **({"provisional_subject_key": f"provisional-world:{UUID(int=3)}"} if provisional
           else {"world_setting_id": UUID(int=3)}),
        subject_name="돌물문", version=2, protected_paths=list(protected_paths),
        properties=[{
            "scopeName": "남쪽 점검로", "settingName": "야간 통행 규정", "value": NARROW_VALUE,
            "provenance": {"confirmationStatus": "PROVISIONAL", "sourceEpisodeNo": 1,
                           "sourceKind": "EPISODE", "currentValueProtected": False},
        }],
    )


def _reply(mode, **changes):
    return {
        "consolidation_status": "SINGLE", "operation": "ADD", "review_reason": None,
        "target_ref": "T1", "proposed_scope_name": None,
        "proposed_setting_name": "야간 통행 규정", "proposed_value": GENERAL_VALUE,
        "comparison_reason": "일반 통행 금지와 명시된 점검반 예외를 보존하며 남쪽 점검로의 조건도 유지합니다.",
        **({"matched_property_ref": None} if mode == "ordered" else {
            "matched_scope_name": None, "matched_property_name": None,
        }),
        **({"source_candidate_refs": ["C1"], "existing_root_property_names_to_move": []}
           if mode != "single" else {}),
        PROOF_FIELD: 0,
        **changes,
    }


def _compare(mode, *, candidate=None, target=None, reply=None, source=None, candidates=None):
    candidate = candidate or _candidate()
    target = target or _target()
    source = source or _source()
    client = RecordingClient(reply if mode == "single" else {"decisions": [reply]})
    comparator = WorldSettingComparator(
        llm_client=client, model="gpt-5.6-luna", max_attempts=1,
        max_output_tokens=3000, batch_max_output_tokens=16000,
    )
    if mode == "single":
        fields = candidate.model_dump(exclude={"candidate_ref"})
        single = WorkerWorldSettingCandidatePayload(
            **fields, category="LOCATION", work_id=UUID(int=2), source=source,
            source_episode_id=source.episode_id,
        )
        result, raw = asyncio.run(comparator.compare(single, [target]))
    else:
        result, raw = asyncio.run(comparator.compare_batch(
            "LOCATION", candidates or [candidate], [target],
            ordered_context=mode == "ordered", analysis_source=source,
        ))
        result = result.decisions[0]
    return result, raw, client


@pytest.mark.parametrize("mode", ["single", "batch", "ordered"])
@pytest.mark.parametrize("book", [False, True], ids=["episode", "asserted_book"])
def test_explicit_general_root_add_preserves_existing_narrow_exception(mode, book):
    # A return to the blanket null-scope guard loses the explicit general rule.
    target = _target(provisional=mode == "ordered")
    before = target.model_dump(mode="json")
    result, raw, client = _compare(mode, target=target, source=_source(book), reply=_reply(mode))

    assert result.operation == "ADD"
    assert result.review_reason is None
    assert result.target_ref == "T1"
    assert result.matched_scope_name is None and result.matched_property_name is None
    assert result.proposed_scope_name is None
    assert result.proposed_setting_name == "야간 통행 규정"
    assert result.proposed_value == GENERAL_VALUE
    assert target.model_dump(mode="json") == before
    assert target.properties[0].value == NARROW_VALUE
    assert PROOF_FIELD not in json.dumps(raw)
    assert len(client.requests) == 1


@pytest.mark.parametrize("mode", ["single", "batch", "ordered"])
@pytest.mark.parametrize("proof", ["absent", "null", "negative", "out_of_bounds", "unlocated", "bad_length"])
def test_unproved_general_scope_keeps_existing_scope_review(mode, proof):
    reply = _reply(mode)
    candidate = _candidate()
    if proof == "absent":
        reply.pop(PROOF_FIELD)
    elif proof == "null":
        reply[PROOF_FIELD] = None
    elif proof in {"negative", "out_of_bounds"}:
        reply[PROOF_FIELD] = -1 if proof == "negative" else 2
    else:
        candidate = _candidate(evidenceSpans=[{
            "quote": GENERAL_QUOTE,
            "startOffset": None if proof == "unlocated" else 3987,
            "endOffset": None if proof == "unlocated" else 4040,
        }])
    result, raw, _ = _compare(mode, candidate=candidate, reply=reply)

    assert result.operation == "REVIEW_REQUIRED"
    assert result.review_reason == "SCOPE_UNRESOLVED"
    assert result.matched_scope_name == "남쪽 점검로"
    assert result.matched_property_name == "야간 통행 규정"
    assert result.proposed_scope_name is None
    assert result.proposed_value == GENERAL_VALUE
    assert PROOF_FIELD not in json.dumps(raw)


@pytest.mark.parametrize("mode", ["single", "batch", "ordered"])
@pytest.mark.parametrize("operation", ["UPDATE", "MERGE", "EXCLUDE", "REVIEW_REQUIRED"])
def test_general_scope_proof_cannot_upgrade_review_or_cross_scope_mutation(mode, operation):
    changes = {
        "operation": operation,
        "review_reason": "SCOPE_UNRESOLVED" if operation == "REVIEW_REQUIRED" else None,
        **({"matched_property_ref": "T1.P1"} if mode == "ordered" else {
            "matched_scope_name": "남쪽 점검로", "matched_property_name": "야간 통행 규정",
        }),
    }
    if operation in {"UPDATE", "MERGE"}:
        changes.update(proposed_scope_name=None if mode == "ordered" else "남쪽 점검로",
                       proposed_setting_name=None if mode == "ordered" else "야간 통행 규정")
    target = _target()
    before = target.model_dump(mode="json")
    result, _, _ = _compare(mode, target=target, reply=_reply(mode, **changes))

    assert result.operation == "REVIEW_REQUIRED"
    assert result.review_reason == "SCOPE_UNRESOLVED"
    assert result.proposed_scope_name is None
    assert target.model_dump(mode="json") == before


def test_one_evidence_selection_does_not_prove_multiple_sources():
    candidates = [_candidate(), _candidate(candidateRef="C2", candidateId=UUID(int=4))]
    result, _, _ = _compare("ordered", candidates=candidates, reply=_reply(
        "ordered", source_candidate_refs=["C1", "C2"], consolidation_status="MERGED",
    ))

    assert result.operation == "REVIEW_REQUIRED"
    assert result.review_reason == "SCOPE_UNRESOLVED"
    assert result.source_candidate_refs == ["C1", "C2"]


def test_general_scope_proof_does_not_project_conflicting_values():
    candidate = _candidate(extractedValue=GENERAL_VALUE + "\n야간 통행은 모두에게 허용된다.")
    result, _, _ = _compare("ordered", candidate=candidate, reply=_reply(
        "ordered", consolidation_status="CONFLICT", proposed_value=candidate.extracted_value,
    ))

    assert result.operation == "REVIEW_REQUIRED"
    assert result.consolidation_status == "CONFLICT"
    assert result.proposed_value == candidate.extracted_value


@pytest.mark.parametrize("guard", ["nonasserted", "PAST", "HYPOTHETICAL", "UNKNOWN", "protected_root"])
def test_general_scope_proof_cannot_bypass_book_current_guards(guard):
    changes = {}
    if guard == "nonasserted":
        changes["rawExtractionJson"] = {"epistemic_status": "UNRESOLVED_CLAIM"}
    elif guard != "protected_root":
        changes["temporalScope"] = guard
    protected = [{"scopeName": None, "settingName": "야간 통행 규정"}] if guard == "protected_root" else []

    with pytest.raises(ComparisonValidationError):
        _compare("ordered", source=_source(book=True), candidate=_candidate(**changes),
                 target=_target(protected_paths=protected), reply=_reply("ordered"))


@pytest.mark.parametrize("proof", [True, "0", 0.0])
def test_evidence_selection_rejects_coerced_index_types(proof):
    with pytest.raises(ComparisonValidationError):
        _compare("ordered", reply=_reply("ordered", **{PROOF_FIELD: proof}))
