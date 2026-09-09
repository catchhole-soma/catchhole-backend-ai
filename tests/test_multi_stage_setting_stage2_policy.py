import hashlib
import json

import pytest
from pydantic import ValidationError

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage2Gold,
    GoldSnapshotV3,
    ScenarioGold,
    Stage2Policy,
    WorldStage1Gold,
)
from evals.multi_stage_setting.loaders import load_gold_snapshot_v3


@pytest.mark.parametrize("explicit_default", [False, True])
def test_required_policy_keeps_legacy_fixture_hash_and_loads_without_new_field(
    tmp_path, explicit_default: bool
) -> None:
    source = _character_source(**({"stage2Policy": "REQUIRED"} if explicit_default else {}))
    gold = _snapshot(source, [_stage2()]).with_fixture_hash()
    payload = gold.model_dump(mode="json", by_alias=True)

    assert source.stage2_policy == Stage2Policy.REQUIRED
    assert "stage2Policy" not in payload["stage1"][0]
    legacy_payload = {key: value for key, value in payload.items() if key != "fixtureHash"}
    legacy_hash = hashlib.sha256(
        json.dumps(
            legacy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    assert gold.fixture_hash == legacy_hash
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = load_gold_snapshot_v3(path)

    assert loaded.fixture_hash == gold.fixture_hash
    assert loaded.stage1[0].stage2_policy == Stage2Policy.REQUIRED


def test_wait_policy_preserves_extract_subject_and_value_without_stage2(tmp_path) -> None:
    source = _character_source(stage2Policy="WAIT_FOR_CHARACTER_MATCH")
    gold = _snapshot(source, []).with_fixture_hash()
    payload = gold.model_dump(mode="json", by_alias=True)

    assert payload["stage1"][0]["stage2Policy"] == "WAIT_FOR_CHARACTER_MATCH"
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loaded = load_gold_snapshot_v3(path)
    row = loaded.stage1[0]

    assert row.decision == "EXTRACT"
    assert row.entity_ref == "character:unknown"
    assert row.entity_name == "미상"
    assert row.fact_key == "profile.species"
    assert row.display_value == "인간"
    assert row.evidence_quotes == ["그는 인간이다."]
    assert row.stage2_policy == Stage2Policy.WAIT_FOR_CHARACTER_MATCH
    assert loaded.stage2 == []
    assert loaded.fixture_hash == gold.fixture_hash


def test_unknown_character_name_does_not_implicitly_enable_wait_policy() -> None:
    with pytest.raises(ValidationError, match="EXTRACT setting rows require one Stage2"):
        _snapshot(_character_source(), [])


def test_wait_policy_rejects_a_linked_stage2_gold() -> None:
    with pytest.raises(ValidationError, match="WAIT_FOR_CHARACTER_MATCH rows must not feed Stage2"):
        _snapshot(_character_source(stage2Policy="WAIT_FOR_CHARACTER_MATCH"), [_stage2()])


@pytest.mark.parametrize("decision", ["DO_NOT_EXTRACT", "REVIEW_REQUIRED"])
def test_wait_policy_requires_an_extract_decision(decision: str) -> None:
    with pytest.raises(
        ValidationError, match="WAIT_FOR_CHARACTER_MATCH requires an EXTRACT Character SETTING"
    ):
        _character_source(decision=decision, stage2Policy="WAIT_FOR_CHARACTER_MATCH")


def test_wait_policy_is_not_available_for_character_discovery() -> None:
    with pytest.raises(
        ValidationError, match="WAIT_FOR_CHARACTER_MATCH requires an EXTRACT Character SETTING"
    ):
        _character_source(
            candidate_kind="CHARACTER_DISCOVERY",
            stage2Policy="WAIT_FOR_CHARACTER_MATCH",
            fact_type=None,
            fact_key=None,
            value_type=None,
            display_value=None,
        )


def test_world_gold_rejects_character_stage2_policy() -> None:
    with pytest.raises(ValidationError, match="stage2Policy"):
        WorldStage1Gold(
            gold_id="W1",
            scenario_id="S1",
            episode_no=1,
            sort_order=1,
            decision="EXTRACT",
            importance="MUST",
            evidence_quotes=["숲에 산다."],
            review_status="FINAL",
            domain="WORLD",
            candidate_kind="WORLD_SETTING",
            category="RACE",
            subject_name="요정",
            setting_name="서식지",
            source_values=["숲"],
            stage2Policy="WAIT_FOR_CHARACTER_MATCH",
        )


def _character_source(**changes) -> CharacterStage1Gold:
    payload = {
        "gold_id": "C1",
        "scenario_id": "S1",
        "episode_no": 1,
        "sort_order": 1,
        "decision": "EXTRACT",
        "importance": "MUST",
        "evidence_quotes": ["그는 인간이다."],
        "review_status": "FINAL",
        "domain": "CHARACTER",
        "candidate_kind": "SETTING",
        "entity_ref": "character:unknown",
        "entity_name": "미상",
        "fact_type": "PROFILE",
        "fact_key": "profile.species",
        "value_type": "STRING",
        "display_value": "인간",
    }
    return CharacterStage1Gold.model_validate({**payload, **changes})


def _stage2() -> CharacterStage2Gold:
    return CharacterStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["C1"],
        domain="CHARACTER",
        operation="ADD",
        temporal_scope="PRESENT",
        proposed_value="인간",
        proposed_value_json={"value": "인간"},
        review_status="FINAL",
    )


def _snapshot(source: CharacterStage1Gold, stage2: list[CharacterStage2Gold]) -> GoldSnapshotV3:
    return GoldSnapshotV3(
        dataset_version="v3",
        name="stage2 policy",
        scenarios=[
            ScenarioGold(
                scenario_id="S1",
                episode_no=1,
                source_identifier="01화.txt",
                target_domains={"CHARACTER"},
                gold_version="v3",
                start_state_mode="EMPTY",
                cumulative_through_episode=0,
                review_status="FINAL",
            )
        ],
        stage1=[source],
        stage2=stage2,
    )
