"""A shared full name is not evidence that two discovered people are identical."""

import asyncio
from uuid import uuid4

import pytest

from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.ordered_character_subjects import resolve_ordered_character_subjects
from app.services.setting_candidate_service import prepare_setting_candidates
from tests.test_ordered_subject_rules import (
    RecordingClient,
    _discovery,
    _known,
    _setting,
)
from tests.test_ordered_subject_rules import (
    no_network as no_network,
)
from tests.test_setting_book_reference_time import book_source

FIRST = "한서린: 기록관, 27세, 별칭 은등."
SECOND = "한서린: 선원, 34세."


def resolve(candidates, response=None, *, book=False, subjects=()):
    provider = RecordingClient(response)
    resolved, updates = asyncio.run(resolve_ordered_character_subjects(
        client=provider, model="offline-test-model", max_attempts=1, max_output_tokens=3000,
        context=SubjectResolutionChunkContext(None, f"# 인물 설정\n- {FIRST}\n- {SECOND}", None),
        candidates=candidates, subjects=list(subjects), episode_no=None if book else 2,
        analysis_source=book_source() if book else None,
    ))
    return resolved, updates, provider


@pytest.mark.parametrize("book", [False, True])
def test_same_fullname_preserves_two_discovery_anchors_and_ambiguous_setting(book):
    candidates = [_discovery("한서린", quote=FIRST), _discovery("한서린", quote=SECOND),
        _setting("한서린", raw="은등", quote=FIRST), _setting("한서린", raw="선원 한서린", quote=SECOND),
        _setting("한서린", quote="한서린의 레벨은 12였다.")]
    response = {"resolutions": [{"candidate_ref": ref, "target_ref": target} for ref, target in [
        ("C1", "D1"), ("C2", "D2"), ("C3", "D1"), ("C4", "D2"), ("C5", None)]]}
    resolved, updates, provider = resolve(candidates, response, book=book)
    assert len(resolved) == 5 and len(updates) == 2 and len(provider.requests) == 1
    first, second = [item.binding.provisional_subject_key for item in resolved[:2]]
    assert first != second
    assert [item.binding.provisional_subject_key for item in resolved[2:]] == [first, second, None]
    assert resolved[-1].binding.match_status == "AMBIGUOUS"
    assert {item.evidence for item in updates} == {(FIRST,), (SECOND,)}
    assert {item.name for item in updates} == {"한서린"}


@pytest.mark.parametrize("book", [False, True])
def test_same_person_repeated_discoveries_can_share_an_evidence_verified_anchor(book):
    candidates = [_discovery("한서린", quote=FIRST),
        _discovery("한서린", quote="기록관 한서린은 오늘도 장부를 썼다."), _setting("한서린", quote=FIRST)]
    response = {"resolutions": [{"candidate_ref": f"C{index}", "target_ref": "D1"} for index in range(1, 4)]}
    resolved, updates, provider = resolve(candidates, response, book=book)
    assert len(updates) == 1 and len(provider.requests) == 1
    assert len({item.binding.provisional_subject_key for item in resolved}) == 1
    assert set(updates[0].evidence) == {item.evidence_spans[0].quote for item in candidates[:2]}


def test_exact_same_located_discovery_from_overlapping_chunks_keeps_one_anchor():
    first = _discovery("한서린", quote=FIRST)
    first.evidence_spans[0].start_offset = 10
    first.evidence_spans[0].end_offset = 10 + len(FIRST)
    second = first.model_copy(deep=True, update={"source_chunk_id": uuid4()})
    resolved, updates, provider = resolve([first, second])
    assert len(updates) == 1 and provider.requests == []
    assert resolved[0].binding.provisional_subject_key == resolved[1].binding.provisional_subject_key


def test_uncertain_same_name_discoveries_stay_reviewable_without_forced_identity():
    candidates = [_discovery("한서린", quote=FIRST), _discovery("한서린", quote=SECOND)]
    response = {"resolutions": [{"candidate_ref": f"C{index}", "target_ref": None} for index in (1, 2)]}
    resolved, updates, provider = resolve(candidates, response, book=True)
    assert len(resolved) == 2 and not updates and len(provider.requests) == 1
    assert all(item.binding.match_status == "AMBIGUOUS" for item in resolved)
    assert [item.candidate.evidence_spans[0].quote for item in resolved] == [FIRST, SECOND]


@pytest.mark.parametrize("target", ["K1", "D1"])
def test_same_fullname_as_registered_person_requires_identity_evidence(target):
    known = _known("한서린")
    candidates = [_discovery("한서린", quote=SECOND), _setting("한서린", quote=SECOND)]
    response = {"resolutions": [{"candidate_ref": f"C{index}", "target_ref": target} for index in (1, 2)]}
    resolved, updates, provider = resolve(candidates, response, book=True, subjects=[known])
    assert len(provider.requests) == 1 and len(updates) == 1
    assert all(item.binding.actual_character_id == (known.actual_character_id if target == "K1" else None)
               for item in resolved)
    assert (updates[0].provisional_subject_key is not None) == (target == "D1")


def test_legacy_save_does_not_drop_different_discovery_evidence_with_same_fullname():
    candidates = [_discovery("한서린", quote=FIRST), _discovery("한서린", quote=SECOND)]
    prepared = prepare_setting_candidates(candidates, [])
    assert [item.source_index for item in prepared] == [0, 1]
    assert [item.candidate.evidence_spans[0].quote for item in prepared] == [FIRST, SECOND]


def test_discovery_cannot_bind_to_an_anchor_whose_owner_stays_unresolved():
    from app.analysis.exceptions import ComparisonValidationError
    candidates = [_discovery("한서린", quote=FIRST), _discovery("한서린", quote=SECOND)]
    response = {"resolutions": [{"candidate_ref": "C1", "target_ref": None},
                                {"candidate_ref": "C2", "target_ref": "D1"}]}
    with pytest.raises(ComparisonValidationError):
        resolve(candidates, response, book=True)


@pytest.mark.parametrize("same_source,expected_count", [(False, 2), (True, 1)])
def test_discovery_deduplication_requires_the_same_frozen_source(same_source, expected_count):
    first = _discovery("한서린", quote=FIRST)
    first.evidence_spans[0].start_offset = 10
    first.evidence_spans[0].end_offset = 10 + len(FIRST)
    second = first.model_copy(deep=True, update={"source_chunk_id": uuid4()})
    source = (uuid4(), "v1")
    identities = [source, source if same_source else (uuid4(), "v1")]
    prepared = prepare_setting_candidates([first, second], [], source_identities=identities)
    assert len(prepared) == expected_count
