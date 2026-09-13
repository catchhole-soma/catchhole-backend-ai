"""Only repeated extractions of the same located fact are deduplicated."""
from uuid import uuid4

import pytest

from app.analysis.ordered_character_subjects import OrderedCandidateBinding
from app.domain.enums import SettingCandidateMatchStatus
from app.services.ordered_analysis_fence import OrderedCandidateWriteContext
from app.services.setting_candidate_service import SettingCandidateSaveItem, SettingCandidateService
from tests.test_ordered_analysis_runtime import FenceRepository, FenceSession, _input_context
from tests.test_setting_candidate_service import _candidate


def save_pair(*, provisional=False, change=None):
    ctx = OrderedCandidateWriteContext(uuid4(), _input_context(), uuid4(), 'source', 'v1', 1)
    binding = OrderedCandidateBinding(candidate_id=uuid4(),
        actual_character_id=None if provisional else uuid4(),
        provisional_subject_key=f'provisional-character:{uuid4()}' if provisional else None,
        match_status=SettingCandidateMatchStatus.MATCHED)
    first = _candidate().model_copy(update={'confidence': 0.6})
    first.evidence_spans[0].start_offset = 20
    first.evidence_spans[0].end_offset = 40
    second = first.model_copy(deep=True, update={'source_chunk_id': uuid4(), 'confidence': 0.9})
    from dataclasses import replace
    second_binding = replace(binding, candidate_id=uuid4())
    if change == 'position':
        second.evidence_spans[0].start_offset = 80
        second.evidence_spans[0].end_offset = 100
    elif change == 'unknown_position':
        first.evidence_spans[0].start_offset = second.evidence_spans[0].start_offset = None
    elif change == 'identity':
        second_binding = replace(second_binding, actual_character_id=uuid4())
    elif change == 'unresolved':
        binding = replace(binding, actual_character_id=None, match_status=SettingCandidateMatchStatus.AMBIGUOUS)
        second_binding = replace(second_binding, actual_character_id=None, match_status=SettingCandidateMatchStatus.AMBIGUOUS)
    elif change == 'value':
        second.value_json = {'value': 9999}
    elif change == 'evidence':
        second.evidence_spans[0].quote = '다른 시점의 근거'
    items = [SettingCandidateSaveItem(ctx.episode_id, 'source', first, binding),
             SettingCandidateSaveItem(ctx.episode_id, 'source', second, second_binding)]
    original = [item.candidate.model_dump() for item in items]
    session = FenceSession(True)
    saved = SettingCandidateService(lambda: session, FenceRepository).replace_candidates_for_analysis_job(
        uuid4(), uuid4(), items, [], ctx)
    assert [item.candidate.model_dump() for item in items] == original
    assert session.events == ['fence', 'delete', 'save', 'commit']
    return saved, second_binding


@pytest.mark.parametrize('provisional', [False, True])
def test_overlapping_chunks_keep_one_located_fact_and_stronger_evidence(provisional):
    saved, winner = save_pair(provisional=provisional)
    assert len(saved) == 1
    assert saved[0].id == winner.candidate_id
    assert float(saved[0].confidence) == 0.9
    assert saved[0].provisional_subject_key == winner.provisional_subject_key


@pytest.mark.parametrize('change', ['position', 'unknown_position', 'identity', 'unresolved', 'value', 'evidence'])
def test_equal_names_do_not_erase_distinct_or_uncertain_facts(change):
    saved, _ = save_pair(change=change)
    assert len(saved) == 2
