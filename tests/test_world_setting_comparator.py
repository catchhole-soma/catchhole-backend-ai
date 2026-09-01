import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.exceptions import ComparisonValidationError
from app.analysis.world_setting_comparator import WorldSettingComparator
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingProperty,
)

TARGET_ID = UUID("00000000-0000-0000-0000-000000000004")


def test_batch_comparator_consolidates_two_sources_into_one_canonical_decision() -> None:
    candidates = [
        _batch_candidate("C1", "사냥 방식", "무리를 지어 사냥한다."),
        _batch_candidate("C2", "사냥 전술", "여럿이 목표를 포위한다."),
    ]
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1", "C2"],
                        "consolidation_status": "MERGED",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "전투 특성",
                        "proposed_setting_name": "사냥 전술",
                        "proposed_value": "무리를 지어 목표를 포위해 사냥한다.",
                        "comparison_reason": "두 원문이 같은 사냥 전술을 보완한다.",
                    }
                ]
            }
        ]
    )

    result, raw = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
            "RACE",
            candidates,
            [_target()],
        )
    )

    assert len(result.decisions) == 1
    assert result.decisions[0].source_candidate_refs == ["C1", "C2"]
    assert result.decisions[0].proposed_setting_name == "사냥 전술"
    assert raw["decisions"][0]["source_candidate_refs"] == ["C1", "C2"]
    prompt = text_client.requests[0]["user_prompt"]
    assert '"ref": "C1"' in prompt
    assert '"ref": "C2"' in prompt
    assert str(candidates[0].candidate_id) not in prompt
    prompt_payload = json.loads(prompt)
    assert prompt_payload["candidates"][0]["evidence_spans"] == [
        {"quote": "원문 근거", "start_offset": None, "end_offset": None}
    ]


def test_batch_comparator_preserves_two_conflicting_sources_in_one_decision() -> None:
    candidates = [
        _batch_candidate("C1", "무기", "곤봉을 사용한다."),
        _batch_candidate("C2", "무기", "검만 사용한다."),
    ]
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1", "C2"],
                        "consolidation_status": "CONFLICT",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "전투 특성",
                        "proposed_setting_name": "무기",
                        "proposed_value": "곤봉을 사용한다.\n검만 사용한다.",
                        "comparison_reason": "두 원문의 무기 설명이 서로 충돌한다.",
                    }
                ]
            }
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
            "RACE",
            candidates,
            [_target()],
        )
    )

    assert len(result.decisions) == 1
    assert result.decisions[0].consolidation_status == "CONFLICT"
    assert result.decisions[0].source_candidate_refs == ["C1", "C2"]
    assert "곤봉" in result.decisions[0].proposed_value
    assert "검만" in result.decisions[0].proposed_value


@pytest.mark.parametrize(
    "invalid_decisions",
    [
        [
            {
                "source_candidate_refs": ["C1"],
                "consolidation_status": "SINGLE",
                "operation": "ADD",
                "target_ref": "T1",
                "proposed_scope_name": "전투 특성",
                "proposed_setting_name": "사냥 방식",
                "proposed_value": "무리를 지어 사냥한다.",
                "comparison_reason": "새 사냥 방식이다.",
            },
            {
                "source_candidate_refs": ["C1", "C2"],
                "consolidation_status": "MERGED",
                "operation": "ADD",
                "target_ref": "T1",
                "proposed_scope_name": "전투 특성",
                "proposed_setting_name": "사냥 전술",
                "proposed_value": "무리를 지어 목표를 포위한다.",
                "comparison_reason": "두 사냥 설명을 합친다.",
            },
        ],
        [
            {
                "source_candidate_refs": ["C1", "C9"],
                "consolidation_status": "MERGED",
                "operation": "ADD",
                "target_ref": "T1",
                "proposed_scope_name": "전투 특성",
                "proposed_setting_name": "사냥 전술",
                "proposed_value": "무리를 지어 목표를 포위한다.",
                "comparison_reason": "두 사냥 설명을 합친다.",
            }
        ],
    ],
    ids=["source-used-in-two-decisions", "unknown-source-ref"],
)
def test_batch_comparator_retries_invalid_source_membership(
    invalid_decisions: list[dict],
) -> None:
    candidates = [
        _batch_candidate("C1", "사냥 방식", "무리를 지어 사냥한다."),
        _batch_candidate("C2", "사냥 전술", "여럿이 목표를 포위한다."),
    ]
    valid_decision = {
        "source_candidate_refs": ["C1", "C2"],
        "consolidation_status": "MERGED",
        "operation": "ADD",
        "target_ref": "T1",
        "proposed_scope_name": "전투 특성",
        "proposed_setting_name": "사냥 전술",
        "proposed_value": "무리를 지어 목표를 포위해 사냥한다.",
        "comparison_reason": "두 사냥 설명을 하나로 합친다.",
    }
    text_client = FakeTextClient(
        [
            {"decisions": invalid_decisions},
            {"decisions": [valid_decision]},
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=2).compare_batch(
            "RACE",
            candidates,
            [_target()],
        )
    )

    assert result.decisions[0].source_candidate_refs == ["C1", "C2"]
    assert len(text_client.requests) == 2
    retry_payload = json.loads(text_client.requests[1]["user_prompt"])
    assert retry_payload["validation_feedback"]["previous_response_rejected"] is True


def test_episode_50_alias_regression_merges_one_existing_weapon_property() -> None:
    candidates = [
        _batch_candidate("C1", "장비", "곤봉을 사용한다.").model_copy(
            update={"subject_name": "고블린 떼"}
        ),
        _batch_candidate("C2", "휴대 무기", "단검도 사용한다.").model_copy(
            update={"subject_name": "고블린 무리"}
        ),
    ]
    target = _target().model_copy(
        update={
            "subject_name": "고블린",
            "properties": [
                WorkerWorldSettingProperty(
                    scope_name="전투 특성",
                    setting_name="무기",
                    value="곤봉을 사용한다.",
                )
            ],
        }
    )
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1", "C2"],
                        "consolidation_status": "MERGED",
                        "operation": "MERGE",
                        "target_ref": "T1",
                        "matched_scope_name": "전투 특성",
                        "matched_property_name": "무기",
                        "proposed_scope_name": "전투 특성",
                        "proposed_setting_name": "무기",
                        "proposed_value": "곤봉과 단검을 사용한다.",
                        "comparison_reason": "두 별칭의 무기 정보를 하나로 합친다.",
                    }
                ]
            }
        ]
    )

    result, raw = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
            "RACE",
            candidates,
            [target],
        )
    )

    assert len(result.decisions) == 1
    decision = result.decisions[0]
    assert decision.consolidation_status == "MERGED"
    assert decision.operation == "MERGE"
    assert decision.matched_property_name == "무기"
    assert decision.proposed_setting_name == "무기"
    assert decision.source_candidate_refs == ["C1", "C2"]
    assert raw["decisions"][0]["source_candidate_refs"] == ["C1", "C2"]


def test_batch_comparator_retries_when_a_source_candidate_is_missing() -> None:
    candidates = [
        _batch_candidate("C1", "사냥 방식", "무리를 지어 사냥한다."),
        _batch_candidate("C2", "사냥 전술", "여럿이 목표를 포위한다."),
    ]
    decision = {
        "consolidation_status": "MERGED",
        "operation": "ADD",
        "target_ref": "T1",
        "proposed_scope_name": "전투 특성",
        "proposed_setting_name": "사냥 전술",
        "proposed_value": "무리를 지어 목표를 포위해 사냥한다.",
        "comparison_reason": "두 원문이 같은 사냥 전술을 보완한다.",
    }
    text_client = FakeTextClient(
        [
            {"decisions": [{**decision, "source_candidate_refs": ["C1"]}]},
            {"decisions": [{**decision, "source_candidate_refs": ["C1", "C2"]}]},
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=2).compare_batch(
            "RACE",
            candidates,
            [_target()],
        )
    )

    assert result.decisions[0].source_candidate_refs == ["C1", "C2"]
    assert len(text_client.requests) == 2
    retry_payload = json.loads(text_client.requests[1]["user_prompt"])
    assert retry_payload["validation_feedback"]["previous_response_rejected"] is True


@pytest.mark.parametrize("operation", ["ADD", "EXCLUDE"])
def test_batch_comparator_requires_existing_canonical_subject_target(
    operation: str,
) -> None:
    candidate = _batch_candidate("C1", "사냥 방식", "무리를 지어 사냥한다.")
    decision = {
        "source_candidate_refs": ["C1"],
        "consolidation_status": "SINGLE",
        "operation": operation,
        "target_ref": None,
        "proposed_scope_name": "전투 특성",
        "proposed_setting_name": "사냥 방식",
        "proposed_value": "무리를 지어 사냥한다.",
        "comparison_reason": "기존 고블린 주체에 대한 판단이다.",
    }
    text_client = FakeTextClient(
        [
            {"decisions": [decision]},
            {"decisions": [{**decision, "target_ref": "T1"}]},
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=2).compare_batch(
            "RACE",
            [candidate],
            [_target()],
        )
    )

    assert result.decisions[0].target_ref == "T1"
    assert len(text_client.requests) == 2


@pytest.mark.parametrize(
    "invalid_reason",
    [
        (
            "C1을 ADD하고 00000000-0000-4000-8000-000000000001의 "
            "sourceCandidateRefs를 저장한다."
        ),
        "MERGED 결과이며 BATCH_LIMIT_EXCEEDED 상태다.",
        "00000000-0000-7000-c000-000000000001 대상을 사용한다.",
        "c1을 add하고 sourcecandidaterefs를 저장한다.",
        "conflict 결과와 targetref를 기록한다.",
        "uuid와 source_candidate_refs를 노출한다.",
        "race 분류의 새 정보다.",
        "canonicalsubjectkey와 category를 기록한다.",
    ],
)
def test_batch_comparator_retries_user_reason_that_exposes_internal_metadata(
    invalid_reason: str,
) -> None:
    candidate = _batch_candidate("C1", "사냥 방식", "무리를 지어 사냥한다.")
    decision = {
        "source_candidate_refs": ["C1"],
        "consolidation_status": "SINGLE",
        "operation": "ADD",
        "target_ref": "T1",
        "proposed_scope_name": "전투 특성",
        "proposed_setting_name": "사냥 방식",
        "proposed_value": "무리를 지어 사냥한다.",
        "comparison_reason": invalid_reason,
    }
    text_client = FakeTextClient(
        [
            {"decisions": [decision]},
            {
                "decisions": [
                    {
                        **decision,
                        "comparison_reason": "기존 고블린에 새 사냥 방식을 추가한다.",
                    }
                ]
            },
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=2).compare_batch(
            "RACE",
            [candidate],
            [_target()],
        )
    )

    assert result.decisions[0].comparison_reason == "기존 고블린에 새 사냥 방식을 추가한다."
    assert len(text_client.requests) == 2


def test_batch_comparator_accepts_normalized_equivalent_source_scope() -> None:
    candidate = _batch_candidate(
        "C1",
        "무기",
        "곤봉과 단검을 함께 사용한다.",
    ).model_copy(update={"scope_name": "전투 특성"})
    target = _target().model_copy(
        update={
            "properties": [
                WorkerWorldSettingProperty(
                    scope_name="전투 특성",
                    setting_name="무기",
                    value="곤봉을 사용한다.",
                )
            ]
        }
    )
    decision = {
        "source_candidate_refs": ["C1"],
        "consolidation_status": "SINGLE",
        "operation": "MERGE",
        "target_ref": "T1",
        "matched_scope_name": "전투 특성",
        "matched_property_name": "무기",
        "proposed_scope_name": "전투 특성",
        "proposed_setting_name": "무기",
        "proposed_value": "곤봉과 단검을 함께 사용한다.",
        "comparison_reason": "기존 무기 정보에 단검 사용 정보를 함께 반영한다.",
    }
    text_client = FakeTextClient([{"decisions": [decision]}])

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
            "RACE",
            [candidate],
            [target],
        )
    )

    assert result.decisions[0].matched_scope_name == "전투 특성"
    assert len(text_client.requests) == 1


def test_batch_comparator_uses_backend_lowercase_not_python_casefold_for_paths() -> None:
    candidate = _batch_candidate("C1", "Straße", "새 값")
    target = _target().model_copy(
        update={
            "properties": [
                WorkerWorldSettingProperty(
                    scope_name="전투 특성",
                    setting_name="Straße",
                    value="기존 값",
                )
            ]
        }
    )
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "consolidation_status": "SINGLE",
                        "operation": "UPDATE",
                        "target_ref": "T1",
                        "matched_scope_name": "전투 특성",
                        "matched_property_name": "STRASSE",
                        "proposed_scope_name": "전투 특성",
                        "proposed_setting_name": "STRASSE",
                        "proposed_value": "새 값",
                        "comparison_reason": "기존 속성의 값을 바꾼다.",
                    }
                ]
            }
        ]
    )

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(
                llm_client=text_client,
                max_attempts=1,
            ).compare_batch("RACE", [candidate], [target])
        )


def test_batch_comparator_replaces_longest_target_ref_without_prefix_corruption() -> None:
    candidate = _batch_candidate("C1", "사냥 방식", "무리를 지어 사냥한다.")
    targets = [
        _target().model_copy(
            update={
                "world_setting_id": UUID(int=100 + index),
                "subject_name": f"대상{index}",
            }
        )
        for index in range(1, 11)
    ]
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T10",
                        "proposed_scope_name": "전투 특성",
                        "proposed_setting_name": "사냥 방식",
                        "proposed_value": "무리를 지어 사냥한다.",
                        "comparison_reason": "T10의 새 사냥 방식으로 정리한다.",
                    }
                ]
            }
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
            "RACE",
            [candidate],
            targets,
        )
    )

    assert "기존 '대상10' 설정" in result.decisions[0].comparison_reason
    assert "설정0" not in result.decisions[0].comparison_reason


def test_batch_comparator_preserves_existing_canonical_path_for_merge() -> None:
    candidates = [
        _batch_candidate("C1", "장비", "곤봉을 사용한다."),
        _batch_candidate("C2", "휴대 무기", "단검도 사용한다."),
    ]
    invalid = {
        "source_candidate_refs": ["C1", "C2"],
        "consolidation_status": "MERGED",
        "operation": "MERGE",
        "target_ref": "T1",
        "matched_property_name": "무기",
        "proposed_setting_name": "장비",
        "proposed_value": "곤봉과 단검을 사용한다.",
        "comparison_reason": "두 무기 정보가 함께 성립한다.",
    }
    valid = {**invalid, "proposed_setting_name": "무기"}
    text_client = FakeTextClient(
        [
            {"decisions": [invalid]},
            {"decisions": [valid]},
        ]
    )
    target = _target().model_copy(
        update={
            "properties": [
                WorkerWorldSettingProperty(
                    scope_name="전투 특성",
                    setting_name="무기",
                    value="곤봉을 사용한다.",
                )
            ]
        }
    )
    invalid["matched_scope_name"] = "전투 특성"
    invalid["proposed_scope_name"] = "전투 특성"
    valid["matched_scope_name"] = "전투 특성"
    valid["proposed_scope_name"] = "전투 특성"

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=2).compare_batch(
            "RACE",
            candidates,
            [target],
        )
    )

    decision = result.decisions[0]
    assert decision.matched_property_name == "무기"
    assert decision.proposed_setting_name == "무기"
    assert len(text_client.requests) == 2


def test_batch_comparator_keeps_independent_fact_as_separate_decision() -> None:
    candidates = [
        _batch_candidate("C1", "사냥 방식", "함정을 설치한다."),
        _batch_candidate("C2", "매복 습성", "숨어서 기다린다."),
        _batch_candidate("C3", "직접 전투력", "완력이 강하다."),
    ]
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1", "C2"],
                        "consolidation_status": "MERGED",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "전투 특성",
                        "proposed_setting_name": "사냥 전술",
                        "proposed_value": "함정을 설치하고 숨어서 기다린다.",
                        "comparison_reason": "두 후보는 같은 사냥 전술을 보완한다.",
                    },
                    {
                        "source_candidate_refs": ["C3"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "전투 특성",
                        "proposed_setting_name": "직접 전투력",
                        "proposed_value": "완력이 강하다.",
                        "comparison_reason": "독립적으로 갱신할 전투 능력이다.",
                    },
                ]
            }
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
            "RACE",
            candidates,
            [_target()],
        )
    )

    assert [decision.source_candidate_refs for decision in result.decisions] == [
        ["C1", "C2"],
        ["C3"],
    ]


def test_batch_comparator_preserves_canonical_paths_for_unscoped_independent_adds() -> None:
    candidates = [
        _batch_candidate("C1", "생명력 수치", "평균 생명력은 150이다."),
        _batch_candidate("C2", "근력 예상치", "근력 기댓값은 100이다."),
        _batch_candidate("C3", "착용 장비 종류", "가죽과 금속 장비를 착용할 수 있다."),
    ]
    candidates = [
        candidate.model_copy(update={"subject_name": "바바리안", "scope_name": None})
        for candidate in candidates
    ]
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "신체 능력",
                        "proposed_setting_name": "생명력",
                        "proposed_value": "평균 생명력은 150이다.",
                        "comparison_reason": "신체 능력 아래 생명력 설정으로 정리한다.",
                    },
                    {
                        "source_candidate_refs": ["C2"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "신체 능력",
                        "proposed_setting_name": "근력 기댓값",
                        "proposed_value": "근력 기댓값은 100이다.",
                        "comparison_reason": "신체 능력 아래 근력 기댓값 설정으로 정리한다.",
                    },
                    {
                        "source_candidate_refs": ["C3"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": None,
                        "proposed_setting_name": "착용 가능 장비",
                        "proposed_value": "가죽과 금속 장비를 착용할 수 있다.",
                        "comparison_reason": "함께 묶을 다른 장비 설정이 없어 루트에 둔다.",
                    },
                ]
            }
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
            "RACE",
            candidates,
            [_target()],
        )
    )

    assert [
        (
            decision.source_candidate_refs,
            decision.consolidation_status,
            decision.operation,
            decision.proposed_scope_name,
            decision.proposed_setting_name,
        )
        for decision in result.decisions
    ] == [
        (["C1"], "SINGLE", "ADD", "신체 능력", "생명력"),
        (["C2"], "SINGLE", "ADD", "신체 능력", "근력 기댓값"),
        (["C3"], "SINGLE", "ADD", None, "착용 가능 장비"),
    ]
    system_prompt = text_client.requests[0]["system_prompt"]
    assert "서로 다른 하위 속성이\n  실제로 둘 이상" in system_prompt
    assert "하위 속성 하나뿐인 범위를 만들지 않는다" in system_prompt
    assert "생명력과 근력을 한 값이나 한 source_candidate_refs로 합치면 안 된다" in system_prompt


def test_batch_comparator_retries_when_projection_leaves_synthetic_scope_singleton() -> None:
    candidates = [
        _batch_candidate("C1", "근력", "근력이 높다.").model_copy(
            update={"scope_name": None}
        ),
        _batch_candidate("C2", "생명력", "생명력이 높다.").model_copy(
            update={"scope_name": None}
        ),
    ]
    target = _target().model_copy(
        update={
            "properties": [
                WorkerWorldSettingProperty(
                    scope_name="기타",
                    setting_name="생명력",
                    value="기존 생명력 값이다.",
                )
            ]
        }
    )
    first_decisions = [
        {
            "source_candidate_refs": ["C1"],
            "consolidation_status": "SINGLE",
            "operation": "ADD",
            "target_ref": "T1",
            "proposed_scope_name": "신체 능력",
            "proposed_setting_name": "근력",
            "proposed_value": "근력이 높다.",
            "comparison_reason": "신체 능력 범위에 근력을 추가한다.",
        },
        {
            "source_candidate_refs": ["C2"],
            "consolidation_status": "SINGLE",
            "operation": "ADD",
            "target_ref": "T1",
            "proposed_scope_name": "신체 능력",
            "proposed_setting_name": "생명력",
            "proposed_value": "생명력이 높다.",
            "comparison_reason": "신체 능력 범위에 생명력을 추가한다.",
        },
    ]
    corrected_decisions = [
        {
            **first_decisions[0],
            "proposed_scope_name": None,
            "comparison_reason": "함께 묶을 형제가 없어 근력을 루트에 추가한다.",
        },
        {
            **first_decisions[1],
            "operation": "REVIEW_REQUIRED",
            "review_reason": "SCOPE_UNRESOLVED",
            "matched_scope_name": "기타",
            "matched_property_name": "생명력",
            "proposed_scope_name": None,
            "comparison_reason": "기존 기타 범위의 생명력과 관련되어 범위 확인이 필요하다.",
        },
    ]
    text_client = FakeTextClient(
        [
            {"decisions": first_decisions},
            {"decisions": corrected_decisions},
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=2).compare_batch(
            "RACE",
            candidates,
            [target],
        )
    )

    assert len(text_client.requests) == 2
    assert result.decisions[0].operation == "ADD"
    assert result.decisions[0].proposed_scope_name is None
    assert result.decisions[1].operation == "REVIEW_REQUIRED"
    retry_payload = json.loads(text_client.requests[1]["user_prompt"])
    assert "at least two distinct final child properties" in retry_payload[
        "validation_feedback"
    ]["reason"]


def test_batch_comparator_allows_new_scope_when_later_candidate_relocates_root_sibling() -> None:
    candidate = _batch_candidate("C1", "근력 기댓값", "근력 기댓값은 100이다.").model_copy(
        update={"subject_name": "바바리안", "scope_name": None}
    )
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name="바바리안",
        properties=[
            WorkerWorldSettingProperty(
                scope_name=None,
                setting_name="생명력",
                value="선택 가능한 종족 중 생명력이 가장 높다.",
            )
        ],
        version=3,
    )
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "matched_scope_name": None,
                        "matched_property_name": None,
                        "proposed_scope_name": "신체 능력",
                        "proposed_setting_name": "근력 기댓값",
                        "proposed_value": "근력 기댓값은 100이다.",
                        "existing_root_property_names_to_move": ["생명력"],
                        "comparison_reason": "기존 생명력과 함께 신체 능력 범위로 정리한다.",
                    }
                ]
            }
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
            "RACE",
            [candidate],
            [target],
        )
    )

    decision = result.decisions[0]
    assert decision.proposed_scope_name == "신체 능력"
    assert decision.existing_root_property_names_to_move == ["생명력"]


def test_batch_comparator_allows_one_add_under_an_existing_scope() -> None:
    candidate = _batch_candidate("C1", "근력 기댓값", "근력 기댓값은 100이다.").model_copy(
        update={"subject_name": "바바리안", "scope_name": None}
    )
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name="바바리안",
        properties=[
            WorkerWorldSettingProperty(
                scope_name="신체 능력",
                setting_name="생명력",
                value="선택 가능한 종족 중 생명력이 가장 높다.",
            )
        ],
        version=3,
    )
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "신체 능력",
                        "proposed_setting_name": "근력 기댓값",
                        "proposed_value": "근력 기댓값은 100이다.",
                        "comparison_reason": "기존 생명력과 같은 신체 능력 범위에 추가한다.",
                    }
                ]
            }
        ]
    )

    result, _ = asyncio.run(
        WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
            "RACE",
            [candidate],
            [target],
        )
    )

    assert result.decisions[0].proposed_scope_name == "신체 능력"
    assert result.decisions[0].existing_root_property_names_to_move == []


def test_batch_comparator_rejects_move_for_a_missing_root_property() -> None:
    candidate = _batch_candidate("C1", "근력 기댓값", "근력 기댓값은 100이다.").model_copy(
        update={"subject_name": "바바리안", "scope_name": None}
    )
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "existing_root_property_names_to_move": ["존재하지 않는 생명력"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "신체 능력",
                        "proposed_setting_name": "근력 기댓값",
                        "proposed_value": "근력 기댓값은 100이다.",
                        "comparison_reason": "존재하지 않는 root 속성을 이동한다.",
                    }
                ]
            }
        ]
    )

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
                "RACE",
                [candidate],
                [_target()],
            )
        )


def test_batch_comparator_rejects_scope_that_conflicts_with_existing_root_scalar() -> None:
    candidate = _batch_candidate("C1", "근력", "근력이 높다.").model_copy(
        update={"scope_name": None}
    )
    target = _target().model_copy(
        update={
            "properties": [
                WorkerWorldSettingProperty(
                    scope_name=None,
                    setting_name="신체",
                    value="강건하다.",
                ),
                WorkerWorldSettingProperty(
                    scope_name=None,
                    setting_name="생명력",
                    value="생명력이 높다.",
                ),
            ]
        }
    )
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "existing_root_property_names_to_move": ["생명력"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "신체",
                        "proposed_setting_name": "근력",
                        "proposed_value": "근력이 높다.",
                        "comparison_reason": "신체 범위에 근력을 추가한다.",
                    }
                ]
            }
        ]
    )

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
                "RACE",
                [candidate],
                [target],
            )
        )


def test_batch_comparator_rejects_root_add_that_conflicts_with_existing_scope() -> None:
    candidate = _batch_candidate("C1", "신체", "신체 능력을 설명한다.").model_copy(
        update={"scope_name": None}
    )
    target = _target().model_copy(
        update={
            "properties": [
                WorkerWorldSettingProperty(
                    scope_name="신체",
                    setting_name="생명력",
                    value="생명력이 높다.",
                )
            ]
        }
    )
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": None,
                        "proposed_setting_name": "신체",
                        "proposed_value": "신체 능력을 설명한다.",
                        "comparison_reason": "기존 범위와 같은 이름의 root 설정이다.",
                    }
                ]
            }
        ]
    )

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
                "RACE",
                [candidate],
                [target],
            )
        )


def test_batch_comparator_rejects_add_for_an_existing_exact_path() -> None:
    candidate = _batch_candidate("C1", "서식지", "새 서식지 값")
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": None,
                        "proposed_setting_name": "서식지",
                        "proposed_value": "새 서식지 값",
                        "comparison_reason": "이미 있는 경로에 다시 추가한다.",
                    }
                ]
            }
        ]
    )

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
                "RACE",
                [candidate],
                [_target()],
            )
        )


def test_batch_comparator_rejects_duplicate_final_paths_across_decisions() -> None:
    candidates = [
        _batch_candidate("C1", "사냥 방식", "함정을 설치한다."),
        _batch_candidate("C2", "매복 방식", "숨어서 기다린다."),
    ]
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": [candidate_ref],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "전투 특성",
                        "proposed_setting_name": "사냥 전술",
                        "proposed_value": value,
                        "comparison_reason": "같은 최종 경로를 중복 제안한다.",
                    }
                    for candidate_ref, value in (
                        ("C1", "함정을 설치한다."),
                        ("C2", "숨어서 기다린다."),
                    )
                ]
            }
        ]
    )

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
                "RACE",
                candidates,
                [_target()],
            )
        )


def test_batch_comparator_rejects_cross_decision_top_level_kind_conflict() -> None:
    candidates = [
        _batch_candidate("C1", "신체", "강건하다.").model_copy(update={"scope_name": None}),
        _batch_candidate("C2", "근력", "근력이 높다.").model_copy(update={"scope_name": None}),
        _batch_candidate("C3", "민첩", "민첩하다.").model_copy(update={"scope_name": None}),
    ]
    decisions = [
        {
            "source_candidate_refs": ["C1"],
            "consolidation_status": "SINGLE",
            "operation": "ADD",
            "target_ref": "T1",
            "proposed_scope_name": None,
            "proposed_setting_name": "신체",
            "proposed_value": "강건하다.",
            "comparison_reason": "신체를 root 설정으로 추가한다.",
        },
        *[
            {
                "source_candidate_refs": [candidate_ref],
                "consolidation_status": "SINGLE",
                "operation": "ADD",
                "target_ref": "T1",
                "proposed_scope_name": "신체",
                "proposed_setting_name": setting_name,
                "proposed_value": proposed_value,
                "comparison_reason": "같은 이름을 scope로 사용하는 구조 충돌이다.",
            }
            for candidate_ref, setting_name, proposed_value in (
                ("C2", "근력", "근력이 높다."),
                ("C3", "민첩", "민첩하다."),
            )
        ],
    ]
    text_client = FakeTextClient([{"decisions": decisions}])

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
                "RACE",
                candidates,
                [_target()],
            )
        )


def test_batch_comparator_rejects_add_that_overlaps_a_move_destination() -> None:
    candidates = [
        _batch_candidate("C1", "근력", "근력이 높다.").model_copy(
            update={"scope_name": None}
        ),
        _batch_candidate("C2", "생명력", "새 생명력 설명이다.").model_copy(
            update={"scope_name": None}
        ),
    ]
    target = _target().model_copy(
        update={
            "properties": [
                WorkerWorldSettingProperty(
                    scope_name=None,
                    setting_name="생명력",
                    value="기존 생명력 값이다.",
                )
            ]
        }
    )
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "existing_root_property_names_to_move": ["생명력"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "신체 능력",
                        "proposed_setting_name": "근력",
                        "proposed_value": "근력이 높다.",
                        "comparison_reason": "기존 생명력과 함께 묶는다.",
                    },
                    {
                        "source_candidate_refs": ["C2"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": "신체 능력",
                        "proposed_setting_name": "생명력",
                        "proposed_value": "새 생명력 설명이다.",
                        "comparison_reason": "이동 목적지와 같은 경로를 추가한다.",
                    },
                ]
            }
        ]
    )

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
                "RACE",
                candidates,
                [target],
            )
        )


@pytest.mark.parametrize(
    ("proposed_scope_name", "proposed_setting_name"),
    [
        ("기능", "기능"),
        ("장비", "착용 가능 장비"),
    ],
    ids=["same-scope-and-setting", "unsupported-singleton-scope"],
)
def test_batch_comparator_rejects_unsupported_generated_singleton_scope(
    proposed_scope_name: str,
    proposed_setting_name: str,
) -> None:
    candidate = _batch_candidate("C1", proposed_setting_name, "지속 설정이다.").model_copy(
        update={"subject_name": "바바리안", "scope_name": None}
    )
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1"],
                        "consolidation_status": "SINGLE",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_scope_name": proposed_scope_name,
                        "proposed_setting_name": proposed_setting_name,
                        "proposed_value": "지속 설정이다.",
                        "comparison_reason": "새 범위로 정리한다.",
                    }
                ]
            }
        ]
    )

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
                "RACE",
                [candidate],
                [_target()],
            )
        )


def test_batch_comparator_rejects_one_decision_that_mixes_explicit_scopes() -> None:
    candidates = [
        _batch_candidate("C1", "사냥 방식", "함정을 설치한다."),
        _batch_candidate("C2", "체격", "몸집이 작다.").model_copy(
            update={"scope_name": "신체 특성"}
        ),
    ]
    text_client = FakeTextClient(
        [
            {
                "decisions": [
                    {
                        "source_candidate_refs": ["C1", "C2"],
                        "consolidation_status": "MERGED",
                        "operation": "ADD",
                        "target_ref": "T1",
                        "proposed_setting_name": "특성",
                        "proposed_value": "함정을 설치하며 몸집이 작다.",
                        "comparison_reason": "서로 다른 범위를 잘못 묶었다.",
                    }
                ]
            }
        ]
    )

    with pytest.raises(ComparisonValidationError):
        asyncio.run(
            WorldSettingComparator(llm_client=text_client, max_attempts=1).compare_batch(
                "RACE",
                candidates,
                [_target()],
            )
        )


@pytest.mark.parametrize(
    ("operation", "candidate_setting_name", "candidate_value", "decision_overrides"),
    [
        ("ADD", "사회 구조", "부족 단위", {"target_ref": "T1"}),
        (
            "UPDATE",
            "서식지",
            "극지방",
            {"target_ref": "T1", "matched_property_name": "서식지"},
        ),
        (
            "MERGE",
            "서식지",
            "극지방",
            {
                "target_ref": "T1",
                "matched_property_name": "서식지",
                "proposed_value": "혹한 지역과 극지방",
            },
        ),
        ("EXCLUDE", "현재 소유자", "수아", {}),
        (
            "EXCLUDE",
            "새 서식지 설명",
            "혹한 지역에 산다.",
            {"target_ref": "T1", "matched_property_name": "서식지"},
        ),
    ],
)
def test_comparator_accepts_each_supported_operation(
    operation: str,
    candidate_setting_name: str,
    candidate_value: str,
    decision_overrides: dict,
) -> None:
    candidate = _candidate(candidate_setting_name, candidate_value)
    decision = {
        "consolidation_status": "SINGLE",
        "operation": operation,
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": candidate_setting_name,
        "proposed_value": candidate_value,
        "comparison_reason": "테스트 비교 이유",
        **decision_overrides,
    }
    text_client = FakeTextClient([decision])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.operation == operation
    user_prompt = text_client.requests[0]["user_prompt"]
    assert "T1" in user_prompt
    assert str(TARGET_ID) not in user_prompt
    prompt_payload = json.loads(user_prompt)
    assert prompt_payload["candidate"]["evidence_spans"] == [
        {"quote": "원문 근거", "start_offset": None, "end_offset": None}
    ]


def test_comparator_restores_single_extracted_value_without_retry() -> None:
    candidate = _candidate("현재 소유자", "수아")
    invalid = {
        "consolidation_status": "SINGLE",
        "operation": "EXCLUDE",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": "현재 소유자",
        "proposed_value": "다른 값",
        "comparison_reason": "일시적 소유 상태다.",
    }
    text_client = FakeTextClient([invalid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.proposed_value == "수아"
    assert result.consolidation_status == "SINGLE"
    assert len(text_client.requests) == 1


def test_comparator_preserves_single_candidate_merge_value_when_conflict_is_normalized() -> None:
    candidate = _candidate("서식지", "극지방")
    merged_value = "혹한 지역과 극지방"
    text_client = FakeTextClient([{
        "consolidation_status": "CONFLICT",
        "operation": "MERGE",
        "target_ref": "T1",
        "matched_property_name": "서식지",
        "proposed_setting_name": "서식지",
        "proposed_value": merged_value,
        "comparison_reason": "기존 서식지 설명과 신규 정보를 함께 보존한다.",
    }])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.consolidation_status == "SINGLE"
    assert result.operation == "MERGE"
    assert result.proposed_value == merged_value


def test_comparator_restores_add_identity_fields_without_retry() -> None:
    candidate = _candidate("도달 가능성", "미궁 진입 직후 외곽으로 떨어질 수 있다.")
    invalid = {
        "consolidation_status": "MERGED",
        "operation": "ADD",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_scope_name": "다른 범위",
        "proposed_setting_name": "다듬은 설정명",
        "proposed_value": "미궁 외곽으로 이동할 가능성이 있다.",
        "comparison_reason": "기존에 없는 설정이다.",
    }
    text_client = FakeTextClient([invalid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.consolidation_status == "SINGLE"
    assert result.proposed_scope_name == candidate.scope_name
    assert result.proposed_setting_name == candidate.setting_name
    assert result.proposed_value == candidate.extracted_value


def test_comparator_passes_validation_reason_to_retry() -> None:
    candidate = _candidate("서식지", "극지방")
    invalid = {
        "consolidation_status": "SINGLE",
        "operation": "UPDATE",
        "target_ref": "T1",
        "matched_property_name": "존재하지 않는 속성",
        "proposed_setting_name": "존재하지 않는 속성",
        "proposed_value": "극지방",
        "comparison_reason": "기존 서식지를 바꾼다.",
    }
    valid = {
        **invalid,
        "matched_property_name": "서식지",
        "proposed_setting_name": "서식지",
    }
    text_client = FakeTextClient([invalid, valid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=2,
        ).compare(candidate, [_target()])
    )

    assert result.matched_property_name == "서식지"
    first_prompt = json.loads(text_client.requests[0]["user_prompt"])
    retry_prompt = json.loads(text_client.requests[1]["user_prompt"])
    assert "validation_feedback" not in first_prompt
    assert retry_prompt["validation_feedback"]["previous_response_rejected"] is True
    assert "does not exist" in retry_prompt["validation_feedback"]["reason"]


def test_comparator_consolidates_multiple_same_key_values_for_add() -> None:
    source_values = [
        "공명시킨 메시지 스톤끼리 대화할 수 있다.",
        "짧게 읊조려 신호를 보낼 수 있다.",
        "조작해 연락 내용을 수신할 수 있다.",
    ]
    candidate = _candidate("기능", "\n".join(source_values))
    merged_value = (
        "공명시킨 메시지 스톤끼리 대화할 수 있으며, 짧게 읊조려 신호를 보내고 "
        "조작해 연락 내용을 수신할 수 있다."
    )
    text_client = FakeTextClient([{
        "consolidation_status": "MERGED",
        "operation": "ADD",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": "기능",
        "proposed_value": merged_value,
        "comparison_reason": "같은 기능을 설명하는 원문 근거를 하나의 설정으로 정리했다.",
    }])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [])
    )

    assert result.proposed_value == merged_value
    prompt_payload = json.loads(text_client.requests[0]["user_prompt"])
    assert prompt_payload["candidate"]["extracted_values"] == source_values


def test_comparator_retries_when_exclude_matches_property_without_target() -> None:
    candidate = _candidate("새 서식지 설명", "혹한 지역에 산다.")
    invalid = {
        "consolidation_status": "SINGLE",
        "operation": "EXCLUDE",
        "target_ref": None,
        "matched_property_name": "서식지",
        "proposed_setting_name": "새 서식지 설명",
        "proposed_value": "혹한 지역에 산다.",
        "comparison_reason": "기존 서식지와 같은 내용이다.",
    }
    valid = {**invalid, "target_ref": "T1"}
    text_client = FakeTextClient([invalid, valid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=2,
        ).compare(candidate, [_target()])
    )

    assert result.target_ref == "T1"
    assert result.matched_property_name == "서식지"
    assert len(text_client.requests) == 2


def test_comparator_replaces_internal_target_reference_in_user_facing_reason() -> None:
    candidate = _candidate("서식지", "극지방")
    text_client = FakeTextClient([
        {
            "consolidation_status": "SINGLE",
            "operation": "MERGE",
            "target_ref": "T1",
            "matched_property_name": "서식지",
            "proposed_setting_name": "서식지",
            "proposed_value": "혹한 지역과 극지방",
            "comparison_reason": "T1의 기존 서식지와 모순되지 않아 병합한다.",
        }
    ])

    result, raw_result = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [_target()])
    )

    assert result.comparison_reason == "기존 '바바리안' 설정의 기존 서식지와 모순되지 않아 병합한다."
    assert raw_result["comparison_reason"] == result.comparison_reason
    assert "T1" not in result.comparison_reason


def test_comparator_preserves_conflicting_values_for_user_review() -> None:
    source_values = ["통신 반경은 약 300m다.", "통신 반경은 약 3km다."]
    candidate = _candidate("통신 반경", "\n".join(source_values))
    text_client = FakeTextClient([{
        "consolidation_status": "CONFLICT",
        "operation": "ADD",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": "통신 반경",
        "proposed_value": "\n".join(source_values),
        "comparison_reason": "원문마다 통신 반경이 달라 최종값 확인이 필요하다.",
    }])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [])
    )

    assert result.consolidation_status == "CONFLICT"
    assert result.proposed_value == candidate.extracted_value


def test_comparator_restores_conflicting_source_values_without_retry() -> None:
    candidate = _candidate("통신 반경", "약 300m\n약 3km")
    invalid = {
        "consolidation_status": "CONFLICT",
        "operation": "ADD",
        "target_ref": None,
        "matched_property_name": None,
        "proposed_setting_name": "통신 반경",
        "proposed_value": "약 300m 또는 약 3km",
        "comparison_reason": "두 수치가 달라 확인이 필요하다.",
    }
    text_client = FakeTextClient([invalid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [])
    )

    assert result.proposed_value == candidate.extracted_value
    assert len(text_client.requests) == 1


def test_comparator_never_matches_same_setting_name_from_a_different_scope() -> None:
    candidate = _candidate(
        "방향별 몬스터 출몰 규칙",
        "동쪽에서 고블린이 출몰한다.",
        scope_name="1층",
    )
    invalid = {
        "consolidation_status": "SINGLE",
        "operation": "UPDATE",
        "target_ref": "T1",
        "matched_scope_name": "2층",
        "matched_property_name": "방향별 몬스터 출몰 규칙",
        "proposed_scope_name": "2층",
        "proposed_setting_name": "방향별 몬스터 출몰 규칙",
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "다른 층의 기존 설정을 갱신한다.",
    }
    valid = {
        **invalid,
        "matched_scope_name": "1층",
        "proposed_scope_name": "1층",
        "comparison_reason": "1층의 기존 설정을 갱신한다.",
    }
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name="미궁",
        properties=[
            WorkerWorldSettingProperty(
                scope_name="1층",
                setting_name="방향별 몬스터 출몰 규칙",
                value="동쪽에서 슬라임이 출몰한다.",
            ),
            WorkerWorldSettingProperty(
                scope_name="2층",
                setting_name="방향별 몬스터 출몰 규칙",
                value="동쪽에서 오크가 출몰한다.",
            ),
        ],
        version=3,
    )
    text_client = FakeTextClient([invalid, valid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=2,
        ).compare(candidate, [target])
    )

    assert result.matched_scope_name == "1층"
    assert result.proposed_scope_name == "1층"
    prompt_payload = json.loads(text_client.requests[0]["user_prompt"])
    assert prompt_payload["candidate"]["scope_name"] == "1층"
    assert [property["scope_name"] for property in prompt_payload["targets"][0]["properties"]] == [
        "1층",
        "2층",
    ]
    assert len(text_client.requests) == 2


@pytest.mark.parametrize("operation", ["UPDATE", "MERGE", "EXCLUDE", "REVIEW_REQUIRED"])
def test_comparator_turns_unscoped_same_name_match_into_scope_review(
    operation: str,
) -> None:
    candidate = _candidate("광원", "벽의 수정들이 주변을 밝힌다.")
    decision = {
        "consolidation_status": "SINGLE",
        "operation": operation,
        "review_reason": "SCOPE_UNRESOLVED" if operation == "REVIEW_REQUIRED" else None,
        "target_ref": "T1",
        "matched_scope_name": "1층",
        "matched_property_name": "광원",
        "proposed_scope_name": "1층",
        "proposed_setting_name": "광원",
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "1층의 기존 광원 설정과 관련된다.",
    }
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name="미궁",
        properties=[
            WorkerWorldSettingProperty(
                scope_name="1층",
                setting_name="광원",
                value="벽의 수정들이 광원 역할을 한다.",
            )
        ],
        version=2,
    )
    text_client = FakeTextClient([decision])

    result, raw_result = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [target])
    )

    assert result.operation == "REVIEW_REQUIRED"
    assert result.review_reason == "SCOPE_UNRESOLVED"
    assert result.matched_scope_name == "1층"
    assert result.matched_property_name == "광원"
    assert result.proposed_scope_name is None
    assert result.proposed_setting_name == "광원"
    assert result.proposed_value == candidate.extracted_value
    assert "범위 확인" in result.comparison_reason
    assert raw_result == result.model_dump(mode="json")
    assert len(text_client.requests) == 1


@pytest.mark.parametrize("operation", ["UPDATE", "MERGE", "EXCLUDE"])
def test_comparator_scopes_root_property_check_to_selected_target(
    operation: str,
) -> None:
    candidate = _candidate("광원", "벽의 수정들이 주변을 밝힌다.")
    decision = {
        "consolidation_status": "SINGLE",
        "operation": operation,
        "target_ref": "T2",
        "matched_scope_name": "1층",
        "matched_property_name": "광원",
        "proposed_scope_name": "1층",
        "proposed_setting_name": "광원",
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "1층의 기존 광원 설정과 관련된다.",
    }
    unrelated_target = WorkerWorldSettingComparisonTarget(
        world_setting_id=UUID("00000000-0000-0000-0000-000000000005"),
        subject_name="성곽",
        properties=[
            WorkerWorldSettingProperty(
                scope_name=None,
                setting_name="광원",
                value="마법등이 주변을 밝힌다.",
            )
        ],
        version=1,
    )
    selected_target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name=candidate.subject_name,
        properties=[
            WorkerWorldSettingProperty(
                scope_name="1층",
                setting_name="광원",
                value="벽의 수정들이 광원 역할을 한다.",
            )
        ],
        version=2,
    )
    text_client = FakeTextClient([decision])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [unrelated_target, selected_target])
    )

    assert result.operation == "REVIEW_REQUIRED"
    assert result.review_reason == "SCOPE_UNRESOLVED"
    assert result.target_ref == "T2"
    assert result.matched_scope_name == "1층"
    assert result.matched_property_name == "광원"


def test_comparator_preserves_unmatched_exclusion_during_scope_normalization() -> None:
    candidate = _candidate("광원", "잠시 불빛이 번쩍였다.")
    decision = {
        "consolidation_status": "SINGLE",
        "operation": "EXCLUDE",
        "target_ref": None,
        "matched_scope_name": None,
        "matched_property_name": None,
        "proposed_scope_name": None,
        "proposed_setting_name": "광원",
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "일시적인 사건이어서 확정 설정으로 반영하지 않는다.",
    }
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name=candidate.subject_name,
        properties=[
            WorkerWorldSettingProperty(
                scope_name="1층",
                setting_name="광원",
                value="벽의 수정들이 광원 역할을 한다.",
            )
        ],
        version=2,
    )
    text_client = FakeTextClient([decision])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [target])
    )

    assert result.operation == "EXCLUDE"
    assert result.review_reason is None
    assert result.target_ref is None
    assert result.matched_scope_name is None
    assert result.matched_property_name is None


def test_comparator_does_not_assign_targetless_add_to_possible_subject() -> None:
    candidate = _candidate("광원", "천장의 수정이 주변을 밝힌다.")
    decision = {
        "consolidation_status": "SINGLE",
        "operation": "ADD",
        "target_ref": None,
        "matched_scope_name": None,
        "matched_property_name": None,
        "proposed_scope_name": None,
        "proposed_setting_name": "광원",
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "새 대상의 루트 광원 설정으로 추가한다.",
    }
    possible_targets = [
        WorkerWorldSettingComparisonTarget(
            world_setting_id=UUID("00000000-0000-0000-0000-000000000005"),
            subject_name="동부 바바리안",
            properties=[
                WorkerWorldSettingProperty(
                    scope_name="1층",
                    setting_name="광원",
                    value="마법등이 주변을 밝힌다.",
                )
            ],
            version=1,
        ),
        WorkerWorldSettingComparisonTarget(
            world_setting_id=UUID("00000000-0000-0000-0000-000000000006"),
            subject_name="서부 바바리안",
            properties=[
                WorkerWorldSettingProperty(
                    scope_name="2층",
                    setting_name="광원",
                    value="횃불이 주변을 밝힌다.",
                )
            ],
            version=1,
        ),
    ]
    text_client = FakeTextClient([decision])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, possible_targets)
    )

    assert result.operation == "ADD"
    assert result.review_reason is None
    assert result.target_ref is None
    assert result.matched_scope_name is None
    assert result.matched_property_name is None


def test_comparator_detects_scope_ambiguity_when_model_returns_root_add() -> None:
    candidate = _candidate("광원", "벽의 수정들이 주변을 밝힌다.")
    decision = {
        "consolidation_status": "SINGLE",
        "operation": "ADD",
        "target_ref": None,
        "matched_scope_name": None,
        "matched_property_name": None,
        "proposed_scope_name": None,
        "proposed_setting_name": "광원",
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "루트에 새 광원 설정을 추가한다.",
    }
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name=candidate.subject_name,
        properties=[
            WorkerWorldSettingProperty(
                scope_name="1층",
                setting_name="광원",
                value="벽의 수정들이 광원 역할을 한다.",
            )
        ],
        version=2,
    )
    text_client = FakeTextClient([decision])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [target])
    )

    assert result.operation == "REVIEW_REQUIRED"
    assert result.review_reason == "SCOPE_UNRESOLVED"
    assert result.target_ref == "T1"
    assert result.matched_scope_name == "1층"
    assert result.matched_property_name == "광원"
    assert result.proposed_scope_name is None
    assert len(text_client.requests) == 1


def test_comparator_does_not_mark_scope_unresolved_when_same_root_property_exists() -> None:
    candidate = _candidate("광원", "천장의 수정이 주변을 밝힌다.")
    decision = {
        "consolidation_status": "SINGLE",
        "operation": "ADD",
        "target_ref": None,
        "matched_scope_name": None,
        "matched_property_name": None,
        "proposed_scope_name": None,
        "proposed_setting_name": "광원",
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "루트 광원 설정을 추가한다.",
    }
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name="미궁",
        properties=[
            WorkerWorldSettingProperty(
                scope_name=None,
                setting_name="광원",
                value="마법등이 주변을 밝힌다.",
            ),
            WorkerWorldSettingProperty(
                scope_name="1층",
                setting_name="광원",
                value="벽의 수정들이 광원 역할을 한다.",
            ),
        ],
        version=2,
    )
    text_client = FakeTextClient([decision])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=1,
        ).compare(candidate, [target])
    )

    assert result.operation == "ADD"
    assert result.review_reason is None
    assert result.matched_scope_name is None
    assert result.matched_property_name is None


def test_comparator_still_retries_unscoped_match_to_different_setting_name() -> None:
    candidate = _candidate("광원", "벽의 수정들이 주변을 밝힌다.")
    invalid = {
        "consolidation_status": "SINGLE",
        "operation": "UPDATE",
        "target_ref": "T1",
        "matched_scope_name": "1층",
        "matched_property_name": "조도",
        "proposed_scope_name": "1층",
        "proposed_setting_name": "조도",
        "proposed_value": candidate.extracted_value,
        "comparison_reason": "다른 이름의 설정을 갱신한다.",
    }
    valid = {
        **invalid,
        "operation": "ADD",
        "target_ref": "T1",
        "matched_scope_name": None,
        "matched_property_name": None,
        "proposed_scope_name": None,
        "proposed_setting_name": "광원",
        "comparison_reason": "루트의 새 광원 설정으로 추가한다.",
    }
    target = WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name="미궁",
        properties=[
            WorkerWorldSettingProperty(
                scope_name="1층",
                setting_name="조도",
                value="희미하다.",
            )
        ],
        version=2,
    )
    text_client = FakeTextClient([invalid, valid])

    result, _ = asyncio.run(
        WorldSettingComparator(
            llm_client=text_client,
            max_attempts=2,
        ).compare(candidate, [target])
    )

    assert result.operation == "ADD"
    assert len(text_client.requests) == 2


class FakeTextClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    async def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        return LlmTextResponse(
            text=json.dumps(self.responses.pop(0), ensure_ascii=False),
        )


def _candidate(
    setting_name: str,
    extracted_value: str,
    scope_name: str | None = None,
) -> WorkerWorldSettingCandidatePayload:
    return WorkerWorldSettingCandidatePayload.model_validate(
        {
            "candidateId": "00000000-0000-0000-0000-000000000003",
            "workId": "00000000-0000-0000-0000-000000000010",
            "sourceEpisodeId": "00000000-0000-0000-0000-000000000011",
            "category": "RACE",
            "subjectName": "바바리안",
            "scopeName": scope_name,
            "settingName": setting_name,
            "extractedValue": extracted_value,
            "evidenceSpans": [{"quote": "원문 근거"}],
            "extractionConfidence": 0.95,
        }
    )


def _batch_candidate(
    candidate_ref: str,
    setting_name: str,
    extracted_value: str,
) -> WorkerWorldSettingComparisonBatchCandidate:
    candidate_number = 30 + int(candidate_ref[1:])
    return WorkerWorldSettingComparisonBatchCandidate.model_validate(
        {
            "candidateRef": candidate_ref,
            "candidateId": f"00000000-0000-0000-0000-{candidate_number:012d}",
            "subjectName": "고블린",
            "scopeName": "전투 특성",
            "settingName": setting_name,
            "extractedValue": extracted_value,
            "evidenceSpans": [{"quote": "원문 근거"}],
            "extractionConfidence": 0.95,
        }
    )


def _target() -> WorkerWorldSettingComparisonTarget:
    return WorkerWorldSettingComparisonTarget(
        world_setting_id=TARGET_ID,
        subject_name="바바리안",
        properties=[
            WorkerWorldSettingProperty(
                scope_name=None,
                setting_name="서식지",
                value="혹한 지역",
            )
        ],
        version=3,
    )
