"""Author-facing decision text without internal refs or pipeline words (오류 리포트 가독성 2026-11-02)."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from app.validation.author_text import (
    DISAGREEMENT_FALLBACK,
    IMPACT_REASON_FALLBACKS,
    REASON_FALLBACKS,
    author_labels,
    clean_author_text,
    sanitize_decision,
)
from app.validation.contracts import ValidationDecision
from app.validation.service import SettingValidationService
from tests.support.validation_synthetic import (
    ROLES,
    FakeEmbedding,
    FakeSearch,
    ScriptedModel,
    SyntheticData,
    budget,
    decided,
    draft,
    follow_up,
)


def contract():
    path = Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json"
    return json.loads(path.read_text())["execution"]["judgeFlow"]["authorText"]


LABELS = author_labels(
    evidence=[{"ref": "E2", "source_kind": "EPISODE", "source_context": {"episodeNo": 18}},
              {"ref": "E4", "source_kind": "SETTING_BOOK", "source_context": {"revisionNo": 2}}],
    source_catalogue=[{"source_ref": "R13", "source_kind": "EPISODE", "episodeNo": 18},
                      {"source_ref": "R14", "source_kind": "SETTING_BOOK", "revisionNo": 1}],
    settings=[{"entry_ref": "B83"}],
    subjects=[{"subject_ref": "S1", "name": "민수"}, {"subject_ref": "S2", "name": ""}],
)


def test_labels_cover_target_evidence_catalogue_settings_and_subjects():
    assert LABELS == {"T1": "이번 원고", "R13": "18화", "R14": "설정집", "E2": "18화", "E4": "설정집",
                      "B83": "저장된 설정", "S1": "민수", "S2": "대상"}


@pytest.mark.parametrize("text,expected", [
    ("T1의 장면은 E2와 다르다.", "이번 원고의 장면은 18화와 다르다."),
    ("S1은 B83을 따르지 않는다.", "민수는 저장된 설정을 따르지 않는다."),
    ("T1은 E4와 다르다.", "이번 원고는 설정집과 다르다."),
    ("T1이 E2를 반복한다.", "이번 원고가 18화를 반복한다."),
    ("T1이다.", "이번 원고이다."),                                  # copula, not the subject particle
    ("T1으로 옮겼다.", "이번 원고로 옮겼다."),
    ("B83으로 확인했다.", "저장된 설정으로 확인했다."),
    ("TSEG2에서 S1과 만났다.", "이번 원고에서 민수와 만났다."),
    ("R13과 R14를 봤다.", "18화와 설정집을 봤다."),
])
def test_known_refs_become_author_words_with_the_right_particle(text, expected):
    assert clean_author_text(text, LABELS) == expected


@pytest.mark.parametrize("text,expected", [
    ("검색 영수증은 limited로 표시되어 있다. E2에는 이 규칙이 없다.", "18화에는 이 규칙이 없다."),
    ("VECTOR 검색만 했다! B83에는 값이 없다.", "저장된 설정에는 값이 없다."),
    ("L117 기록을 봤다. 비교는 끝났다.", "비교는 끝났다."),
    ("Q35-BASELINE도 확인했다.", ""),
    ("E99와 T1을 비교했다.", ""),                                   # E99 is not this question's evidence
    ("readiness가 비어 있다? 그래도 T1은 맞다.", "그래도 이번 원고는 맞다."),
])
def test_sentences_with_unknown_refs_or_internal_words_are_dropped(text, expected):
    assert clean_author_text(text, LABELS) == expected


def test_clean_text_and_lookalike_words_stay_byte_for_byte():
    for text in ("저장된 설정과 이번 원고가 서로 맞아요.", "  공백도   그대로  ", "Ragnarok과 Baselines는 고유명사다.",
                 "3.5배 강해졌다. 그 뒤로도 같다."):
        assert clean_author_text(text, LABELS) == text


def test_without_a_subject_catalogue_every_s_ref_reads_as_the_subject():
    labels = author_labels(settings=[{"entry_ref": "B1"}])
    assert clean_author_text("S7은 B1과 다르다.", labels) == "대상은 저장된 설정과 다르다."
    assert clean_author_text("S7은 B1과 다르다.", author_labels(subjects=[])) == ""


@pytest.mark.parametrize("case", contract()["examples"]["cases"], ids=lambda case: case["text"][:12])
def test_shared_contract_examples(case):
    assert clean_author_text(case["text"], contract()["examples"]["labels"]) == case["expected"]


def test_contract_fallbacks_and_counter_match_the_worker():
    rule = contract()
    assert rule["fallbacks"] == {"reason": REASON_FALLBACKS, "impact_reason": IMPACT_REASON_FALLBACKS,
                                 "disagreement": DISAGREEMENT_FALLBACK}
    assert rule["counter"] == "AUTHOR_TEXT_SANITIZED"


def decision(**changes):
    assessment = {"reason": "T1과 E2, L3을 확인했다.", "evidence_refs": ["T1"], "receipt_refs": ["L3"]}
    value = {"action": "DECIDED", "outcome": "CONFLICT", "basis": "DB_RAW_DISAGREEMENT",
             "reason": "T1은 B83과 다르다.", "impact": "SEVERE", "impact_reason": "limited 검색이었다.",
             "target_spans": [], "evidence_refs": ["B83"], "follow_ups": [], "affected_question_refs": [],
             "completion": {key: assessment for key in ("comparison_context", "changes_exceptions",
                                                        "no_pending_clues", "required_reads")},
             "disagreements": [{"fact_ref": "B83", "evidence_ref": "E2", "reason": "B83과 E2가 다르다."},
                               {"fact_ref": "B83", "evidence_ref": "E2", "reason": "Q1 receipt 기준."}]}
    return ValidationDecision.model_validate(value | changes)


def test_decision_fields_are_cleaned_counted_and_completion_is_left_alone():
    stats = Counter()
    cleaned = sanitize_decision(decision(), LABELS, stats)
    assert cleaned.reason == "이번 원고는 저장된 설정과 다르다."
    assert cleaned.impact_reason == IMPACT_REASON_FALLBACKS["SEVERE"]
    assert [row.reason for row in cleaned.disagreements] == ["저장된 설정과 18화가 다르다.", DISAGREEMENT_FALLBACK]
    assert cleaned.completion == decision().completion  # audit records keep their refs
    assert stats["AUTHOR_TEXT_SANITIZED"] == 4


def test_reason_falls_back_by_outcome_and_a_clean_decision_is_returned_as_is():
    stats = Counter()
    cleaned = sanitize_decision(decision(outcome="NEEDS_CONTEXT", basis="AMBIGUOUS_MEANING", reason="L1 영수증.",
                                         impact="REFERENCE", impact_reason="부수 묘사다.", disagreements=[]),
                                LABELS, stats)
    assert cleaned.reason == REASON_FALLBACKS["NEEDS_CONTEXT"] and cleaned.impact_reason == "부수 묘사다."
    assert stats["AUTHOR_TEXT_SANITIZED"] == 1
    clean = decision(reason="저장된 설정과 다르다.", impact_reason="장면이 달라진다.", disagreements=[])
    assert sanitize_decision(clean, LABELS, stats) is clean and stats["AUTHOR_TEXT_SANITIZED"] == 1


def test_judge_stores_sanitized_text_with_episode_and_subject_names():
    def judge(payload, n):
        if n == 1:
            return follow_up(payload, searches=["민수가 의수를 얻은 장면"])
        (row,) = payload["evidence"]
        return decided(payload, "NEEDS_CONTEXT", reason=f"T1은 {row['ref']}와 다르고 S1의 B1과도 다르다. L9도 봤다.",
                       impact_reason="S1의 행동이 limited 범위에서 달라진다.")

    data = SyntheticData()
    service = SettingValidationService(data, ScriptedModel({"TSEG1": [draft()]}, judge), budget(), ROLES,
                                       search=FakeSearch(data), embedding_client=FakeEmbedding())
    result = asyncio.run(service.inspect())
    assert result.status == "COMPLETED"
    stored = result.decisions["Q1"]
    assert stored.reason == "이번 원고는 3화와 다르고 민수의 저장된 설정과도 다르다."  # R2 is episode 3
    assert stored.impact_reason == IMPACT_REASON_FALLBACKS["CAUTION"]
    assert "T1" in stored.completion.comparison_context.evidence_refs
    assert result.normalizations["AUTHOR_TEXT_SANITIZED"] == 2
