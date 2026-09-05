import json
import sys

import pytest

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage2Gold,
    CharacterStateEntry,
    EvaluationState,
    GoldSnapshotV3,
    KnownCharacter,
    ReviewStatus,
    ScenarioGold,
    StartStateMode,
    WorldStage1Gold,
    WorldStage2Gold,
    WorldStateEntry,
    character_state_ref,
    world_state_ref,
)
from evals.multi_stage_setting.loaders import load_gold_snapshot_v3
from evals.multi_stage_setting.report_cli import (
    build_source_free_summary,
    render_markdown_summary,
)
from evals.multi_stage_setting.state_cli import main as state_cli_main
from evals.multi_stage_setting.state_effects import build_gold_state_chain


def test_v3_loader_verifies_fixture_hash_before_attaching_sources(tmp_path) -> None:
    gold = _gold().with_fixture_hash()
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = load_gold_snapshot_v3(path)

    assert loaded.fixture_hash == gold.fixture_hash

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["name"] = "tampered"
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="fixtureHash does not match"):
        load_gold_snapshot_v3(path)


def test_v3_loader_rejects_versionless_payload_and_source_root_escape(tmp_path) -> None:
    versionless = tmp_path / "versionless.json"
    versionless.write_text('{"name":"legacy"}', encoding="utf-8")
    with pytest.raises(ValueError, match="legacy setting_extraction CLI"):
        load_gold_snapshot_v3(versionless)

    scenario = _gold().scenarios[0].model_copy(update={"source_identifier": "../secret.txt"})
    escaping = GoldSnapshotV3(
        dataset_version="v3",
        name="escape",
        scenarios=[scenario],
    ).with_fixture_hash()
    escaping_path = tmp_path / "escaping.json"
    escaping_path.write_text(
        json.dumps(escaping.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    source_root = tmp_path / "sources"
    source_root.mkdir()

    with pytest.raises(ValueError, match="escapes the source root"):
        load_gold_snapshot_v3(escaping_path, source_root=source_root)


def test_v3_loader_maps_author_local_absolute_source_to_episode_pattern(tmp_path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source_text = "실제 평가 원문"
    (source_root / "01화.txt").write_text(source_text, encoding="utf-8")
    scenario = _gold().scenarios[0].model_copy(
        update={"source_identifier": "/Users/author/private/01화.txt"}
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="absolute-author-source",
        scenarios=[scenario],
    ).with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = load_gold_snapshot_v3(gold_path, source_root=source_root)

    assert loaded.scenarios[0].source_text == source_text


def test_external_seed_state_requires_a_content_hash(tmp_path) -> None:
    scenario = _gold().scenarios[0].model_copy(
        update={
            "start_state_mode": StartStateMode.SEED,
            "before_state_uri": "seed.json",
        }
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="external-seed",
        scenarios=[scenario],
    ).with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    state_root = tmp_path / "states"
    state_root.mkdir()
    (state_root / "seed.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="has no beforeState hash"):
        load_gold_snapshot_v3(gold_path, state_root=state_root)


def test_source_free_report_drops_scenario_details_and_source_derived_text() -> None:
    report = {
        "reportVersion": "setting-eval-report/v3",
        "run": {"mode": "FIXED", "domains": ["CHARACTER"]},
        "dataset": {"name": "test", "version": "v3", "episodes": [1]},
        "stages": {
            "character": {
                "stage1": {"metrics": {"candidateF1": 1}, "counts": {"gold": 1}},
                "stage2": {
                    "metrics": {"fullDecisionAccuracy": 1},
                    "counts": {"gold": 1},
                    "matches": [{"evidence": "비밀 원문"}],
                },
            },
            "world": {
                "stage1": {"evaluated": False, "reason": "Domain not selected."},
                "stage2": {"evaluated": False, "reason": "Domain not selected."},
            },
            "macroAverage": {"stage1CandidateF1": 1},
        },
        "endToEnd": {
            "metrics": {"afterStateF1": 1},
            "counts": {"stateApplicationErrors": 0},
            "domains": {"CHARACTER": {"afterStateF1": 1}},
            "scenarios": [{"actualValue": "비밀 상태"}],
        },
        "failureCauses": {},
        "scenarios": [{"evidence": "비밀 원문"}],
    }

    summary = build_source_free_summary(report)
    serialized = json.dumps(summary, ensure_ascii=False)
    markdown = render_markdown_summary(report)

    assert "비밀" not in serialized
    assert "scenarios" not in summary
    assert "100.00%" in markdown


def test_state_cli_generates_verified_hashes_and_updated_gold(tmp_path, monkeypatch) -> None:
    gold = _gold().with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    state_dir = tmp_path / "states"
    updated_path = tmp_path / "gold-with-states.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "state_cli",
            "--gold",
            str(gold_path),
            "--output-dir",
            str(state_dir),
            "--updated-gold",
            str(updated_path),
            "--mode",
            "verified",
        ],
    )

    state_cli_main()

    updated = load_gold_snapshot_v3(updated_path)
    transition = build_gold_state_chain(updated)["S1"]
    assert updated.scenarios[0].state_generation_status == "VERIFIED"
    assert updated.scenarios[0].after_state_hash == (
        f"sha256:{transition.after_state.content_hash()}"
    )
    assert (state_dir / "manifest.json").is_file()
    preview_path = state_dir / "0001-S1.before.notion.md"
    assert preview_path.is_file()
    assert "평가 시작 전 누적 상태 · 자동 생성" in preview_path.read_text(
        encoding="utf-8"
    )
    assert "첫 회차는 빈 상태에서 평가를 시작합니다." in preview_path.read_text(
        encoding="utf-8"
    )


def test_state_cli_materializes_missing_stage2_before_values_from_fixture(
    tmp_path,
    monkeypatch,
) -> None:
    gold = _gold_with_character_update().with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    state_dir = tmp_path / "states"
    updated_path = tmp_path / "gold-with-states.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "state_cli",
            "--gold",
            str(gold_path),
            "--output-dir",
            str(state_dir),
            "--updated-gold",
            str(updated_path),
            "--mode",
            "verified",
        ],
    )

    state_cli_main()

    updated = load_gold_snapshot_v3(updated_path)
    decision = updated.stage2[0]
    assert decision.before_value == "170cm"
    assert decision.before_value_json == {"value": "170cm"}
    world_decision = next(item for item in updated.stage2 if item.decision_id == "D2")
    assert world_decision.before_value == "평균 140cm다."
    assert world_decision.before_value_json is None
    assert updated.scenarios[0].known_character_names == ["비요른", "비요른"]
    assert updated.scenarios[0].provided_context == "knownCharacters=[비요른, 비요른]"
    assert build_gold_state_chain(updated)["S1"].after_state.character_facts[0].value == (
        "180cm"
    )


def test_state_cli_preview_marks_generated_without_official_state_fixtures(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = _gold().scenarios[0].model_copy(
        update={"review_status": ReviewStatus.DRAFT}
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="preview",
        scenarios=[scenario],
    ).with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    state_dir = tmp_path / "states"
    updated_path = tmp_path / "gold-with-preview.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "state_cli",
            "--gold",
            str(gold_path),
            "--output-dir",
            str(state_dir),
            "--updated-gold",
            str(updated_path),
        ],
    )

    state_cli_main()

    updated = load_gold_snapshot_v3(updated_path)
    updated_scenario = updated.scenarios[0]
    assert updated_scenario.state_generation_status == "GENERATED"
    assert updated_scenario.before_state_uri is None
    assert updated_scenario.before_state_hash is None
    assert updated_scenario.after_state_uri is None
    assert updated_scenario.after_state_hash is None
    assert not (state_dir / "0001-S1.before.json").exists()
    assert not (state_dir / "0001-S1.after.json").exists()
    preview_path = state_dir / "0001-S1.before.notion.md"
    assert "검수 전 미리보기" in preview_path.read_text(encoding="utf-8")
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "preview"
    assert manifest["states"][0]["stateGenerationStatus"] == "GENERATED"
    assert "beforeStateUri" not in manifest["states"][0]


def test_state_cli_verified_rejects_non_final_rows(tmp_path, monkeypatch) -> None:
    scenario = _gold().scenarios[0].model_copy(
        update={"review_status": ReviewStatus.DRAFT}
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="invalid-verified",
        scenarios=[scenario],
    ).with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "state_cli",
            "--gold",
            str(gold_path),
            "--output-dir",
            str(tmp_path / "states"),
            "--mode",
            "verified",
        ],
    )

    with pytest.raises(ValueError, match="requires every included Scenario and Gold row"):
        state_cli_main()


def _gold() -> GoldSnapshotV3:
    return GoldSnapshotV3(
        dataset_version="v3",
        name="io",
        scenarios=[
            ScenarioGold(
                scenario_id="S1",
                episode_no=1,
                source_identifier="01화.txt",
                target_domains={"CHARACTER"},
                gold_version="v3",
                candidate_free=True,
                start_state_mode="EMPTY",
                cumulative_through_episode=0,
                review_status="FINAL",
            )
        ],
    )


def _gold_with_character_update() -> GoldSnapshotV3:
    target_ref = character_state_ref(
        "character:bjorn", "PROFILE", "profile.height"
    )
    seed = EvaluationState(
        known_characters=[
            KnownCharacter(
                entity_ref="character:bjorn-variant",
                name="비요른",
                creation_order=2,
            ),
            KnownCharacter(
                entity_ref="character:bjorn",
                name="비요른",
                creation_order=1,
            ),
            KnownCharacter(
                entity_ref="character:retired",
                name="은퇴자",
                creation_order=3,
                active=False,
            ),
        ],
        character_facts=[
            CharacterStateEntry(
                ref=target_ref,
                entity_ref="character:bjorn",
                entity_name="비요른",
                fact_type="PROFILE",
                fact_key="profile.height",
                value_type="STRING",
                value="170cm",
                value_json={"value": "170cm"},
            )
        ],
        world_facts=[
            WorldStateEntry(
                ref=world_state_ref("RACE", "고블린", None, "체격"),
                category="RACE",
                subject_name="고블린",
                setting_name="체격",
                value="평균 140cm다.",
            )
        ],
    )
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains={"CHARACTER", "WORLD"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=seed,
        review_status="FINAL",
    )
    source = CharacterStage1Gold(
        gold_id="C1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["키가 180cm가 되었다."],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref="character:bjorn",
        entity_name="비요른",
        fact_type="PROFILE",
        fact_key="profile.height",
        value_type="STRING",
        display_value="180cm",
        value_json={"value": "180cm"},
        value_json_provenance="ANNOTATED",
        structured_scorable=True,
    )
    decision = CharacterStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["C1"],
        domain="CHARACTER",
        operation="UPDATE",
        temporal_scope="PRESENT",
        target_ref=target_ref,
        proposed_value="180cm",
        proposed_value_json={"value": "180cm"},
        review_status="FINAL",
    )
    world_source = WorldStage1Gold(
        gold_id="W1",
        scenario_id="S1",
        episode_no=1,
        sort_order=2,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["고블린의 평균 체격이 커졌다."],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="RACE",
        subject_name="고블린",
        setting_name="체격",
        source_values=["평균 150cm다."],
    )
    world_decision = WorldStage2Gold(
        decision_id="D2",
        scenario_id="S1",
        episode_no=1,
        sort_order=2,
        source_gold_ids=["W1"],
        domain="WORLD",
        operation="UPDATE",
        consolidation_status="SINGLE",
        target_ref=world_state_ref("RACE", "고블린", None, "체격"),
        matched_property_name="체격",
        proposed_setting_name="체격",
        proposed_value="평균 150cm다.",
        review_status="FINAL",
    )
    return GoldSnapshotV3(
        dataset_version="v3",
        name="io update",
        scenarios=[scenario],
        stage1=[source, world_source],
        stage2=[decision, world_decision],
    )
