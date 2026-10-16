"""Live evaluation uses the product manuscript resolver without changing state policy."""

import asyncio
import json
from types import SimpleNamespace

import pytest

import evals.multi_stage_setting.runtime_adapter as adapter
from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
from app.analysis.world_setting_schemas import (
    ExtractedWorldSettingCandidate,
    WorldSettingComparisonBatchDecision,
    WorldSettingComparisonBatchResult,
    WorldSettingExtractionResult,
)
from app.domain.enums import AnalysisFailureCode
from app.llm.responses import LlmTextResponse
from app.schemas.worker import WorkerWorldSettingSubjectResolutionRequestItem
from evals.multi_stage_setting.contracts import (
    EvaluationState,
    GoldSnapshotV3,
    ScenarioGold,
    WorldStage1Gold,
    WorldStage2Gold,
    WorldStateEntry,
    world_state_ref,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    class Encoding:
        def encode(self, text, **kwargs):
            return list(text.encode())

    monkeypatch.setattr("app.analysis.ordered_context.tiktoken.get_encoding", lambda *args: Encoding())
    monkeypatch.setattr("app.analysis.world_setting_comparator.get_settings", lambda: SimpleNamespace())


class Client:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def create_text_response(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        return LlmTextResponse(text=output if isinstance(output, str) else json.dumps(output))


def row(ref, selected=()):
    return {"candidate_ref": ref, "selected_subject_refs": list(selected),
            "ambiguous": False, "identity_proofs": []}


def result(*rows):
    return {"decisions": list(rows)}


class Extractor:
    def __init__(self, names):
        self.names = names

    async def extract_from_chunk(self, **kwargs):
        return WorldSettingExtractionResult(candidates=[
            ExtractedWorldSettingCandidate(
                category="LOCATION", subject_name=name, setting_name=f"특성{index}",
                extracted_value=f"{name}의 {index}번 특성이다.", confidence=0.9,
                evidence_spans=[{"quote": f"{name}의 {index}번 특성이다."}],
            )
            for index, name in enumerate(self.names, 1)
        ])


class Comparator:
    def __init__(self):
        self.calls = []

    async def compare_batch(self, category, candidates, targets):
        self.calls.append((candidates, targets))
        outcome = WorldSettingComparisonBatchResult(decisions=[
            WorldSettingComparisonBatchDecision(
                source_candidate_refs=[candidate.candidate_ref], consolidation_status="SINGLE",
                operation="ADD", target_ref="T1" if targets else None,
                proposed_setting_name=candidate.setting_name, proposed_value=candidate.extracted_value,
                comparison_reason="원문에서 확인한 특성을 추가합니다.",
            )
            for candidate in candidates
        ])
        return outcome, outcome.model_dump(mode="json")


def state():
    return EvaluationState(world_facts=[WorldStateEntry(
        ref=world_state_ref("LOCATION", "백탑", None, "위치", subject_ref="backend-world:1"),
        subject_ref="backend-world:1", category="LOCATION", subject_name="백탑",
        setting_name="위치", value="동쪽에 있다.",
    )])


def scenario():
    return ScenarioGold(
        scenario_id="S2", episode_no=2, source_identifier="02화.txt",
        source_text="흰 탑의 1번 특성이다. 붉은 탑의 2번 특성이다. 백색 탑의 3번 특성이다. 백탑의 4번 특성이다.",
        target_domains={"WORLD"}, gold_version="v3", start_state_mode="SEED",
        cumulative_through_episode=1, seed_state=state(), candidate_free=True, review_status="FINAL",
    )


def run(names, outputs, *, mode="FIXED", attempts=1):
    client = Client(outputs)
    comparator = Comparator()
    resolver = WorldSettingSubjectResolver(
        llm_client=client, model="subject-model", max_attempts=attempts, max_output_tokens=2000,
    )
    bundle = asyncio.run(adapter.run_multi_stage_predictions(
        GoldSnapshotV3(dataset_version="v3", name="world batch", scenarios=[scenario()]).with_fixture_hash(),
        mode=mode, domains={"WORLD"},
        components=adapter.RuntimeComponents(
            character_comparator=None, world_extractor=Extractor(names),
            world_subject_resolver=resolver, world_comparator=comparator,
        ),
        **({"frozen_start_state": state()} if mode == "COMMON_START" else {}),
    ))
    return bundle, client, comparator


@pytest.mark.parametrize("mode", ["FIXED", "ROLLING", "COMMON_START"])
def test_live_modes_resolve_alias_new_and_later_candidates_in_one_confirmed_request(mode):
    bundle, client, comparator = run(
        ["흰 탑", "붉은 탑", "백색 탑", "백탑"],
        [result(row("C1", ["S1"]), row("C2"), row("C3", ["S1"]))], mode=mode,
    )
    prediction = bundle.scenarios[0]
    assert prediction.failures == []
    assert len(client.calls) == 1
    request = json.loads(client.calls[0]["user_prompt"])
    assert [item["candidate_ref"] for item in request["candidates"]] == ["C1", "C2", "C3"]
    assert all("new_subject_ref" not in item for item in request["candidates"])
    assert [item["ref"] for item in request["initial_subjects"]] == ["S1"]
    assert client.calls[0]["max_output_tokens"] == 32000
    assert bundle.prompt_versions["worldSubjectResolution"] == "world-manuscript-subject-resolution:v1:confirmed"
    assert [[item.subject_name for item in candidates] for candidates, _ in comparator.calls] == [
        ["흰 탑", "백색 탑", "백탑"], ["붉은 탑"],
    ]
    assert [item.target_ref for item in prediction.stage2] == [
        "gold:world-subject-ref:backend-world%3A1", None,
        "gold:world-subject-ref:backend-world%3A1", "gold:world-subject-ref:backend-world%3A1",
    ]
    assert len(prediction.processing) == 4
    assert all(item.status == "COMPARED" for item in prediction.processing)
    after = adapter._apply_runtime_scenario(scenario(), state(), prediction)
    assert {(item.subject_name, item.setting_name) for item in after.world_facts if item.setting_name != "위치"} == {
        ("백탑", "특성1"), ("붉은 탑", "특성2"), ("백탑", "특성3"), ("백탑", "특성4"),
    }


def test_subject_split_failure_is_typed_and_does_not_hide_successful_candidates():
    bundle, client, comparator = run(["흰 탑", "붉은 탑", "백색 탑"], [
        result(row("C1", ["S1"])),  # Missing coverage splits the request.
        result(row("C1", ["S1"])),
        "invalid json",
        "invalid json",
        result(row("C3", ["S1"])),
    ])
    prediction = bundle.scenarios[0]
    ids = [item.candidate_id for item in prediction.stage1]
    assert len(client.calls) == 5
    assert [item.source_candidate_id for item in prediction.stage2] == [ids[0], ids[2]]
    assert [(item.source_id, item.error_type) for item in prediction.failures] == [
        (ids[1], AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR.value),
    ]
    records = {item.candidate_id: item for item in prediction.processing}
    assert records[ids[1]].status == "PREPARATION_FAILED"
    assert records[ids[1]].stage == "WORLD_PREPARATION"
    assert records[ids[1]].failure_code == AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR
    assert not records[ids[1]].comparison_forwarded
    assert records[ids[0]].status == records[ids[2]].status == "COMPARED"
    assert [[item.subject_name for item in candidates] for candidates, _ in comparator.calls] == [
        ["흰 탑", "백색 탑"],
    ]


@pytest.mark.parametrize("indexes", [[], [0, 0], [1, 0]])
def test_malformed_resolver_coverage_fails_every_candidate_without_comparison(monkeypatch, indexes):
    async def malformed(self, candidates, subjects_by_category, **kwargs):
        return [WorkerWorldSettingSubjectResolutionRequestItem(
            candidate_id=candidates[index].candidate_id, target_world_setting_ids=[],
        ) for index in indexes]

    monkeypatch.setattr(WorldSettingSubjectResolver, "select_manuscript_subjects", malformed)
    bundle, client, comparator = run(["흰 탑", "백색 탑"], [])
    prediction = bundle.scenarios[0]
    assert client.calls == comparator.calls == []
    assert prediction.stage2 == []
    assert len(prediction.failures) == len(prediction.processing) == 2
    assert {item.source_id for item in prediction.failures} == {item.candidate_id for item in prediction.stage1}
    assert all(item.error_type == "COMPARISON_VALIDATION_FAILED" for item in prediction.failures)
    assert all(item.status == "PREPARATION_FAILED" for item in prediction.processing)


def test_missing_product_batch_method_cannot_fall_back_to_candidate_requests(monkeypatch):
    monkeypatch.setattr(WorldSettingSubjectResolver, "select_manuscript_subjects", None)
    with pytest.raises(TypeError, match="requires manuscript resolution"):
        run(["흰 탑"], [])


def test_oracle_keeps_gold_target_mapping_without_subject_resolution():
    source = WorldStage1Gold(
        gold_id="W2", scenario_id="S2", episode_no=2, sort_order=1, decision="EXTRACT",
        importance="MUST", evidence_quotes=["흰 탑은 높다."], review_status="FINAL",
        domain="WORLD", candidate_kind="WORLD_SETTING", category="LOCATION", subject_name="흰 탑",
        setting_name="높이", source_values=["높다."],
    )
    gold = GoldSnapshotV3(
        dataset_version="v3", name="oracle world", scenarios=[scenario().model_copy(update={"candidate_free": False})],
        stage1=[source], stage2=[WorldStage2Gold(
            decision_id="D2", scenario_id="S2", episode_no=2, sort_order=1,
            source_gold_ids=["W2"], domain="WORLD", operation="ADD", consolidation_status="SINGLE",
            target_ref="gold:world-subject-ref:backend-world%3A1", proposed_setting_name="높이", proposed_value="높다.",
            review_status="FINAL",
        )],
    ).with_fixture_hash()
    client = Client([])
    resolver = WorldSettingSubjectResolver(llm_client=client, model="subject-model", max_attempts=1, max_output_tokens=2000)
    bundle = asyncio.run(adapter.run_multi_stage_predictions(
        gold, mode="ORACLE", components=adapter.RuntimeComponents(
            character_comparator=None, world_comparator=Comparator(), world_subject_resolver=resolver,
        ),
    ))
    assert client.calls == []
    assert bundle.scenarios[0].stage2[0].target_ref == "gold:world-subject-ref:backend-world%3A1"
    assert bundle.prompt_versions["worldSubjectResolution"] == "world-setting-subject-resolution:v1"
