"""Synthetic GH287 frozen reference selection and first-stage hints."""
from uuid import uuid4

from app.analysis.pending_reference_projection import (
    PendingSubjectScope,
    episode_character_hints,
    select_pending_references,
)
from app.analysis.setting_extractor import CharacterSettingSchemaHint
from app.schemas.analysis_context import WorkerAnalysisReference


def ref(name='민수', domain='characters', **selection):
    return WorkerAnalysisReference(domain=domain, subject_name=name, source_episode_no=1,
        reason='보류 이유', setting_name='부상', value='다리 골절',
        evidence_spans=[{'quote': '이전 원문 근거'}], selection=selection or None)


def test_metadata_is_frozen_transport_only_and_false_survives():
    subject_id = uuid4()
    item = ref(actualSubjectIds=[subject_id], factType='STATUS', valueJson={'active': False})
    assert item.selection.actual_subject_ids == (subject_id,)
    assert item.selection.value_json['active'] is False
    assert 'selection' not in item.prompt_data()
    assert str(subject_id) not in str(item.prompt_data())


def test_identity_mismatch_overrides_names_and_cross_slot_references_survive():
    subject_id, other_id = uuid4(), uuid4()
    items = (ref(actualSubjectIds=[other_id]), ref(actualSubjectIds=[subject_id], factType='PROFILE'),
             ref(), ref('다른 인물'), ref('민수', 'worldSettings'), ref(None))
    scope = PendingSubjectScope('characters', actual_subject_ids=(subject_id,), names=('민수',))
    assert select_pending_references(items, (scope,)) == (items[1], items[2])
    assert len(items) == 6


def test_alias_raw_mention_and_provisional_link_and_unknown_category():
    items = (ref('별칭', rawSubjectMention='민수'), ref(None, provisionalSubjectKeys=['provisional:test']),
             ref('서울', 'worldSettings', worldCategory='LOCATION'), ref('서울', 'worldSettings'),
             ref('서울', 'worldSettings', worldCategory='ORGANIZATION'))
    scopes = (PendingSubjectScope('characters', names=('민수',), provisional_subject_keys=('provisional:test',)),
              PendingSubjectScope('worldSettings', names=('서울',), world_category='LOCATION'))
    assert select_pending_references(items, scopes) == items[:4]


def test_hints_preserve_ended_unknown_and_do_not_guess_profile_as_status():
    schema = (CharacterSettingSchemaHint(schema_key='injury', display_name='부상', attribute_pattern=None,
        aliases=('부상',), value_type='TEXT', canonical_fact_type='STATUS'),)
    items = (ref(factType='STATUS', canonicalFactKey='injury', valueJson={'active': False}), ref(),
             ref('다른 인물', factType='PROFILE'))
    hints = episode_character_hints(items, schema)
    assert hints['names'] == ['민수', '다른 인물']
    assert [item['active'] for item in hints['statuses']] == [False, None]
    assert '이전 원문 근거' not in str(hints) and '보류 이유' not in str(hints)
    assert all(item['confirmation_status'] == 'UNRESOLVED' for item in hints['statuses'])


def test_identical_linked_status_hints_deduplicate_without_merging_homonyms_or_active_states():
    owner, other = uuid4(), uuid4()
    first = ref(actualSubjectIds=[owner], factType='STATUS', canonicalFactKey='status.부상', valueJson={'active': True})
    other_person = ref(actualSubjectIds=[other], factType='STATUS', canonicalFactKey='status.부상', valueJson={'active': True})
    ended = ref(actualSubjectIds=[owner], factType='STATUS', canonicalFactKey='status.부상', valueJson={'active': False})
    statuses = episode_character_hints((first, first, other_person, ended), ())['statuses']
    assert [item['active'] for item in statuses] == [True, True, False]


def test_legacy_status_pattern_schema_retains_unknown_and_book_origin_does_not_change_episode_selection():
    from app.analysis.pending_reference_projection import character_comparison_scope
    schema = (CharacterSettingSchemaHint('statuses.condition', '상태', 'status.*', (), 'JSON'),)
    legacy = ref().model_copy(update={'setting_name': 'status.부상'})
    assert episode_character_hints((legacy,), schema)['statuses'][0]['active'] is None
    key = f'provisional-character:{uuid4()}'
    linked = ref('이전 표기', provisionalSubjectKeys=[key])
    scope = character_comparison_scope('현재 표기', (), {'provisionalSubjectKey': key})
    assert select_pending_references((linked,), (scope,)) == (linked,)
    from app.schemas.worker import WorkerAnalysisJobPayload
    from tests.test_setting_book_source_contract import book_payload_data
    origin = WorkerAnalysisJobPayload.model_validate(book_payload_data()).frozen_source()
    book_reference = ref('무관한 인물').model_copy(update={'source': origin, 'source_episode_no': None})
    assert select_pending_references((book_reference,), (scope,)) == ()
