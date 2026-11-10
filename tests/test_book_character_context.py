"""BOOK provider input regressions; all characters and text are synthetic."""
import asyncio
import json
from copy import deepcopy
from uuid import UUID

import pytest

from app.analysis.book_character_selection import (
    BOOK_CHARACTER_SELECTION_CACHE_KEY,
    BOOK_CHARACTER_SELECTION_SCHEMA_NAME,
    exact_name_refs,
    selection_response_schema,
)
from app.analysis.exceptions import LlmExtractionError
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
from tests.book_selection_fakes import fake_book_selection, is_book_selection_request
from tests.test_setting_extractor import DEFAULT_SCHEMA_HINTS


class Client:
    """Stage-1 selection answers come from `selection` (refs) or the default name match;
    `selection_texts` scripts raw selection responses for failure tests."""

    def __init__(self, retry=False, *, selection=None, ambiguous=(), unmatched=(), selection_texts=None):
        self.requests = []
        self.selection_requests = []
        self.retry = retry
        self.selection = selection
        self.ambiguous = ambiguous
        self.unmatched = unmatched
        self.selection_texts = list(selection_texts or [])

    async def create_text_response(self, **kwargs):
        if is_book_selection_request(kwargs):
            self.selection_requests.append(kwargs)
            if self.selection_texts:
                return LlmTextResponse(text=self.selection_texts.pop(0))
            payload = fake_book_selection(kwargs, refs=self.selection, ambiguous=self.ambiguous,
                                          unmatched=self.unmatched)
            return LlmTextResponse(text=json.dumps(payload, ensure_ascii=False))
        self.requests.append(kwargs)
        return LlmTextResponse(text='{"candidates":[{}]}' if self.retry and len(self.requests) == 1
                               else '{"candidates":[]}')


def inputs(names=("아린 벨", "보라 남"), *, aliases=None, status_value="회복 중", profile=None):
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
                *[{'factType': 'PROFILE', 'factKey': key, 'factValue': value, 'valueJson': {'value': value}}
                  for key, value in (profile or {}).get(i, {}).items()],
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


def run(body, *, values=None, client=None, max_attempts=2):
    ordered, semantic, book = values or inputs()
    client = client or Client()
    asyncio.run(CharacterSettingExtractor(llm_client=client, max_attempts=max_attempts).extract_from_chunk(
        UUID(int=8), body, schema_hints=DEFAULT_SCHEMA_HINTS,
        ordered_context=ordered, semantic_current_context=semantic, setting_book=book))
    return client


def request(body, *, values=None, retry=False, client=None):
    return run(body, values=values, client=client or Client(retry)).requests


def projected(req):
    marker = 'book_character_context:\n'
    assert marker in req['user_prompt'], 'BOOK must use the compact provider projection'
    return json.JSONDecoder().raw_decode(req['user_prompt'].split(marker, 1)[1])[0]


def selected_refs(client):
    return [r['ref'] for r in projected(client.requests[0])['characters']]


# --- stage 2 projection -------------------------------------------------------------------------

def test_book_selects_details_but_keeps_full_identity_catalogue_and_source():
    req = request('아린: level=12')[0]
    data = projected(req)
    assert [row['name'] for row in data['catalog']] == ['아린 벨', '보라 남']
    assert [row['ref'] for row in data['characters']] == ['K1']
    assert data['characters'][0]['current_slots'][0]['fact_value'] == '직업-0'
    assert data['unmatched_mentions'] == []
    assert '직업-1' not in req['user_prompt']
    assert req['user_prompt'].count('아린 벨의 신원 근거') == 1
    assert 'ordered_character_state:' not in req['user_prompt']
    assert 'semantic_current_state:' not in req['user_prompt']
    assert 'known_character_names:' not in req['user_prompt']
    assert 'active_character_statuses:' not in req['user_prompt']
    assert 'chunk_text:\n아린: level=12' in req['user_prompt']
    assert 'WORK_START' in req['user_prompt']
    assert req['response_schema'] == SETTING_EXTRACTION_RESPONSE_SCHEMA
    assert 'book-character-context-v4' in req['prompt_cache_key']


@pytest.mark.parametrize('body,want', [
    ('아린: level=12', ['K1']),
    ('청람: level=12', ['K1']),
    ('아린: level=12\n누리: level=3', ['K1', 'K2']),
])
def test_numeric_rows_select_without_a_selection_call(body, want):
    client = run(body, values=inputs(('아린 벨', '누리 벨', '보라 남'), aliases={0: ['청람']}))
    assert client.selection_requests == []
    assert selected_refs(client) == want


def test_same_name_owners_remain_separate():
    client = run('아린: level=12', values=inputs(('아린', '아린', '보라')))
    assert client.selection_requests == []
    data = projected(client.requests[0])
    assert [r['ref'] for r in data['characters']] == ['K1', 'K2']
    assert [r['current_slots'][0]['fact_value'] for r in data['characters']] == ['직업-0', '직업-1']


def test_exact_status_consolidation_preserves_protection_time_and_structured_value():
    data = projected(request('아린: level=12')[0])
    row = data['characters'][0]
    status = row['current_slots'][1]
    assert status['fact_value'] == '회복 중'
    assert status['value_json'] == {'active': True, 'severity': None}
    assert status['active_status']['current_value_protected'] is True
    assert status['active_status']['current_source_times'][0]['source_episode_no'] == 1
    assert row['active_statuses'] == []


def test_differing_status_values_are_not_dropped():
    row = projected(request('아린: level=12', values=inputs(status_value='다른 상태'))[0])['characters'][0]
    assert row['active_statuses'][0]['fact_value'] == '다른 상태'
    assert row['current_slots'][1]['fact_value'] == '회복 중'
    assert 'active_status' not in row['current_slots'][1]


def test_frozen_inputs_unchanged_and_retry_uses_same_selected_context():
    values = inputs()
    before = deepcopy((values[0].prompt_data(), values[1].character_prompt_json()))
    client = run('아린 벨은 왕국의 검사다.', values=values, client=Client(retry=True))
    assert len(client.selection_requests) == 1, 'the extraction retry must not re-run selection'
    requests = client.requests
    assert len(requests) == 2
    assert projected(requests[0]) == projected(requests[1])
    assert [r['ref'] for r in projected(requests[0])['characters']] == ['K1']
    assert 'previous_response_correction' in requests[1]['user_prompt']
    assert (values[0].prompt_data(), values[1].character_prompt_json()) == before
    for req in requests:
        assert ensure_ordered_prompt_fits(req['system_prompt'], req['user_prompt'],
            response_schema=req['response_schema']) < 64_000


def test_shared_multichar_name_part_keeps_all_possible_owners():
    data = projected(request('벨론: level=12', values=inputs(('아린 벨론', '누리 벨론', '보라 남')))[0])
    assert [r['ref'] for r in data['characters']] == ['K1', 'K2']


def test_large_named_request_fits_and_narrative_selection_also_fits():
    values = inputs(tuple(f'합성인물{i:03}' for i in range(400)))
    client = run('합성인물000: level=12', values=values)
    req = client.requests[0]
    assert client.selection_requests == []
    assert len(projected(req)['characters']) == 1
    assert ensure_ordered_prompt_fits(req['system_prompt'], req['user_prompt'],
                                     response_schema=req['response_schema']) < 25_000
    # Narrative about the whole cast: the model selection (here: nobody by name) decides, and no
    # full-detail fallback re-inflates the request past the guard.
    client = run('모든 인물의 성년은 20세다.', values=values)
    assert len(client.selection_requests) == 1
    assert projected(client.requests[0])['characters'] == []
    for req in (client.selection_requests[0], client.requests[0]):
        assert ensure_ordered_prompt_fits(req['system_prompt'], req['user_prompt'],
                                         response_schema=req['response_schema']) < 64_000


def test_unresolved_references_keep_order_and_never_become_catalogue_owners():
    from app.schemas.analysis_context import WorkerAnalysisReference
    ordered, semantic, book = inputs()
    refs = [WorkerAnalysisReference(domain='characters', subjectName='미확정인',
        sourceEpisodeNo=i, reason='UNRESOLVED_IDENTITY', value=f'보류값-{i}') for i in (2, 1)]
    ctx = ordered.analysis_context.model_copy(update={'unresolved_references': refs})
    ordered = OrderedExtractionContext(ctx, ordered.known_characters, ())
    client = run('아린 벨은 미확정인의 스승이다.', values=(ordered, semantic, book))
    data = projected(client.requests[0])
    assert data['unresolved_references'] == [ref.prompt_data() for ref in refs]
    assert len(data['catalog']) == 2
    assert [r['ref'] for r in data['characters']] == ['K1']
    selection_payload = json.loads(client.selection_requests[0]['user_prompt'])
    assert selection_payload['unresolved_references'] == ['미확정인']
    assert [row['ref'] for row in selection_payload['characters']] == ['K1', 'K2']


def test_mismatched_semantic_local_refs_fail_before_provider():
    from app.analysis.exceptions import OrderedInputContextError
    from app.analysis.semantic_current_context import SemanticCurrentInput
    ordered, semantic, book = inputs()
    data = json.loads(semantic.character_prompt_json())
    data['characters'][0]['ref'] = 'K2'
    bad = SemanticCurrentInput(json.dumps(data), semantic.world_prompt_json())
    client = Client()
    with pytest.raises(OrderedInputContextError, match='identity_mismatch'):
        run('아린: level=12', values=(ordered, bad, book), client=client)
    assert client.requests == [] and client.selection_requests == []


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
    result = projected(request('새별: level=12', values=(ordered,semantic,book))[0])
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
    result = projected(request('아린: level=12', values=(ordered,semantic,book))[0])['characters'][0]
    assert result['active_statuses'][0]['fact_value'] == '회복 중'
    assert result['current_slots'][1]['value_json']['active'] is False
    assert 'active_status' not in result['current_slots'][1]


def test_book_rules_reference_the_context_fields_actually_sent():
    req = request('아린: level=12')[0]
    assert 'known_character_names' not in req['system_prompt']
    assert 'active_character_statuses' not in req['system_prompt']
    assert 'book_character_context.catalog' in req['system_prompt']
    assert 'current_slots(active_status 포함)와 active_statuses' in req['system_prompt']
    assert 'unmatched_mentions' in req['system_prompt']


def test_selected_projection_preserves_every_current_slot_and_status_origin():
    ordered, semantic, book = inputs()
    client = run('아린 벨과 보라 남은 같은 마을 출신이다.', values=(ordered, semantic, book))
    projected_rows = projected(client.requests[0])['characters']
    assert [row['ref'] for row in projected_rows] == ['K1', 'K2']
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


def test_book_projection_does_not_instruct_dropping_repeated_current_facts():
    req = request('아린: 부상은 회복 중이며 내일 나을 예정이다.')[0]
    assert 'CURRENT와 같은 사실도 이번 원문에 명시되면' in req['system_prompt']
    assert '현재 사실과 미실현 전망을 각각' in req['system_prompt']


def test_book_projection_preserves_explicit_roster_qualifiers_in_values():
    req = request('새벽 기사단\n아린: 정예 근위대장')[0]
    assert '소속·직속 관계·직책/등급의 수식어' in req['system_prompt']
    assert '값에도 보존' in req['system_prompt']


# --- stage 1 selection --------------------------------------------------------------------------

@pytest.mark.parametrize('body', [
    '아린은 검사다. 왕국 시민은 20세부터 성인이다.',
    '아린: 비고=보라에게만 회복 능력이 있다',
    '아린: level=12\n왕국 시민은 20세부터 성인이다.',
    '벨 가문의 전원은 기사다.',
    '3아린 2000년은 배경 시대다.',
])
def test_narrative_and_mixed_chunks_go_through_model_selection(body):
    client = run(body, values=inputs(('아린', '보라', '누리 벨')))
    assert len(client.selection_requests) == 1
    assert client.requests[0]['prompt_cache_key'].count('book-character-context-v4') == 1


def test_selection_request_sends_light_catalogue_with_hints_not_current_details():
    values = inputs(('아린 벨', '보라 남', '합성인물'), aliases={0: ['청람']}, profile={
        0: {'profile.position': '재판장', 'profile.hair_color': '검정'},
        1: {'profile.children': '아린 벨'},
    })
    client = run('재판장의 어머니는 상단을 이끈다.', values=values)
    req = client.selection_requests[0]
    payload = json.loads(req['user_prompt'])
    assert payload['chunk_text'] == '재판장의 어머니는 상단을 이끈다.'
    assert payload['characters'][0] == {'ref': 'K1', 'name': '아린 벨', 'aliases': ['청람'],
        'confirmation_status': 'CONFIRMED', 'hints': {'직업': '직업-0', 'position': '재판장'}}
    assert payload['characters'][1]['hints'] == {'직업': '직업-1', 'children': '아린 벨'}
    assert '검정' not in req['user_prompt'], 'appearance is not an identity hint'
    assert '신원 근거' not in req['user_prompt'] and 'current_slots' not in req['user_prompt']
    assert req['prompt_cache_key'] == BOOK_CHARACTER_SELECTION_CACHE_KEY
    assert req['response_schema'].name == BOOK_CHARACTER_SELECTION_SCHEMA_NAME
    assert req['max_output_tokens'] == 4000
    schema = req['response_schema'].schema
    assert schema['$defs']['_Selected']['properties']['ref']['enum'] == ['K1', 'K2', 'K3']
    assert schema['$defs']['_Ambiguous']['properties']['candidate_refs']['items']['enum'] == ['K1', 'K2', 'K3']
    assert ensure_ordered_prompt_fits(req['system_prompt'], req['user_prompt'],
                                     response_schema=req['response_schema']) < 10_000


def test_model_selection_by_title_and_kinship_sends_only_those_details():
    values = inputs(('아린 벨', '보라 남', '누리'), profile={0: {'profile.position': '재판장'}})
    client = run('재판장의 어머니는 상단을 이끈다.', values=values, client=Client(selection=['K1', 'K2']))
    assert selected_refs(client) == ['K1', 'K2']
    assert '직업-2' not in client.requests[0]['user_prompt']
    assert [row['name'] for row in projected(client.requests[0])['catalog']] == ['아린 벨', '보라 남', '누리']


def test_ambiguous_candidates_are_all_kept_and_never_merged():
    values = inputs(('아린 벨', '아린 남', '보라 남'))
    client = run('아린은 왕국 기사단 소속이다.', values=values,
                 client=Client(selection=[], ambiguous=[('아린', ['K1', 'K2'])]))
    assert selected_refs(client) == ['K1', 'K2']


def test_unmatched_mentions_reach_extraction_as_data_not_as_new_catalogue_rows():
    client = run('하린은 아린 벨의 동생이며 레벨 7이다.', values=inputs(),
                 client=Client(selection=['K1'], unmatched=['하린']))
    data = projected(client.requests[0])
    assert data['unmatched_mentions'] == ['하린']
    assert [row['ref'] for row in data['catalog']] == ['K1', 'K2']
    assert selected_refs(client) == ['K1']


def test_whole_name_in_chunk_is_unioned_even_when_the_model_omits_it():
    client = run('보라 남은 북부 출신이다.', values=inputs(), client=Client(selection=[]))
    assert selected_refs(client) == ['K2']


def test_exact_name_union_ignores_one_character_names_and_absent_names():
    catalog = [{'ref': 'K1', 'name': '강', 'aliases': []}, {'ref': 'K2', 'name': '보라 남', 'aliases': ['청람']},
               {'ref': 'K3', 'name': '누리', 'aliases': []}]
    assert exact_name_refs('강 건너 마을에서 청람이 왔다.', catalog) == {'K2'}
    assert exact_name_refs('누구도 오지 않았다.', catalog) == set()


def test_selection_schema_rejects_unknown_refs_and_retries_then_fails_without_fallback():
    bad = json.dumps({'selected': [{'ref': 'K9', 'mention': 'x', 'basis': 'NAME'}], 'ambiguous': [],
                      'unmatched_mentions': []})
    client = Client(selection_texts=[bad, bad])
    with pytest.raises(LlmExtractionError):
        run('아린은 검사다.', client=client, max_attempts=2)
    assert len(client.selection_requests) == 2
    assert 'validation_feedback' in client.selection_requests[1]['user_prompt']
    assert client.requests == [], 'no extraction call and no full-catalogue fallback after a failed selection'


def test_selection_failure_is_classified_as_a_response_parse_failure():
    from app.domain.enums import AnalysisFailureCode
    from app.exceptions.failure_classification import analysis_failure_code
    client = Client(selection_texts=['not json', 'not json'])
    with pytest.raises(LlmExtractionError) as info:
        run('아린은 검사다.', client=client, max_attempts=2)
    assert analysis_failure_code(info.value) == AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR


def test_selection_call_obeys_the_input_guard_before_the_provider():
    from app.analysis.exceptions import OrderedPromptBudgetExceeded
    values = inputs(tuple(f'합성인물{i:03}' for i in range(2600)))
    client = Client()
    with pytest.raises(OrderedPromptBudgetExceeded):
        run('모든 인물의 성년은 20세다.', values=values, client=client)
    assert client.selection_requests == [] and client.requests == []


def test_input_guard_default_is_128k_and_applies_to_both_calls():
    from app.analysis.exceptions import OrderedPromptBudgetExceeded
    from app.core.config import get_settings
    assert get_settings().llm_setting_extraction_max_input_tokens == 128_000
    # 1,600 synthetic characters: the selection request alone is over the old 64,000 guard but under 128,000.
    values = inputs(tuple(f'합성인물{i:03}' for i in range(1600)))
    client = run('모든 인물의 성년은 20세다.', values=values)
    selection = client.selection_requests[0]
    guard = ensure_ordered_prompt_fits(selection['system_prompt'], selection['user_prompt'], 10**12,
                                       response_schema=selection['response_schema'])
    assert 64_000 < guard < 128_000
    assert len(client.requests) == 1
    # An explicit smaller limit still rejects before any provider call, for both stages.
    client = Client()
    with pytest.raises(OrderedPromptBudgetExceeded):
        asyncio.run(CharacterSettingExtractor(llm_client=client, max_attempts=2, max_input_tokens=64_000)
                    .extract_from_chunk(UUID(int=8), '모든 인물의 성년은 20세다.', schema_hints=DEFAULT_SCHEMA_HINTS,
                                        ordered_context=values[0], semantic_current_context=values[1], setting_book=values[2]))
    assert client.selection_requests == [] and client.requests == []
    client = Client(selection=[])
    with pytest.raises(OrderedPromptBudgetExceeded):
        asyncio.run(CharacterSettingExtractor(llm_client=client, max_attempts=2, max_input_tokens=20_000)
                    .extract_from_chunk(UUID(int=8), '합성인물000: level=12', schema_hints=DEFAULT_SCHEMA_HINTS,
                                        ordered_context=values[0], semantic_current_context=values[1], setting_book=values[2]))
    assert client.requests == [], 'extraction guard rejects before the provider'


def test_empty_catalogue_skips_selection():
    values = inputs(())
    client = run('하린은 레벨 7이다.', values=values)
    assert client.selection_requests == []
    data = projected(client.requests[0])
    assert data['catalog'] == [] and data['characters'] == [] and data['unmatched_mentions'] == []


def test_selection_schema_is_strict_with_enum_refs():
    schema = selection_response_schema(['K1', 'K2']).schema
    assert schema['additionalProperties'] is False
    assert set(schema['required']) == {'selected', 'ambiguous', 'unmatched_mentions'}
    assert schema['$defs']['_Selected']['properties']['basis']['enum'] == [
        'NAME', 'ALIAS', 'TITLE', 'PRONOUN', 'FAMILY', 'OTHER']
