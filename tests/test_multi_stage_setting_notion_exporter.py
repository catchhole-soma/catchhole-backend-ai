import pytest

from evals.multi_stage_setting.contracts import ReviewStatus, WorldStage1Gold
from evals.multi_stage_setting.notion_exporter import (
    CURRENT_OUTCOME_PROPERTY_SCHEMA,
    LEGACY_OUTCOME_PROPERTY_SCHEMA,
    SCENARIO_PROPERTY_SCHEMA,
    STAGE1_PROPERTY_SCHEMA,
    STAGE2_PROPERTY_SCHEMA,
    _parse_stage1,
    _resolve_world_proposal,
    build_gold_snapshot_v3,
    validate_notion_v3_schemas,
)


def test_notion_v3_export_resolves_relations_dependency_chain_and_new_outcome_facts() -> None:
    scenario_pages = [
        _scenario_page("scenario-1", "S1", 1, status="FINAL", candidate_free=True),
        _scenario_page(
            "scenario-2",
            "S2",
            2,
            status="FINAL",
            previous_page_id="scenario-1",
        ),
    ]
    stage1 = _character_stage1_page("stage1-2", "C2", "scenario-2", status="FINAL")
    stage2 = _character_stage2_page(
        "stage2-2",
        "D2",
        "scenario-2",
        "stage1-2",
        status="FINAL",
        required="비요른 | 종족 | 바바리안\n비요른 | 종족 | 바바리안",
        forbidden="비요른 | 종족 | 엘프",
    )

    snapshot = build_gold_snapshot_v3(
        scenario_pages,
        [stage1],
        [stage2],
        dataset_name="v3 test",
        episode_numbers={2},
    )

    assert [item.scenario_id for item in snapshot.scenarios] == ["S1", "S2"]
    assert snapshot.evaluation_scenario_ids == ["S2"]
    assert snapshot.scenarios[1].previous_scenario_id == "S1"
    assert snapshot.stage2[0].source_gold_ids == ["C2"]
    assert snapshot.stage2[0].required_facts == ["비요른 | 종족 | 바바리안"]
    assert snapshot.stage2[0].forbidden_facts == ["비요른 | 종족 | 엘프"]
    assert snapshot.stage1[0].value_json == {"value": "바바리안"}
    assert snapshot.stage1[0].value_json_provenance == "GENERATED_SCALAR"
    assert snapshot.scorable is True
    assert snapshot.fixture_hash == snapshot.computed_fixture_hash()


def test_character_fact_key_aliases_accept_one_alias_per_line() -> None:
    scenario = _scenario_page("scenario-1", "S1", 1, status="FINAL")
    stage1 = _character_stage1_page(
        "stage1-1", "C1", "scenario-1", status="FINAL"
    )
    stage1["properties"]["허용 factKey 별칭"] = _rich_text(
        "status.오른발_중상\nstatus.오른발_손상\nstatus.오른발_중상"
    )
    stage2 = _character_stage2_page(
        "stage2-1",
        "D1",
        "scenario-1",
        "stage1-1",
        status="FINAL",
    )

    snapshot = build_gold_snapshot_v3(
        [scenario],
        [stage1],
        [stage2],
        dataset_name="line aliases",
    )

    assert snapshot.stage1[0].accepted_fact_key_aliases == [
        "status.오른발_중상",
        "status.오른발_손상",
    ]


def test_world_setting_name_aliases_reuse_the_shared_alias_column() -> None:
    page = _character_stage1_page(
        "stage1-world", "W1", "scenario-1", status="FINAL"
    )
    properties = page["properties"]
    properties.update(
        {
            "도메인": _select("WORLD"),
            "candidateKind": _select("WORLD_SETTING"),
            "worldCategory": _select("MONSTER"),
            "worldSubject": _rich_text("고블린"),
            "worldScope": _rich_text("전투 특성"),
            "worldSettingName": _rich_text("함정 사용"),
            "허용 factKey 별칭": _rich_text(
                "함정 습성\n함정 활용\n함정 습성"
            ),
            "정답 표시값": _rich_text("고블린은 함정을 설치한다."),
        }
    )

    row = _parse_stage1(page, properties, "S1", 1)

    assert isinstance(row, WorldStage1Gold)
    assert row.setting_name == "함정 사용"
    assert row.accepted_setting_name_aliases == ["함정 습성", "함정 활용"]


def test_notion_v3_export_rejects_conflicting_new_and_legacy_claim_columns() -> None:
    scenario = _scenario_page("scenario-1", "S1", 1, status="FINAL")
    stage1 = _character_stage1_page("stage1-1", "C1", "scenario-1", status="FINAL")
    stage2 = _character_stage2_page(
        "stage2-1",
        "D1",
        "scenario-1",
        "stage1-1",
        status="FINAL",
        required="비요른 | 종족 | 바바리안",
    )
    stage2["properties"]["추가 Claim"] = _rich_text("비요른 | 종족 | 엘프")

    with pytest.raises(ValueError, match="conflicting 반영 결과 필수 사실"):
        build_gold_snapshot_v3(
            [scenario],
            [stage1],
            [stage2],
            dataset_name="conflict",
        )


def test_draft_rows_require_explicit_status_and_are_marked_non_scorable() -> None:
    scenario = _scenario_page("scenario-1", "S1", 1, status="DRAFT")
    stage1 = _character_stage1_page("stage1-1", "C1", "scenario-1", status="DRAFT")
    stage2 = _character_stage2_page(
        "stage2-1",
        "D1",
        "scenario-1",
        "stage1-1",
        status="DRAFT",
    )

    with pytest.raises(ValueError, match="No Scenario rows matched"):
        build_gold_snapshot_v3(
            [scenario],
            [stage1],
            [stage2],
            dataset_name="draft",
        )

    snapshot = build_gold_snapshot_v3(
        [scenario],
        [stage1],
        [stage2],
        dataset_name="draft",
        review_statuses={ReviewStatus.DRAFT},
    )

    assert snapshot.scorable is False
    assert snapshot.scenarios[0].review_status == "DRAFT"


def test_stage2_scalar_proposed_json_is_generated_and_conflicting_annotation_rejected() -> None:
    scenario = _scenario_page("scenario-1", "S1", 1, status="FINAL")
    stage1 = _character_stage1_page("stage1-1", "C1", "scenario-1", status="FINAL")
    stage2 = _character_stage2_page(
        "stage2-1",
        "D1",
        "scenario-1",
        "stage1-1",
        status="FINAL",
    )
    stage2["properties"]["proposedValueJson"] = _rich_text("")

    snapshot = build_gold_snapshot_v3(
        [scenario], [stage1], [stage2], dataset_name="generated json"
    )

    assert snapshot.stage2[0].proposed_value_json == {"value": "바바리안"}

    stage2["properties"]["proposedValueJson"] = _rich_text('{"value":"엘프"}')
    with pytest.raises(ValueError, match="differs from its scalar value"):
        build_gold_snapshot_v3(
            [scenario], [stage1], [stage2], dataset_name="invalid json"
        )


@pytest.mark.parametrize("draft_stage", ["stage1", "stage2"])
def test_final_export_rejects_linked_non_final_child_rows(draft_stage: str) -> None:
    scenario = _scenario_page("scenario-1", "S1", 1, status="FINAL")
    stage1 = _character_stage1_page(
        "stage1-1",
        "C1",
        "scenario-1",
        status="DRAFT" if draft_stage == "stage1" else "FINAL",
    )
    stage2 = _character_stage2_page(
        "stage2-1",
        "D1",
        "scenario-1",
        "stage1-1",
        status="DRAFT" if draft_stage == "stage2" else "FINAL",
    )

    with pytest.raises(ValueError, match=f"non-FINAL {draft_stage.title()}"):
        build_gold_snapshot_v3(
            [scenario], [stage1], [stage2], dataset_name="mixed review status"
        )


def test_candidate_free_false_requires_at_least_one_extract_row() -> None:
    scenario = _scenario_page(
        "scenario-1", "S1", 1, status="FINAL", candidate_free=False
    )

    with pytest.raises(ValueError, match="Invalid v3 Notion snapshot"):
        build_gold_snapshot_v3([scenario], [], [], dataset_name="missing candidates")


def test_optional_scenario_episode_title_is_exported_without_becoming_schema_required() -> None:
    scenario = _scenario_page(
        "scenario-1", "S1", 1, status="FINAL", candidate_free=True
    )
    scenario["properties"]["회차 제목"] = _rich_text("게임 속으로")

    snapshot = build_gold_snapshot_v3(
        [scenario], [], [], dataset_name="optional episode title"
    )

    assert snapshot.scenarios[0].episode_title == "게임 속으로"
    assert "회차 제목" not in SCENARIO_PROPERTY_SCHEMA


def test_optional_evaluation_batch_is_exported_for_operational_prior_context() -> None:
    scenario = _scenario_page(
        "scenario-1", "GB-EP01", 1, status="FINAL", candidate_free=True
    )
    scenario["properties"]["평가 Batch"] = _rich_text("upload-batch-2026-08")

    snapshot = build_gold_snapshot_v3(
        [scenario], [], [], dataset_name="optional evaluation batch"
    )

    assert snapshot.scenarios[0].evaluation_batch_id == "upload-batch-2026-08"
    assert "평가 Batch" not in SCENARIO_PROPERTY_SCHEMA


def test_stage_rows_derive_episode_from_scenario_relation_when_cell_is_blank() -> None:
    scenario = _scenario_page("scenario-2", "GB-EP02", 2, status="FINAL")
    stage1 = _character_stage1_page(
        "stage1-2", "GB-EP02-C-001", "scenario-2", status="FINAL"
    )
    stage2 = _character_stage2_page(
        "stage2-2",
        "GB-EP02-D-001",
        "scenario-2",
        "stage1-2",
        status="FINAL",
    )
    stage1["properties"]["회차"] = {"type": "number", "number": None}
    stage2["properties"]["회차"] = {"type": "number", "number": None}

    snapshot = build_gold_snapshot_v3(
        [scenario], [stage1], [stage2], dataset_name="derived episode"
    )

    assert snapshot.stage1[0].episode_no == 2
    assert snapshot.stage2[0].episode_no == 2


def test_stage_row_rejects_episode_that_differs_from_scenario_relation() -> None:
    scenario = _scenario_page("scenario-2", "GB-EP02", 2, status="FINAL")
    stage1 = _character_stage1_page(
        "stage1-2", "GB-EP02-C-001", "scenario-2", status="FINAL"
    )
    stage1["properties"]["회차"] = _number(3)

    with pytest.raises(ValueError, match="differs from its Scenario relation"):
        build_gold_snapshot_v3(
            [scenario], [stage1], [], dataset_name="mismatched episode"
        )


def test_scenario_context_accepts_plain_human_friendly_character_names() -> None:
    scenario = _scenario_page("scenario-1", "S1", 1, status="DRAFT", candidate_free=True)
    scenario["properties"]["1차 제공 컨텍스트"] = _rich_text("비요른\n미샤")

    snapshot = build_gold_snapshot_v3(
        [scenario],
        [],
        [],
        dataset_name="plain context",
        review_statuses={ReviewStatus.DRAFT},
    )

    assert snapshot.scenarios[0].known_character_names == ["비요른", "미샤"]


@pytest.mark.parametrize(
    ("state_status", "before_hash"),
    [("PENDING", "a" * 64), ("VERIFIED", "")],
)
def test_final_external_seed_requires_verified_status_and_before_state_hash(
    state_status: str,
    before_hash: str,
) -> None:
    scenario = _scenario_page(
        "scenario-1", "S1", 1, status="FINAL", candidate_free=True
    )
    scenario["properties"]["시작 상태 방식"] = _select("SEED")
    scenario["properties"]["상태 생성 상태"] = _select(state_status)
    scenario["properties"]["beforeState URI"] = _rich_text("seed/S1.before.json")
    scenario["properties"]["beforeState Hash"] = _rich_text(before_hash)

    with pytest.raises(ValueError, match="FINAL external SEED"):
        build_gold_snapshot_v3([scenario], [], [], dataset_name="external seed")


def test_final_external_seed_accepts_verified_hashed_fixture() -> None:
    scenario = _scenario_page(
        "scenario-1", "S1", 1, status="FINAL", candidate_free=True
    )
    scenario["properties"]["시작 상태 방식"] = _select("SEED")
    scenario["properties"]["상태 생성 상태"] = _select("VERIFIED")
    scenario["properties"]["beforeState URI"] = _rich_text("seed/S1.before.json")
    scenario["properties"]["beforeState Hash"] = _rich_text("a" * 64)

    snapshot = build_gold_snapshot_v3(
        [scenario], [], [], dataset_name="verified external seed"
    )

    assert snapshot.scenarios[0].before_state_hash == "a" * 64


@pytest.mark.parametrize(
    ("value_type", "display_value", "explicit_json", "message"),
    [
        ("STRING", "바바리안", '{"value":"엘프"}', "differs from its scalar"),
        ("NUMBER", "NaN", "", "finite NUMBER"),
        ("NUMBER", "1", '{"value":NaN}', "differs from its scalar"),
    ],
)
def test_stage1_scalar_json_and_number_boundaries_are_strict(
    value_type: str,
    display_value: str,
    explicit_json: str,
    message: str,
) -> None:
    scenario = _scenario_page("scenario-1", "S1", 1, status="FINAL")
    stage1 = _character_stage1_page("stage1-1", "C1", "scenario-1", status="FINAL")
    stage1["properties"]["valueType"] = _select(value_type)
    stage1["properties"]["정답 표시값"] = _rich_text(display_value)
    stage1["properties"]["정답 valueJson"] = _rich_text(explicit_json)

    with pytest.raises(ValueError, match=message):
        build_gold_snapshot_v3([scenario], [stage1], [], dataset_name="scalar boundary")


@pytest.mark.parametrize("value_type", ["JSON", "UNKNOWN"])
def test_json_like_character_mutation_requires_annotated_proposed_json(
    value_type: str,
) -> None:
    scenario = _scenario_page("scenario-1", "S1", 1, status="FINAL")
    stage1 = _character_stage1_page("stage1-1", "C1", "scenario-1", status="FINAL")
    stage1["properties"]["valueType"] = _select(value_type)
    stage1["properties"]["정답 표시값"] = _rich_text("복합 구조")
    stage1["properties"]["정답 valueJson"] = _rich_text('{"parts":["복합","구조"]}')
    stage2 = _character_stage2_page(
        "stage2-1", "D1", "scenario-1", "stage1-1", status="FINAL"
    )
    stage2["properties"]["proposedValue"] = _rich_text("갱신된 복합 구조")
    stage2["properties"]["proposedValueJson"] = _rich_text("")

    with pytest.raises(ValueError, match="requires proposedValueJson for JSON/UNKNOWN"):
        build_gold_snapshot_v3(
            [scenario], [stage1], [stage2], dataset_name="json mutation"
        )


def test_notion_v3_schema_preflight_supports_current_and_explicit_legacy_modes() -> None:
    scenario_schema = dict(SCENARIO_PROPERTY_SCHEMA)
    stage1_schema = dict(STAGE1_PROPERTY_SCHEMA)
    stage2_schema = {**STAGE2_PROPERTY_SCHEMA, **CURRENT_OUTCOME_PROPERTY_SCHEMA}

    assert validate_notion_v3_schemas(
        scenario_schema=scenario_schema,
        stage1_schema=stage1_schema,
        stage2_schema=stage2_schema,
    ) == "CURRENT"

    legacy_stage2 = {**STAGE2_PROPERTY_SCHEMA, **LEGACY_OUTCOME_PROPERTY_SCHEMA}
    assert validate_notion_v3_schemas(
        scenario_schema=scenario_schema,
        stage1_schema=stage1_schema,
        stage2_schema=legacy_stage2,
    ) == "LEGACY"


def test_notion_v3_schema_preflight_rejects_partial_current_or_wrong_property_type() -> None:
    scenario_schema = dict(SCENARIO_PROPERTY_SCHEMA)
    stage1_schema = dict(STAGE1_PROPERTY_SCHEMA)
    partial_current = {
        **STAGE2_PROPERTY_SCHEMA,
        **LEGACY_OUTCOME_PROPERTY_SCHEMA,
        "반영 결과 필수 사실": "rich_text",
    }
    with pytest.raises(ValueError, match="missing 반영 결과 금지 사실"):
        validate_notion_v3_schemas(
            scenario_schema=scenario_schema,
            stage1_schema=stage1_schema,
            stage2_schema=partial_current,
        )

    with pytest.raises(ValueError, match="Stage2 legacy outcome schema mismatch"):
        validate_notion_v3_schemas(
            scenario_schema=scenario_schema,
            stage1_schema=stage1_schema,
            stage2_schema=dict(STAGE2_PROPERTY_SCHEMA),
        )

    wrong_type = {**STAGE2_PROPERTY_SCHEMA, **CURRENT_OUTCOME_PROPERTY_SCHEMA}
    wrong_type["targetRef"] = "title"
    with pytest.raises(ValueError, match="targetRef expected rich_text, got title"):
        validate_notion_v3_schemas(
            scenario_schema=scenario_schema,
            stage1_schema=stage1_schema,
            stage2_schema=wrong_type,
        )


def test_world_proposal_path_uses_production_internal_whitespace_identity() -> None:
    sources = [
        _world_stage1_gold("W1", "고블린  족", "평균 140cm다."),
        _world_stage1_gold("W2", "고블린 족", "큰 변종은 190cm다."),
    ]

    with pytest.raises(ValueError, match="WORLD sources use different paths"):
        _resolve_world_proposal(
            sources,
            operation="ADD",
            consolidation_status="MERGED",
            matched_scope_name=None,
            matched_property_name=None,
            annotated_scope_name=None,
            annotated_setting_name="체격",
            annotated_value="두 사실을 병합한다.",
            row_id="D1",
        )


def test_world_proposal_value_dedupe_uses_production_internal_whitespace_identity() -> None:
    sources = [
        _world_stage1_gold("W1", "고블린", "큰  변종"),
        _world_stage1_gold("W2", "고블린", "큰 변종"),
    ]

    _, _, proposed_value = _resolve_world_proposal(
        sources,
        operation="ADD",
        consolidation_status="MERGED",
        matched_scope_name=None,
        matched_property_name=None,
        annotated_scope_name=None,
        annotated_setting_name="체격",
        annotated_value="두 표현을 병합한다.",
        row_id="D1",
    )

    assert proposed_value == "두 표현을 병합한다."


def test_world_scope_mismatch_reports_the_notion_decision_row() -> None:
    source = _world_stage1_gold(
        "W1",
        "고블린",
        "희귀 변종은 190cm다.",
        scope_name="변종",
    )

    with pytest.raises(
        ValueError,
        match="Notion Stage2 row D-scope worldScope must equal matchedScopeName",
    ):
        _resolve_world_proposal(
            [source],
            operation="MERGE",
            consolidation_status="SINGLE",
            matched_scope_name="일반",
            matched_property_name="체격",
            annotated_scope_name="일반",
            annotated_setting_name="체격",
            annotated_value="평균은 140cm이며 변종은 190cm다.",
            row_id="D-scope",
        )


def _world_stage1_gold(
    gold_id: str,
    subject_name: str,
    value: str,
    *,
    scope_name: str | None = None,
) -> WorldStage1Gold:
    return WorldStage1Gold(
        gold_id=gold_id,
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=[value],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="RACE",
        subject_name=subject_name,
        scope_name=scope_name,
        setting_name="체격",
        source_values=[value],
    )


def _scenario_page(
    page_id: str,
    scenario_id: str,
    episode_no: int,
    *,
    status: str,
    previous_page_id: str | None = None,
    candidate_free: bool = False,
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "시나리오 ID": _title(scenario_id),
            "회차": _number(episode_no),
            "원문 식별자": _rich_text(f"{episode_no:02d}화.txt"),
            "원문 Hash": _rich_text("0" * 64),
            "대상 도메인": _multi_select("CHARACTER"),
            "정답지 버전": _rich_text("gold-v3"),
            "후보 없음 회차": _checkbox(candidate_free),
            "시작 상태 방식": _select("EMPTY" if previous_page_id is None else "PREVIOUS_GOLD"),
            "이전 시나리오": _relation(previous_page_id),
            "누적 기준 회차": _number(0 if previous_page_id is None else episode_no - 1),
            "1차 제공 컨텍스트": _rich_text("knownCharacters=[비요른]"),
            "상태 생성 상태": _select("PENDING"),
            "beforeState URI": _rich_text(""),
            "beforeState Hash": _rich_text(""),
            "afterState URI": _rich_text(""),
            "afterState Hash": _rich_text(""),
            "검수 상태": _select(status),
            "검수 메모": _rich_text(""),
        },
    }


def _character_stage1_page(
    page_id: str,
    gold_id: str,
    scenario_page_id: str,
    *,
    status: str,
) -> dict:
    return {
        "id": page_id,
        "url": f"https://notion.test/{page_id}",
        "properties": {
            "정답 ID": _title(gold_id),
            "시나리오": _relation(scenario_page_id),
            "회차": _number(1 if scenario_page_id.endswith("1") else 2),
            "정렬 순서": _number(1),
            "도메인": _select("CHARACTER"),
            "candidateKind": _select("SETTING"),
            "1차 판정": _select("EXTRACT"),
            "중요도": _select("MUST"),
            "맥락 태그": _multi_select("CURRENT"),
            "canonical entityRef": _rich_text("character:bjorn"),
            "canonical entityName": _rich_text("비요른"),
            "rawEntityMention": _rich_text("비요른은"),
            "factType": _select("PROFILE"),
            "canonical factKey": _rich_text("profile.species"),
            "허용 factKey 별칭": _rich_text("[]"),
            "valueType": _select("STRING"),
            "정답 표시값": _rich_text("바바리안"),
            "정답 valueJson": _rich_text(""),
            "원문 근거": _rich_text("비요른은 바바리안이다."),
            "위치 힌트": _rich_text(""),
            "동일 사실 그룹": _rich_text(""),
            "현재 스키마 표현 가능": _checkbox(True),
            "원본 페이지": {"type": "url", "url": "https://source.test/episode"},
            "검수 상태": _select(status),
            "검수 메모": _rich_text(""),
        },
    }


def _character_stage2_page(
    page_id: str,
    decision_id: str,
    scenario_page_id: str,
    source_page_id: str,
    *,
    status: str,
    required: str = "비요른 | 종족 | 바바리안",
    forbidden: str = "",
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "판단 ID": _title(decision_id),
            "시나리오": _relation(scenario_page_id),
            "1차 정답": _relation(source_page_id),
            "회차": _number(1 if scenario_page_id.endswith("1") else 2),
            "정렬 순서": _number(1),
            "도메인": _select("CHARACTER"),
            "operation": _select("ADD"),
            "targetRef": _rich_text(""),
            "removedSnapshotRefs": _rich_text("[]"),
            "beforeValue": _rich_text(""),
            "beforeValueJson": _rich_text(""),
            "proposedValue": _rich_text("바바리안"),
            "proposedValueJson": _rich_text('{"value":"바바리안"}'),
            "temporalScope": _select("PRESENT"),
            "반영 결과 필수 사실": _rich_text(required),
            "반영 결과 금지 사실": _rich_text(forbidden),
            "유지 Claim": _rich_text(""),
            "추가 Claim": _rich_text(""),
            "제거 Claim": _rich_text(""),
            "금지 Claim": _rich_text(""),
            "비교 이유": _rich_text("새 종족 정보"),
            "검수 상태": _select(status),
            "검수 메모": _rich_text(""),
        },
    }


def _title(value: str) -> dict:
    return {"type": "title", "title": [{"plain_text": value}] if value else []}


def _rich_text(value: str) -> dict:
    return {"type": "rich_text", "rich_text": [{"plain_text": value}] if value else []}


def _number(value: int) -> dict:
    return {"type": "number", "number": value}


def _select(value: str) -> dict:
    return {"type": "select", "select": {"name": value}}


def _multi_select(*values: str) -> dict:
    return {"type": "multi_select", "multi_select": [{"name": value} for value in values]}


def _relation(page_id: str | None) -> dict:
    return {
        "type": "relation",
        "relation": [] if page_id is None else [{"id": page_id}],
    }


def _checkbox(value: bool) -> dict:
    return {"type": "checkbox", "checkbox": value}
