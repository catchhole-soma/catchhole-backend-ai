"""BOOK provider input regressions; all characters and text are synthetic."""
import asyncio
import json
from copy import deepcopy
from uuid import UUID

import pytest

from app.analysis.ordered_context import OrderedExtractionContext, ensure_ordered_prompt_fits
from app.analysis.semantic_current_context import (
    SemanticCurrentContext,
    prepare_semantic_current_context,
)
from app.analysis.setting_extractor import (
    SETTING_EXTRACTION_RESPONSE_SCHEMA,
    CharacterSettingExtractor,
)
from app.llm.responses import LlmTextResponse
from app.schemas.analysis_context import WorkerAnalysisContext
from app.schemas.worker import WorkerAnalysisKnownCharacterPayload, WorkerAnalysisSettingBookPayload
from tests.test_setting_extractor import DEFAULT_SCHEMA_HINTS


class Client:
    def __init__(self, retry=False):
        self.requests = []
        self.retry = retry

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        return LlmTextResponse(text='{"candidates":[{}]}' if self.retry and len(self.requests) == 1
                               else '{"candidates":[]}')


def inputs(names=("아린 벨", "보라 남"), *, aliases=None, status_value="회복 중"):
    ctx = WorkerAnalysisContext.model_validate({
        'runId': str(UUID(int=1)), 'generation': 1, 'inputStateHash': 'a'*64,
        'sourceHash': 'b'*64, 'formatVersion': 1, 'unresolvedReferences': [],
    })
    origin = {'confirmationStatus': 'CONFIRMED', 'sourceEpisodeNo': 1,
              'reviewSource': 'HUMAN', 'sourceKind': 'EPISODE',
              'currentValueProtected': True, 'currentSourceTimes': [
                  {'sourceKind': 'EPISODE', 'sourceEpisodeNo': 1, 'temporalScope': 'PRESENT'}]}
    known = tuple(WorkerAnalysisKnownCharacterPayload.model_validate({
        'characterId': str(UUID(int=100+i)), 'name': name,
        'aliases': (aliases or {}).get(i, []),
        'identityEvidence': [{'quote': f'{name}의 신원 근거', 'startOffset': 0,
                              'endOffset': len(f'{name}의 신원 근거')}],
        'provenance': origin, 'activeStatuses': [
            {'factKey': 'status.부상', 'factValue': status_value, 'provenance': origin}],
    }) for i, name in enumerate(names))
    carrier = SemanticCurrentContext.model_validate({
        'formatVersion': 1, 'runId': str(ctx.run_id), 'generation': 1,
        'inputStateHash': 'a'*64, 'sourceHash': 'b'*64, 'complete': True,
        'characters': [{'characterId': str(k.character_id), 'provisionalSubjectKey': None,
            'currentSlots': [
                {'factType': 'PROFILE', 'factKey': 'profile.직업', 'factValue': f'직업-{i}',
                 'valueJson': {'value': f'직업-{i}'}},
                {'factType': 'STATUS', 'factKey': 'status.부상', 'factValue': '회복 중',
                 'valueJson': {'active': True, 'severity': None}},
            ]} for i,k in enumerate(known)], 'worldSubjects': [],
    })
    semantic = prepare_semantic_current_context(carrier, analysis_context=ctx,
        known_characters=known, provisional_characters=(), source_hash='b'*64)
    ordered = OrderedExtractionContext(ctx, known, ())
    book = WorkerAnalysisSettingBookPayload.model_validate({
        'settingBookId': str(UUID(int=2)), 'revisionId': str(UUID(int=3)), 'revisionNo': 2,
        'originalFilename': '합성 설정.txt', 'contentS3Key': 'synthetic/book.txt',
        'contentHash': 'b'*64, 'charCount': 30, 'referenceTimeKind': 'WORK_START',
    })
    return ordered, semantic, book


def request(body, *, values=None, retry=False):
    ordered, semantic, book = values or inputs()
    client = Client(retry)
    asyncio.run(CharacterSettingExtractor(llm_client=client, max_attempts=2).extract_from_chunk(
        UUID(int=8), body, schema_hints=DEFAULT_SCHEMA_HINTS,
        ordered_context=ordered, semantic_current_context=semantic, setting_book=book))
    return client.requests


def projected(req):
    marker = 'book_character_context:\n'
    assert marker in req['user_prompt'], 'BOOK must use the compact provider projection'
    return json.JSONDecoder().raw_decode(req['user_prompt'].split(marker, 1)[1])[0]


def test_book_selects_details_but_keeps_full_identity_catalogue_and_source():
    req = request('아린: 직업=검사')[0]
    data = projected(req)
    assert [row['name'] for row in data['catalog']] == ['아린 벨', '보라 남']
    assert [row['ref'] for row in data['characters']] == ['K1']
    assert data['characters'][0]['current_slots'][0]['fact_value'] == '직업-0'
    assert '직업-1' not in req['user_prompt']
    assert req['user_prompt'].count('아린 벨의 신원 근거') == 1
    assert 'ordered_character_state:' not in req['user_prompt']
    assert 'semantic_current_state:' not in req['user_prompt']
    assert 'known_character_names:' not in req['user_prompt']
    assert 'active_character_statuses:' not in req['user_prompt']
    assert 'chunk_text:\n아린: 직업=검사' in req['user_prompt']
    assert 'WORK_START' in req['user_prompt']
    assert req['response_schema'] == SETTING_EXTRACTION_RESPONSE_SCHEMA
    assert 'book-character-context-v1' in req['prompt_cache_key']


@pytest.mark.parametrize('body,want', [
    ('아린: 직업=검사', ['K1']),
    ('청람: 직업=검사', ['K1']),
    ('벨 가문의 전원은 기사다.', ['K1', 'K2', 'K3']),
    ('아린과 나머지 인물들은 모두 성인이다.', ['K1', 'K2', 'K3']),
    ('3아린 2000년은 배경 시대다.', ['K1', 'K2', 'K3']),
    ('아린포스 기사단은 강하다.', ['K1', 'K2', 'K3']),
    ('이 시대의 성년은 20세다.', ['K1', 'K2', 'K3']),
    ('벨은 검사다.', ['K1', 'K2', 'K3']),
])
def test_conservative_name_boundaries_aliases_ambiguity_and_global_fallback(body,want):
    values = inputs(('아린 벨', '누리 벨', '보라 남'), aliases={0:['청람']})
    assert [r['ref'] for r in projected(request(body, values=values)[0])['characters']] == want


def test_same_name_owners_remain_separate():
    values = inputs(('아린', '아린', '보라'))
    data = projected(request('아린: 직업=검사', values=values)[0])
    assert [r['ref'] for r in data['characters']] == ['K1', 'K2']
    assert [r['current_slots'][0]['fact_value'] for r in data['characters']] == ['직업-0', '직업-1']


def test_exact_status_consolidation_preserves_protection_time_and_structured_value():
    data = projected(request('아린: 직업=검사')[0])
    row = data['characters'][0]
    status = row['current_slots'][1]
    assert status['fact_value'] == '회복 중'
    assert status['value_json'] == {'active': True, 'severity': None}
    assert status['active_status']['current_value_protected'] is True
    assert status['active_status']['current_source_times'][0]['source_episode_no'] == 1
    assert row['active_statuses'] == []


def test_differing_status_values_are_not_dropped():
    row = projected(request('아린: 직업=검사', values=inputs(status_value='다른 상태'))[0])['characters'][0]
    assert row['active_statuses'][0]['fact_value'] == '다른 상태'
    assert row['current_slots'][1]['fact_value'] == '회복 중'
    assert 'active_status' not in row['current_slots'][1]


def test_frozen_inputs_unchanged_and_retry_uses_same_selected_context():
    values = inputs()
    before = deepcopy((values[0].prompt_data(), values[1].character_prompt_json()))
    requests = request('아린: 직업=검사', values=values, retry=True)
    assert len(requests) == 2
    assert projected(requests[0]) == projected(requests[1])
    assert 'previous_response_correction' in requests[1]['user_prompt']
    assert (values[0].prompt_data(), values[1].character_prompt_json()) == before
    for req in requests:
        assert ensure_ordered_prompt_fits(req['system_prompt'], req['user_prompt'],
            response_schema=req['response_schema']) < 64_000


def test_shared_multichar_name_part_keeps_all_possible_owners():
    data = projected(request('벨론: 직업=검사', values=inputs(('아린 벨론', '누리 벨론', '보라 남')))[0])
    assert [r['ref'] for r in data['characters']] == ['K1', 'K2']


def test_large_named_request_fits_but_unscoped_request_still_obeys_guard():
    from app.analysis.exceptions import OrderedPromptBudgetExceeded
    values = inputs(tuple(f'합성인물{i:03}' for i in range(400)))
    req = request('합성인물000: 직업=검사', values=values)[0]
    assert len(projected(req)['characters']) == 1
    assert ensure_ordered_prompt_fits(req['system_prompt'], req['user_prompt'],
                                     response_schema=req['response_schema']) < 25_000
    client = Client()
    ordered, semantic, book = values
    with pytest.raises(OrderedPromptBudgetExceeded):
        asyncio.run(CharacterSettingExtractor(llm_client=client).extract_from_chunk(
            UUID(int=8), '모든 인물의 성년은 20세다.', schema_hints=DEFAULT_SCHEMA_HINTS,
            ordered_context=ordered, semantic_current_context=semantic, setting_book=book))
    assert client.requests == []


def test_unresolved_references_keep_order_and_never_become_catalogue_owners():
    from app.schemas.analysis_context import WorkerAnalysisReference
    ordered, semantic, book = inputs()
    refs = [WorkerAnalysisReference(domain='characters', subjectName='미확정인',
        sourceEpisodeNo=i, reason='UNRESOLVED_IDENTITY', value=f'보류값-{i}') for i in (2, 1)]
    ctx = ordered.analysis_context.model_copy(update={'unresolved_references': refs})
    ordered = OrderedExtractionContext(ctx, ordered.known_characters, ())
    data = projected(request('아린: 직업=검사', values=(ordered, semantic, book))[0])
    assert data['unresolved_references'] == [ref.prompt_data() for ref in refs]
    assert len(data['catalog']) == 2
    assert [r['ref'] for r in data['characters']] == ['K1']


def test_mismatched_semantic_local_refs_fail_before_provider():
    from app.analysis.exceptions import OrderedInputContextError
    from app.analysis.semantic_current_context import SemanticCurrentInput
    ordered, semantic, book = inputs()
    data = json.loads(semantic.character_prompt_json())
    data['characters'][0]['ref'] = 'K2'
    bad = SemanticCurrentInput(json.dumps(data), semantic.world_prompt_json())
    with pytest.raises(OrderedInputContextError, match='identity_mismatch'):
        request('아린: 직업=검사', values=(ordered, bad, book))


def test_provisional_identity_evidence_kept_once_and_not_promoted():
    from app.analysis.semantic_current_context import SemanticCurrentInput
    from app.schemas.worker import WorkerAnalysisProvisionalCharacterPayload
    ordered, semantic, book = inputs()
    provisional = WorkerAnalysisProvisionalCharacterPayload.model_validate({
        'provisionalSubjectKey': f'provisional-character:{UUID(int=9)}', 'name':'청아',
        'aliases':['새별'], 'sourceEpisodeNo':1,
        'identityEvidence':[{'quote':'청아의 근거', 'startOffset':0, 'endOffset':6}],
        'activeStatuses':[],
    })
    ordered = OrderedExtractionContext(ordered.analysis_context, ordered.known_characters, (provisional,))
    data = json.loads(semantic.character_prompt_json())
    data['characters'].append({'ref':'K3','name':'청아','aliases':['새별'],
                              'identity_evidence':['청아의 근거'], 'current_slots':[]})
    semantic = SemanticCurrentInput(json.dumps(data), semantic.world_prompt_json())
    result = projected(request('새별: 직업=검사', values=(ordered,semantic,book))[0])
    assert result['catalog'][2] == {'ref':'K3','name':'청아','aliases':['새별'],
                                    'confirmation_status':'PROVISIONAL'}
    assert [r['ref'] for r in result['characters']] == ['K3']
    assert result['characters'][0]['identity_evidence'] == ['청아의 근거']
    assert result['characters'][0]['source_episode_no'] == 1


def test_inactive_current_slot_does_not_absorb_active_status_provenance():
    from app.analysis.semantic_current_context import SemanticCurrentInput
    ordered, semantic, book = inputs()
    data = json.loads(semantic.character_prompt_json())
    data['characters'][0]['current_slots'][1]['value_json']['active'] = False
    semantic = SemanticCurrentInput(json.dumps(data),semantic.world_prompt_json())
    result = projected(request('아린: 직업=검사', values=(ordered,semantic,book))[0])['characters'][0]
    assert result['active_statuses'][0]['fact_value'] == '회복 중'
    assert result['current_slots'][1]['value_json']['active'] is False
    assert 'active_status' not in result['current_slots'][1]


@pytest.mark.parametrize('body', [
    '아린은 검사다. 왕국 시민은 20세부터 성인이다.',
    '아린은 검사다. 그녀의 스승은 왼쪽 눈이 보이지 않는다.',
    '아린: 직업=검사\n왕국 시민은 20세부터 성인이다.',
    '아린: 직업=검사\n보라에게만 회복 능력이 있다.',
    '아린은 검사다. 벨은 기사다.',
])
def test_mixed_named_and_unbounded_scope_keeps_all_details(body):
    data = projected(request(body, values=inputs(('아린', '보라', '누리 벨')))[0])
    assert [r['ref'] for r in data['characters']] == ['K1', 'K2', 'K3']


def test_explicit_short_heading_keeps_ambiguous_owners_even_with_another_match():
    data = projected(request('아린: 직업=검사\n벨: 직업=기사',
                             values=inputs(('아린', '누리 벨', '보라 벨', '청아')))[0])
    assert [r['ref'] for r in data['characters']] == ['K1', 'K2', 'K3']


def test_named_row_keeps_other_referenced_owner_with_compound_particle():
    data = projected(request('아린: 보라에게만 회복 능력이 있다고 기록했다.',
                             values=inputs(('아린', '보라', '누리')))[0])
    assert [r['ref'] for r in data['characters']] == ['K1', 'K2']


def test_book_rules_reference_the_context_fields_actually_sent():
    req = request('아린: 직업=검사')[0]
    assert 'known_character_names' not in req['system_prompt']
    assert 'active_character_statuses' not in req['system_prompt']
    assert 'book_character_context.catalog' in req['system_prompt']
    assert 'current_slots(active_status 포함)와 active_statuses' in req['system_prompt']


def test_unscoped_projection_preserves_every_current_slot_and_status_origin():
    ordered, semantic, book = inputs()
    projected_rows = projected(request('왕국 시민은 20세부터 성인이다.',
                               values=(ordered, semantic, book))[0])['characters']
    original_current = json.loads(semantic.character_prompt_json())['characters']
    original_ordered = ordered.prompt_data()['characters']
    for index, row in enumerate(projected_rows):
        slots = deepcopy(row['current_slots'])
        statuses = deepcopy(row['active_statuses'])
        for slot in slots:
            origin = slot.pop('active_status', None)
            if origin is not None:
                statuses.append({'fact_key': slot['fact_key'], 'fact_value': slot['fact_value'], **origin})
        assert slots == original_current[index]['current_slots']
        assert statuses == original_ordered[index]['active_statuses']
        for key, value in original_ordered[index].items():
            if key not in ('name','aliases','confirmation_status','active_statuses'):
                assert row[key] == value
