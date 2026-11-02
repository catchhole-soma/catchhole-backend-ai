import pytest
from app.schemas.worker import WorkerFailureDiagnostics
from app.exceptions.failure_classification import failure_diagnostics
from app.analysis.relationship_comparator import RelationshipComparator, relationship_comparison_prompt_data
from tests.test_relationship_comparator import context, decision, relationship


def test_only_bounded_source_or_response_fields_survive_optional_metadata():
    value = WorkerFailureDiagnostics(origin='WORKER', stage='WORLD_CONTEXT', reasonCode='WORLD_SUBJECT_REFERENCE_INVALID', excerpts=[
        {'kind': 'SOURCE', 'field': 'evidenceQuote', 'text': '가' * 200},
        {'kind': 'RESPONSE', 'field': 'authorization', 'text': 'SECRET'},
    ]).model_dump(by_alias=True, exclude_none=True)
    assert value.get('excerpts') == [{'kind': 'SOURCE', 'field': 'evidenceQuote', 'text': '가' * 120, 'truncated': True}]


def test_merge_failure_shows_differing_existing_and_returned_summary():
    prefix = '같은 문장이다. ' * 30
    old, new = prefix + '서로를 경계한다.', prefix + '서로를 신뢰한다.'
    data = context(relationships=[relationship(summary=old)])
    with pytest.raises(Exception) as failed:
        RelationshipComparator._validate_response({'decisions': [decision(operation='MERGE', relationshipRef='R1', summary=new)]},
                                                  relationship_comparison_prompt_data(data))
    diagnostic = failure_diagnostics(failed.value, stage='RELATIONSHIP_COMPARE').model_dump(by_alias=True, exclude_none=True)
    excerpts = diagnostic.get('excerpts', [])
    assert len(excerpts) == 2
    assert excerpts[0]['kind'] == 'EXPECTED' and '경계한다' in excerpts[0]['text']
    assert excerpts[1]['kind'] == 'RESPONSE' and '신뢰한다' in excerpts[1]['text']
    assert all(e['field'] == 'relationshipSummary' and e['truncated'] for e in excerpts)
    assert all('secret-token' not in e['text'] for e in excerpts)


def test_display_reference_excerpt_centers_the_actual_late_reference():
    text = '정상 설명이다. ' * 18 + 'N1'
    with pytest.raises(Exception) as failed:
        RelationshipComparator._validate_response({'decisions': [decision(summary=text)]},
                                                  relationship_comparison_prompt_data(context()))
    diagnostic = failure_diagnostics(failed.value, stage='RELATIONSHIP_COMPARE')
    assert diagnostic.reason_code == 'RELATIONSHIP_DISPLAY_REFERENCE_LEAK'
    assert 'N1' in diagnostic.excerpts[0].text
    assert diagnostic.excerpts[0].truncated
