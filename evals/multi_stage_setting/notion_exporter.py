from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
import json
import re
import unicodedata
from typing import Any

from pydantic import ValidationError

from app.domain.enums import SettingValueType
from app.mappers.world_setting_candidate_mapper import normalize_world_setting_name
from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage2Gold,
    EvaluationDomain,
    GoldSnapshotV3,
    ReviewStatus,
    ScenarioGold,
    StartStateMode,
    StateGenerationStatus,
    ValueJsonProvenance,
    WorldStage1Gold,
    WorldStage2Gold,
    world_path_key,
)
from evals.setting_extraction.normalization import normalize_text
from evals.setting_extraction.notion_exporter import (
    _read_number,
    _read_select,
    _read_text,
)


KNOWN_CHARACTERS_PATTERN = re.compile(
    r"^\s*knownCharacters\s*=\s*\[(?P<names>.*)]\s*$",
    re.DOTALL | re.IGNORECASE,
)

SCENARIO_PROPERTY_SCHEMA = {
    "시나리오 ID": "title",
    "회차": "number",
    "원문 식별자": "rich_text",
    "원문 Hash": "rich_text",
    "대상 도메인": "multi_select",
    "정답지 버전": "rich_text",
    "후보 없음 회차": "checkbox",
    "시작 상태 방식": "select",
    "이전 시나리오": "relation",
    "누적 기준 회차": "number",
    "1차 제공 컨텍스트": "rich_text",
    "상태 생성 상태": "select",
    "beforeState URI": "rich_text",
    "beforeState Hash": "rich_text",
    "afterState URI": "rich_text",
    "afterState Hash": "rich_text",
    "검수 상태": "select",
    "검수 메모": "rich_text",
}
STAGE1_PROPERTY_SCHEMA = {
    "정답 ID": "title",
    "시나리오": "relation",
    "회차": "number",
    "정렬 순서": "number",
    "도메인": "select",
    "candidateKind": "select",
    "1차 판정": "select",
    "중요도": "select",
    "맥락 태그": "multi_select",
    "canonical entityRef": "rich_text",
    "canonical entityName": "rich_text",
    "rawEntityMention": "rich_text",
    "factType": "select",
    "canonical factKey": "rich_text",
    "허용 factKey 별칭": "rich_text",
    "valueType": "select",
    "worldCategory": "select",
    "worldSubject": "rich_text",
    "worldScope": "rich_text",
    "worldSettingName": "rich_text",
    "정답 표시값": "rich_text",
    "정답 valueJson": "rich_text",
    "원문 근거": "rich_text",
    "위치 힌트": "rich_text",
    "동일 사실 그룹": "rich_text",
    "현재 스키마 표현 가능": "checkbox",
    "원본 페이지": "url",
    "검수 상태": "select",
    "검수 메모": "rich_text",
}
STAGE2_PROPERTY_SCHEMA = {
    "판단 ID": "title",
    "시나리오": "relation",
    "1차 정답": "relation",
    "회차": "number",
    "정렬 순서": "number",
    "도메인": "select",
    "operation": "select",
    "temporalScope": "select",
    "consolidationStatus": "select",
    "targetRef": "rich_text",
    "removedSnapshotRefs": "rich_text",
    "beforeValue": "rich_text",
    "beforeValueJson": "rich_text",
    "proposedValue": "rich_text",
    "proposedValueJson": "rich_text",
    "matchedScopeName": "rich_text",
    "matchedPropertyName": "rich_text",
    "proposedScopeName": "rich_text",
    "proposedSettingName": "rich_text",
    "비교 이유": "rich_text",
    "검수 상태": "select",
    "검수 메모": "rich_text",
}
CURRENT_OUTCOME_PROPERTY_SCHEMA = {
    "반영 결과 필수 사실": "rich_text",
    "반영 결과 금지 사실": "rich_text",
}
LEGACY_OUTCOME_PROPERTY_SCHEMA = {
    "유지 Claim": "rich_text",
    "추가 Claim": "rich_text",
    "제거 Claim": "rich_text",
    "금지 Claim": "rich_text",
}


def validate_notion_v3_schemas(
    *,
    scenario_schema: dict[str, str],
    stage1_schema: dict[str, str],
    stage2_schema: dict[str, str],
) -> str:
    """세 원본 DB의 컬럼 계약을 검증하고 Stage2 결과 컬럼 모드를 반환한다."""

    _validate_property_schema("Scenario", scenario_schema, SCENARIO_PROPERTY_SCHEMA)
    _validate_property_schema("Stage1", stage1_schema, STAGE1_PROPERTY_SCHEMA)
    _validate_property_schema("Stage2", stage2_schema, STAGE2_PROPERTY_SCHEMA)

    current_names = set(CURRENT_OUTCOME_PROPERTY_SCHEMA)
    present_current = current_names & stage2_schema.keys()
    if present_current:
        # 한 컬럼만 삭제된 경우 legacy 값으로 조용히 후퇴시키지 않는다.
        _validate_property_schema(
            "Stage2 current outcome",
            stage2_schema,
            CURRENT_OUTCOME_PROPERTY_SCHEMA,
        )
        return "CURRENT"

    _validate_property_schema(
        "Stage2 legacy outcome",
        stage2_schema,
        LEGACY_OUTCOME_PROPERTY_SCHEMA,
    )
    return "LEGACY"


def _validate_property_schema(
    label: str,
    actual: dict[str, str],
    expected: dict[str, str],
) -> None:
    problems = []
    for name, expected_type in expected.items():
        actual_type = actual.get(name)
        if actual_type is None:
            problems.append(f"missing {name}")
        elif actual_type != expected_type:
            problems.append(f"{name} expected {expected_type}, got {actual_type}")
    if problems:
        raise ValueError(f"Notion {label} schema mismatch: " + "; ".join(problems))


def build_gold_snapshot_v3(
    scenario_pages: Iterable[dict[str, Any]],
    stage1_pages: Iterable[dict[str, Any]],
    stage2_pages: Iterable[dict[str, Any]],
    *,
    dataset_name: str,
    dataset_version: str | None = None,
    episode_numbers: set[int] | None = None,
    review_statuses: set[ReviewStatus] | None = None,
) -> GoldSnapshotV3:
    """세 Notion 원본 DB를 안정적인 v3 JSON 계약으로 변환한다.

    컬럼명과 Relation 해석은 이 adapter에만 둔다. 평가기·reducer는 아래에서 생성한
    snapshot만 소비하므로 Notion view나 사람용 컬럼이 바뀌어도 core 계약은 흔들리지 않는다.
    """

    allowed_statuses = review_statuses or {ReviewStatus.FINAL}
    strict_final = allowed_statuses == {ReviewStatus.FINAL}
    all_scenarios_by_page: dict[str, ScenarioGold] = {}
    previous_relation_by_scenario: dict[str, str | None] = {}
    for page in scenario_pages:
        properties = _properties(page, "scenario")
        status = _required_review_status(properties, _page_label(page, "시나리오 ID"))
        if status not in allowed_statuses:
            continue
        scenario, previous_page_id = _parse_scenario(page, properties)
        page_id = _page_id(page)
        if page_id in all_scenarios_by_page:
            raise ValueError(f"Duplicate Notion scenario page ID: {page_id}.")
        all_scenarios_by_page[page_id] = scenario
        previous_relation_by_scenario[scenario.scenario_id] = previous_page_id

    if not all_scenarios_by_page:
        statuses = ", ".join(sorted(status.value for status in allowed_statuses))
        raise ValueError(f"No Scenario rows matched review statuses: {statuses}.")

    selected_page_ids = {
        page_id
        for page_id, scenario in all_scenarios_by_page.items()
        if episode_numbers is None or scenario.episode_no in episode_numbers
    }
    if episode_numbers is not None:
        found_episode_numbers = {
            all_scenarios_by_page[page_id].episode_no for page_id in selected_page_ids
        }
        missing = sorted(episode_numbers - found_episode_numbers)
        if missing:
            raise ValueError(f"No Scenario rows found for episodes: {missing}.")
    evaluation_page_ids = set(selected_page_ids)
    pending = list(selected_page_ids)
    while pending:
        page_id = pending.pop()
        scenario = all_scenarios_by_page[page_id]
        previous_page_id = previous_relation_by_scenario[scenario.scenario_id]
        if previous_page_id is None or previous_page_id in selected_page_ids:
            continue
        if previous_page_id not in all_scenarios_by_page:
            raise ValueError(
                f"Scenario {scenario.scenario_id} references a previous scenario outside "
                "the selected review statuses."
            )
        selected_page_ids.add(previous_page_id)
        pending.append(previous_page_id)

    scenarios_by_page = {
        page_id: scenario
        for page_id, scenario in all_scenarios_by_page.items()
        if page_id in selected_page_ids
    }
    scenario_id_by_page = {
        page_id: scenario.scenario_id for page_id, scenario in scenarios_by_page.items()
    }
    scenario_by_id = {scenario.scenario_id: scenario for scenario in scenarios_by_page.values()}
    scenarios = []
    for scenario in scenarios_by_page.values():
        previous_page_id = previous_relation_by_scenario[scenario.scenario_id]
        previous_scenario_id = (
            None if previous_page_id is None else scenario_id_by_page.get(previous_page_id)
        )
        if previous_page_id is not None and previous_scenario_id is None:
            raise ValueError(
                f"Scenario {scenario.scenario_id} references a previous scenario outside "
                "the exported snapshot. Include the complete state chain."
            )
        scenarios.append(
            scenario.model_copy(update={"previous_scenario_id": previous_scenario_id})
        )
    scenarios.sort(key=lambda item: (item.episode_no, item.scenario_id))
    if strict_final:
        missing_hashes = [
            scenario.scenario_id for scenario in scenarios if scenario.source_hash is None
        ]
        if missing_hashes:
            raise ValueError(
                "FINAL Scenario rows require 원문 Hash: " + ", ".join(missing_hashes)
            )
        unverified_external_seeds = [
            scenario.scenario_id
            for scenario in scenarios
            if scenario.start_state_mode == StartStateMode.SEED
            and scenario.seed_state is None
            and scenario.before_state_uri is not None
            and (
                scenario.before_state_hash is None
                or scenario.state_generation_status != StateGenerationStatus.VERIFIED
            )
        ]
        if unverified_external_seeds:
            raise ValueError(
                "FINAL external SEED Scenario rows require beforeState Hash and "
                "상태 생성 상태=VERIFIED: "
                + ", ".join(unverified_external_seeds)
            )

    stage1_rows = []
    stage1_id_by_page: dict[str, str] = {}
    for page in stage1_pages:
        properties = _properties(page, "Stage1")
        relation_ids = _relation_ids(properties, "시나리오")
        if not set(relation_ids) & selected_page_ids:
            continue
        status = _required_review_status(properties, _page_label(page, "정답 ID"))
        if status not in allowed_statuses:
            if strict_final:
                raise ValueError(
                    f"FINAL export includes non-FINAL Stage1 row "
                    f"{_page_label(page, '정답 ID')}."
                )
            continue
        scenario_id = _resolve_single_relation(
            properties,
            "시나리오",
            scenario_id_by_page,
            _page_label(page, "정답 ID"),
        )
        row = _parse_stage1(
            page,
            properties,
            scenario_id,
            scenario_by_id[scenario_id].episode_no,
        )
        stage1_rows.append(row)
        stage1_id_by_page[_page_id(page)] = row.gold_id
    stage1_rows.sort(key=lambda item: (item.episode_no, item.sort_order, item.gold_id))

    stage2_rows = []
    for page in stage2_pages:
        properties = _properties(page, "Stage2")
        relation_ids = _relation_ids(properties, "시나리오")
        if not set(relation_ids) & selected_page_ids:
            continue
        status = _required_review_status(properties, _page_label(page, "판단 ID"))
        if status not in allowed_statuses:
            if strict_final:
                raise ValueError(
                    f"FINAL export includes non-FINAL Stage2 row "
                    f"{_page_label(page, '판단 ID')}."
                )
            continue
        row_id = _page_label(page, "판단 ID")
        scenario_id = _resolve_single_relation(
            properties,
            "시나리오",
            scenario_id_by_page,
            row_id,
        )
        source_gold_ids = _resolve_relations(
            properties,
            "1차 정답",
            stage1_id_by_page,
            row_id,
        )
        source_rows = [
            next(item for item in stage1_rows if item.gold_id == source_gold_id)
            for source_gold_id in source_gold_ids
        ]
        stage2_rows.append(
            _parse_stage2(
                page,
                properties,
                scenario_id,
                source_gold_ids,
                source_rows,
                scenario_by_id[scenario_id],
            )
        )
    stage2_rows.sort(key=lambda item: (item.episode_no, item.sort_order, item.decision_id))

    selected_versions = {scenario.gold_version for scenario in scenarios}
    if dataset_version is None:
        if len(selected_versions) != 1:
            raise ValueError(
                "Selected Scenario rows must share one 정답지 버전 or --dataset-version "
                "must be provided."
            )
        resolved_version = selected_versions.pop()
    else:
        if selected_versions != {dataset_version}:
            raise ValueError(
                "--dataset-version must match every selected Scenario 정답지 버전."
            )
        resolved_version = dataset_version

    try:
        snapshot = GoldSnapshotV3(
            dataset_version=resolved_version,
            name=dataset_name,
            scorable=all(
                item.review_status == ReviewStatus.FINAL
                for item in [*scenarios, *stage1_rows, *stage2_rows]
            ),
            evaluation_scenario_ids=[
                scenarios_by_page[page_id].scenario_id
                for page_id in sorted(
                    evaluation_page_ids,
                    key=lambda item: scenarios_by_page[item].episode_no,
                )
            ],
            scenarios=scenarios,
            stage1=stage1_rows,
            stage2=stage2_rows,
        )
    except ValidationError as exc:
        safe_fields = sorted(
            {
                ".".join(str(part) for part in detail.get("loc", ())) or "snapshot"
                for detail in exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            }
        )
        raise ValueError(
            "Invalid v3 Notion snapshot; check fields: " + ", ".join(safe_fields)
        ) from None
    return snapshot.with_fixture_hash()


def _parse_scenario(
    page: dict[str, Any],
    properties: dict[str, Any],
) -> tuple[ScenarioGold, str | None]:
    row_id = _required_text(properties, "시나리오 ID", _page_id(page))
    previous_ids = _relation_ids(properties, "이전 시나리오")
    if len(previous_ids) > 1:
        raise ValueError(f"Scenario {row_id} may reference only one 이전 시나리오.")
    context = _read_text(properties, "1차 제공 컨텍스트")
    return (
        ScenarioGold(
            scenario_id=row_id,
            episode_no=int(_read_number(properties, "회차")),
            episode_title=_read_text(properties, "회차 제목") or None,
            source_identifier=_required_text(properties, "원문 식별자", row_id),
            source_hash=_read_text(properties, "원문 Hash") or None,
            target_domains=set(_read_multi_select(properties, "대상 도메인")),
            gold_version=_required_text(properties, "정답지 버전", row_id),
            evaluation_batch_id=_read_text(properties, "평가 Batch") or None,
            candidate_free=_read_checkbox(properties, "후보 없음 회차"),
            start_state_mode=_required_select(properties, "시작 상태 방식", row_id),
            # Relation은 모든 행을 읽은 뒤 stable scenario ID로 환원한다.
            previous_scenario_id=previous_ids[0] if previous_ids else None,
            cumulative_through_episode=int(_read_number(properties, "누적 기준 회차")),
            provided_context=context,
            known_character_names=_parse_known_character_names(context),
            state_generation_status=(
                _read_select(properties, "상태 생성 상태") or "PENDING"
            ),
            before_state_uri=_read_text(properties, "beforeState URI") or None,
            before_state_hash=_read_text(properties, "beforeState Hash") or None,
            after_state_uri=_read_text(properties, "afterState URI") or None,
            after_state_hash=_read_text(properties, "afterState Hash") or None,
            review_status=_required_select(properties, "검수 상태", row_id),
            review_note=_read_text(properties, "검수 메모") or None,
        ),
        previous_ids[0] if previous_ids else None,
    )


def _parse_stage1(
    page: dict[str, Any],
    properties: dict[str, Any],
    scenario_id: str,
    scenario_episode_no: int | None = None,
) -> CharacterStage1Gold | WorldStage1Gold:
    row_id = _required_text(properties, "정답 ID", _page_id(page))
    domain = _required_select(properties, "도메인", row_id)
    candidate_kind = _read_select(properties, "candidateKind")
    if not candidate_kind:
        if domain == EvaluationDomain.WORLD:
            candidate_kind = "WORLD_SETTING"
        else:
            raise ValueError(f"Notion row {row_id} requires candidateKind.")
    common = {
        "gold_id": row_id,
        "scenario_id": scenario_id,
        "episode_no": _resolved_episode_no(
            properties,
            row_id,
            expected=scenario_episode_no,
        ),
        "sort_order": int(_read_number(properties, "정렬 순서")),
        "decision": _required_select(properties, "1차 판정", row_id),
        "importance": _read_select(properties, "중요도") or None,
        "candidate_kind": candidate_kind,
        "context_tags": _read_multi_select(properties, "맥락 태그"),
        "evidence_quotes": _split_lines(_read_text(properties, "원문 근거")),
        "location_hint": _read_text(properties, "위치 힌트") or None,
        "same_fact_group": _read_text(properties, "동일 사실 그룹") or None,
        "source_page_url": _read_url(properties, "원본 페이지"),
        "current_schema_representable": _read_checkbox(
            properties, "현재 스키마 표현 가능"
        ),
        "review_status": _required_select(properties, "검수 상태", row_id),
        "review_note": _read_text(properties, "검수 메모") or None,
    }
    display_value = _read_text(properties, "정답 표시값") or None
    if domain == EvaluationDomain.CHARACTER:
        value_type = _read_select(properties, "valueType") or None
        explicit_json = _parse_json_object(
            _read_text(properties, "정답 valueJson"),
            row_id,
            "정답 valueJson",
        )
        value_json, provenance, scorable = _resolve_character_value_json(
            value_type,
            display_value,
            explicit_json,
            row_id,
        )
        return CharacterStage1Gold(
            **common,
            domain=EvaluationDomain.CHARACTER,
            entity_ref=_required_text(properties, "canonical entityRef", row_id),
            entity_name=_required_text(properties, "canonical entityName", row_id),
            raw_entity_mention=_read_text(properties, "rawEntityMention") or None,
            fact_type=_read_select(properties, "factType") or None,
            fact_key=_read_text(properties, "canonical factKey") or None,
            accepted_fact_key_aliases=_parse_json_or_line_string_array(
                _read_text(properties, "허용 factKey 별칭"),
                row_id,
                "허용 factKey 별칭",
            ),
            value_type=value_type,
            display_value=display_value,
            value_json=value_json,
            value_json_provenance=provenance,
            structured_scorable=scorable,
        )
    if domain == EvaluationDomain.WORLD:
        return WorldStage1Gold(
            **common,
            domain=EvaluationDomain.WORLD,
            category=_required_select(properties, "worldCategory", row_id),
            subject_name=_required_text(properties, "worldSubject", row_id),
            scope_name=_read_text(properties, "worldScope") or None,
            setting_name=_required_text(properties, "worldSettingName", row_id),
            # 같은 원본 DB를 쓰므로 CHARACTER 행에서는 factKey 별칭,
            # WORLD 행에서는 worldSettingName 별칭으로 해석한다.
            accepted_setting_name_aliases=_parse_json_or_line_string_array(
                _read_text(properties, "허용 factKey 별칭"),
                row_id,
                "허용 factKey 별칭",
            ),
            source_values=_split_lines(display_value or ""),
        )
    raise ValueError(f"Notion Stage1 row {row_id} has unknown 도메인.")


def _parse_stage2(
    page: dict[str, Any],
    properties: dict[str, Any],
    scenario_id: str,
    source_gold_ids: list[str],
    source_rows: list[CharacterStage1Gold | WorldStage1Gold],
    scenario: ScenarioGold,
) -> CharacterStage2Gold | WorldStage2Gold:
    row_id = _required_text(properties, "판단 ID", _page_id(page))
    required_facts = _resolve_outcome_facts(
        properties,
        row_id,
        new_column="반영 결과 필수 사실",
        legacy_columns=("유지 Claim", "추가 Claim"),
    )
    forbidden_facts = _resolve_outcome_facts(
        properties,
        row_id,
        new_column="반영 결과 금지 사실",
        legacy_columns=("제거 Claim", "금지 Claim"),
    )
    proposed_value = _read_text(properties, "proposedValue") or None
    annotated_proposed_json = _parse_json_object(
        _read_text(properties, "proposedValueJson"), row_id, "proposedValueJson"
    )
    common = {
        "decision_id": row_id,
        "scenario_id": scenario_id,
        "episode_no": _resolved_episode_no(
            properties,
            row_id,
            expected=scenario.episode_no,
        ),
        "sort_order": int(_read_number(properties, "정렬 순서")),
        "source_gold_ids": source_gold_ids,
        "target_ref": _read_text(properties, "targetRef") or None,
        "before_value": _read_text(properties, "beforeValue") or None,
        "before_value_json": _parse_json_object(
            _read_text(properties, "beforeValueJson"), row_id, "beforeValueJson"
        ),
        "proposed_value": proposed_value,
        "required_facts": required_facts,
        "forbidden_facts": forbidden_facts,
        "comparison_reason": _read_text(properties, "비교 이유") or None,
        "review_status": _required_select(properties, "검수 상태", row_id),
        "review_note": _read_text(properties, "검수 메모") or None,
    }
    domain = _required_select(properties, "도메인", row_id)
    operation = _required_select(properties, "operation", row_id)
    if domain == EvaluationDomain.CHARACTER:
        return CharacterStage2Gold(
            **common,
            proposed_value_json=_resolve_stage2_character_value_json(
                source_rows,
                operation,
                proposed_value,
                annotated_proposed_json,
                row_id,
            ),
            domain=EvaluationDomain.CHARACTER,
            operation=operation,
            temporal_scope=_required_select(properties, "temporalScope", row_id),
            removed_snapshot_refs=_parse_json_string_array(
                _read_text(properties, "removedSnapshotRefs"),
                row_id,
                "removedSnapshotRefs",
            ),
        )
    if domain == EvaluationDomain.WORLD:
        if annotated_proposed_json is not None:
            raise ValueError(
                f"Notion Stage2 row {row_id} must not use proposedValueJson for WORLD."
            )
        consolidation_status = _required_select(
            properties, "consolidationStatus", row_id
        )
        matched_scope_name = _read_text(properties, "matchedScopeName") or None
        matched_property_name = _read_text(properties, "matchedPropertyName") or None
        proposed_scope_name, proposed_setting_name, proposed_value = (
            _resolve_world_proposal(
                source_rows,
                operation=operation,
                consolidation_status=consolidation_status,
                matched_scope_name=matched_scope_name,
                matched_property_name=matched_property_name,
                annotated_scope_name=_read_text(properties, "proposedScopeName") or None,
                annotated_setting_name=_read_text(properties, "proposedSettingName") or None,
                annotated_value=proposed_value,
                row_id=row_id,
            )
        )
        return WorldStage2Gold(
            **{**common, "proposed_value": proposed_value},
            domain=EvaluationDomain.WORLD,
            operation=operation,
            consolidation_status=consolidation_status,
            matched_scope_name=matched_scope_name,
            matched_property_name=matched_property_name,
            proposed_scope_name=proposed_scope_name,
            proposed_setting_name=proposed_setting_name,
        )
    raise ValueError(f"Notion Stage2 row {row_id} has unknown 도메인.")


def _resolve_world_proposal(
    source_rows: list[CharacterStage1Gold | WorldStage1Gold],
    *,
    operation: str,
    consolidation_status: str,
    matched_scope_name: str | None,
    matched_property_name: str | None,
    annotated_scope_name: str | None,
    annotated_setting_name: str | None,
    annotated_value: str | None,
    row_id: str,
) -> tuple[str | None, str, str]:
    if not source_rows or any(
        not isinstance(source, WorldStage1Gold) for source in source_rows
    ):
        raise ValueError(f"Notion Stage2 row {row_id} has invalid WORLD source rows.")
    world_sources = [source for source in source_rows if isinstance(source, WorldStage1Gold)]
    primary = world_sources[0]
    path = world_path_key(
        primary.category,
        primary.subject_name,
        primary.scope_name,
        primary.setting_name,
    )
    if any(
        world_path_key(
            source.category,
            source.subject_name,
            source.scope_name,
            source.setting_name,
        )
        != path
        for source in world_sources[1:]
    ):
        raise ValueError(f"Notion Stage2 row {row_id} WORLD sources use different paths.")

    compares_existing_property = operation in {"UPDATE", "MERGE"} or (
        operation == "EXCLUDE" and matched_property_name is not None
    )
    if compares_existing_property and primary.scope_name != matched_scope_name:
        raise ValueError(
            f"Notion Stage2 row {row_id} worldScope must equal matchedScopeName "
            f"for {operation}."
        )

    if operation in {"ADD", "EXCLUDE"}:
        expected_scope = primary.scope_name
        expected_setting = primary.setting_name
    else:
        expected_scope = matched_scope_name
        expected_setting = matched_property_name
    if expected_setting is None:
        raise ValueError(f"Notion Stage2 row {row_id} cannot derive proposedSettingName.")
    if (
        annotated_scope_name is not None
        and normalize_world_setting_name(annotated_scope_name)
        != normalize_world_setting_name(expected_scope or "")
    ):
        raise ValueError(f"Notion Stage2 row {row_id} proposedScopeName changes its path.")
    if (
        annotated_setting_name is not None
        and normalize_world_setting_name(annotated_setting_name)
        != normalize_world_setting_name(expected_setting)
    ):
        raise ValueError(f"Notion Stage2 row {row_id} proposedSettingName changes its path.")

    values: list[str] = []
    seen: set[str] = set()
    for source in world_sources:
        for value in source.source_values:
            normalized = normalize_world_setting_name(value)
            if normalized in seen:
                continue
            seen.add(normalized)
            values.append(value)
    if consolidation_status == "SINGLE" and len(values) != 1:
        raise ValueError(f"Notion Stage2 row {row_id} SINGLE requires one source value.")
    if consolidation_status in {"MERGED", "CONFLICT"} and len(values) < 2:
        raise ValueError(
            f"Notion Stage2 row {row_id} {consolidation_status} requires multiple values."
        )

    deterministic_value = None
    if consolidation_status == "CONFLICT":
        deterministic_value = "\n".join(values)
    elif operation in {"ADD", "EXCLUDE"} and len(values) == 1:
        deterministic_value = values[0]
    if deterministic_value is not None:
        if (
            annotated_value is not None
            and normalize_world_setting_name(annotated_value)
            != normalize_world_setting_name(deterministic_value)
        ):
            raise ValueError(
                f"Notion Stage2 row {row_id} proposedValue differs from deterministic source."
            )
        proposed_value = deterministic_value
    elif annotated_value is None:
        raise ValueError(f"Notion Stage2 row {row_id} requires proposedValue.")
    else:
        proposed_value = annotated_value
    return expected_scope, expected_setting, proposed_value


def _resolve_stage2_character_value_json(
    source_rows: list[CharacterStage1Gold | WorldStage1Gold],
    operation: str,
    proposed_value: str | None,
    annotated_json: dict[str, Any] | None,
    row_id: str,
) -> dict[str, Any] | None:
    if operation not in {"ADD", "UPDATE", "MERGE"}:
        if annotated_json is not None:
            raise ValueError(
                f"Notion Stage2 row {row_id} has proposedValueJson for non-mutating operation."
            )
        return None
    if len(source_rows) != 1 or not isinstance(source_rows[0], CharacterStage1Gold):
        raise ValueError(f"Notion Stage2 row {row_id} has invalid CHARACTER source rows.")
    source = source_rows[0]
    generated, _, _ = _resolve_character_value_json(
        source.value_type,
        proposed_value,
        None,
        row_id,
    )
    if generated is not None:
        if annotated_json is not None and not _scalar_json_matches(
            generated, annotated_json
        ):
            raise ValueError(
                f"Notion Stage2 row {row_id} proposedValueJson differs from its scalar value."
            )
        return generated
    # JSON/UNKNOWN의 최종 구조는 exporter가 만들 수 없으므로 명시 annotation만 허용한다.
    if annotated_json is None:
        raise ValueError(
            f"Notion Stage2 row {row_id} requires proposedValueJson for JSON/UNKNOWN."
        )
    return annotated_json


def _resolve_character_value_json(
    value_type: str | None,
    display_value: str | None,
    explicit_json: dict[str, Any] | None,
    row_id: str,
) -> tuple[dict[str, Any] | None, ValueJsonProvenance, bool]:
    if value_type is None or display_value is None:
        if explicit_json is not None:
            return explicit_json, ValueJsonProvenance.ANNOTATED, True
        return None, ValueJsonProvenance.UNAVAILABLE, False
    if value_type == SettingValueType.STRING:
        generated = {"value": display_value}
        return _resolve_scalar_json(generated, explicit_json, row_id)
    normalized = unicodedata.normalize("NFKC", display_value).replace(",", "").strip()
    if value_type == SettingValueType.NUMBER:
        try:
            number = Decimal(normalized)
        except InvalidOperation:
            raise ValueError(
                f"Notion Stage1 row {row_id} has non-canonical NUMBER display value."
            ) from None
        if not number.is_finite():
            raise ValueError(
                f"Notion Stage1 row {row_id} requires a finite NUMBER display value."
            )
        if number == number.to_integral():
            json_number: int | float = int(number)
        else:
            json_number = float(number)
            if Decimal(str(json_number)) != number:
                raise ValueError(
                    f"Notion Stage1 row {row_id} NUMBER loses JSON precision."
                )
        return _resolve_scalar_json({"value": json_number}, explicit_json, row_id)
    if value_type == SettingValueType.BOOLEAN:
        mapping = {
            "true": True,
            "false": False,
            "참": True,
            "거짓": False,
            "예": True,
            "아니오": False,
        }
        boolean = mapping.get(normalize_text(normalized))
        if boolean is None:
            raise ValueError(
                f"Notion Stage1 row {row_id} has non-canonical BOOLEAN display value."
            )
        return _resolve_scalar_json({"value": boolean}, explicit_json, row_id)
    # JSON/UNKNOWN의 구조를 exporter가 추측하면 같은 표가 실행 시점마다 다른 의미가 된다.
    if explicit_json is not None:
        return explicit_json, ValueJsonProvenance.ANNOTATED, True
    return None, ValueJsonProvenance.UNAVAILABLE, False


def _resolve_scalar_json(
    generated: dict[str, Any],
    explicit_json: dict[str, Any] | None,
    row_id: str,
) -> tuple[dict[str, Any], ValueJsonProvenance, bool]:
    if explicit_json is not None and not _scalar_json_matches(generated, explicit_json):
        raise ValueError(
            f"Notion Stage1 row {row_id} valueJson differs from its scalar display value."
        )
    if explicit_json is not None:
        return explicit_json, ValueJsonProvenance.ANNOTATED, True
    return generated, ValueJsonProvenance.GENERATED_SCALAR, True


def _scalar_json_matches(
    generated: dict[str, Any],
    explicit: dict[str, Any],
) -> bool:
    if set(explicit) != {"value"}:
        return False
    expected = generated["value"]
    actual = explicit["value"]
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, str):
        return isinstance(actual, str) and actual == expected
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    return Decimal(str(actual)) == Decimal(str(expected))


def _resolve_outcome_facts(
    properties: dict[str, Any],
    row_id: str,
    *,
    new_column: str,
    legacy_columns: tuple[str, str],
) -> list[str]:
    current = _split_lines(_read_text(properties, new_column))
    legacy = _dedupe_lines(
        [
            *_split_lines(_read_text(properties, legacy_columns[0])),
            *_split_lines(_read_text(properties, legacy_columns[1])),
        ]
    )
    if current and legacy:
        if {normalize_text(item) for item in current} != {
            normalize_text(item) for item in legacy
        }:
            raise ValueError(
                f"Notion Stage2 row {row_id} has conflicting {new_column} and legacy Claims."
            )
        return current
    return current or legacy


def _properties(page: dict[str, Any], label: str) -> dict[str, Any]:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        raise ValueError(f"Notion {label} page has no properties object.")
    return properties


def _page_id(page: dict[str, Any]) -> str:
    value = str(page.get("id") or "").strip()
    if not value:
        raise ValueError("Notion page has no ID.")
    return value.replace("-", "").casefold()


def _page_label(page: dict[str, Any], title_column: str) -> str:
    properties = _properties(page, title_column)
    return _read_text(properties, title_column) or str(page.get("id") or "unknown")


def _required_text(properties: dict[str, Any], name: str, row_id: str) -> str:
    value = _read_text(properties, name)
    if not value:
        raise ValueError(f"Notion row {row_id} requires {name}.")
    return value


def _required_select(properties: dict[str, Any], name: str, row_id: str) -> str:
    value = _read_select(properties, name)
    if not value:
        raise ValueError(f"Notion row {row_id} requires {name}.")
    return value


def _required_review_status(
    properties: dict[str, Any], row_id: str
) -> ReviewStatus:
    value = _required_select(properties, "검수 상태", row_id)
    try:
        return ReviewStatus(value)
    except ValueError:
        raise ValueError(f"Notion row {row_id} has invalid 검수 상태.") from None


def _read_multi_select(properties: dict[str, Any], name: str) -> list[str]:
    items = properties.get(name, {}).get("multi_select")
    if not isinstance(items, list):
        return []
    return [
        str(item.get("name") or "").strip()
        for item in items
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]


def _read_checkbox(properties: dict[str, Any], name: str) -> bool:
    value = properties.get(name, {}).get("checkbox")
    return value is True


def _read_url(properties: dict[str, Any], name: str) -> str | None:
    value = properties.get(name, {}).get("url")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _relation_ids(properties: dict[str, Any], name: str) -> list[str]:
    relations = properties.get(name, {}).get("relation")
    if not isinstance(relations, list):
        return []
    return [
        str(item.get("id") or "").replace("-", "").casefold()
        for item in relations
        if isinstance(item, dict) and item.get("id")
    ]


def _resolve_single_relation(
    properties: dict[str, Any],
    name: str,
    value_by_page_id: dict[str, str],
    row_id: str,
) -> str:
    values = _resolve_relations(properties, name, value_by_page_id, row_id)
    if len(values) != 1:
        raise ValueError(f"Notion row {row_id} requires exactly one {name} relation.")
    return values[0]


def _resolve_relations(
    properties: dict[str, Any],
    name: str,
    value_by_page_id: dict[str, str],
    row_id: str,
) -> list[str]:
    relation_ids = _relation_ids(properties, name)
    if not relation_ids:
        raise ValueError(f"Notion row {row_id} requires {name} relation.")
    unresolved = [page_id for page_id in relation_ids if page_id not in value_by_page_id]
    if unresolved:
        raise ValueError(
            f"Notion row {row_id} has {name} relation outside the exported snapshot."
        )
    return [value_by_page_id[page_id] for page_id in relation_ids]


def _parse_json_object(value: str, row_id: str, field: str) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"Notion row {row_id} has invalid {field} JSON.") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"Notion row {row_id} requires {field} to be a JSON object.")
    return parsed


def _parse_json_string_array(value: str, row_id: str, field: str) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"Notion row {row_id} has invalid {field} JSON.") from None
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item.strip() for item in parsed
    ):
        raise ValueError(f"Notion row {row_id} requires {field} to be a string array.")
    return [item.strip() for item in parsed]


def _parse_json_or_line_string_array(
    value: str,
    row_id: str,
    field: str,
) -> list[str]:
    """Accept legacy JSON arrays and the human-friendly one-alias-per-line form."""

    if not value:
        return []
    try:
        return _parse_json_string_array(value, row_id, field)
    except ValueError:
        aliases = _split_lines(value)
        if aliases:
            return aliases
        raise


def _split_lines(value: str) -> list[str]:
    return _dedupe_lines(line.strip() for line in value.splitlines() if line.strip())


def _resolved_episode_no(
    properties: dict[str, Any],
    row_id: str,
    *,
    expected: int | None,
) -> int:
    raw_property = properties.get("회차")
    raw_number = raw_property.get("number") if isinstance(raw_property, dict) else None
    if raw_number is None:
        if expected is None:
            raise ValueError(f"Notion row {row_id} requires 회차 or a Scenario relation.")
        return expected
    value = int(_read_number(properties, "회차"))
    if expected is not None and value != expected:
        raise ValueError(
            f"Notion row {row_id} 회차 differs from its Scenario relation: "
            f"{value} != {expected}."
        )
    return value


def _dedupe_lines(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value.strip())
    return result


def _parse_known_character_names(value: str) -> list[str]:
    raw = value.strip()
    if not raw or raw.casefold() in {"none", "empty", "없음", "-"}:
        return []
    match = KNOWN_CHARACTERS_PATTERN.fullmatch(raw)
    if match is not None:
        raw = match.group("names").strip()
    else:
        for prefix in ("기존 캐릭터:", "캐릭터명:", "known characters:"):
            if raw.casefold().startswith(prefix.casefold()):
                raw = raw[len(prefix) :].strip()
                break
    if not raw:
        return []
    names = []
    for item in re.split(r"[,\n]", raw):
        cleaned = item.strip().removeprefix("-").strip().strip("'\"")
        if cleaned and cleaned.casefold() not in {"none", "empty", "없음"}:
            names.append(cleaned)
    return _dedupe_lines(names)
