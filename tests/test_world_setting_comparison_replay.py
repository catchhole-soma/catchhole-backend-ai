import asyncio
import json
import re
import unicodedata
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from app.analysis.world_setting_schemas import WorldSettingComparisonBatchDecision
from app.domain.enums import WorldSettingConsolidationStatus, WorldSettingOperation
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerEvidenceSpan,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingProperty,
)
from evals.world_setting_comparison.replay_cli import _parse_args
from evals.world_setting_comparison.replay_report import (
    ReplayProposal,
    _apply_proposal,
    _proposal_hash_payload,
    _simulated_episode_state,
)
from evals.world_setting_comparison.replay_runner import (
    WorldSettingComparisonReplayRunner,
    _java_uuid_sort_key,
    _proposal_from_decision,
)
from evals.world_setting_comparison.replay_snapshot import (
    ReplayCandidate,
    ReplayDataset,
    ReplayEpisode,
    ReplayTarget,
    backend_duplicate_key,
    load_replay_dataset,
    privacy_safe_hash,
)

WORK_ID = UUID("10000000-0000-0000-0000-000000000001")
TARGET_ID = UUID("20000000-0000-0000-0000-000000000001")
BASE_TIME = datetime.fromisoformat("2026-01-01T09:00:00")


def test_loader_uses_read_only_sql_and_reconstructs_pre_comparison_context() -> None:
    session = FakeReadSession(
        candidates=_candidate_rows(),
        targets=[
            {
                "id": TARGET_ID,
                "category": "RACE",
                "subject_name": "비공개 종족명",
                "properties_json": {"비공개 경로": "현재 값"},
                "version": 2,
                "created_at": BASE_TIME - timedelta(days=30),
            }
        ],
        mutations=[
            {
                "mutation_key": UUID("30000000-0000-0000-0000-000000000001"),
                "target_world_setting_id": TARGET_ID,
                "final_operation": "UPDATE",
                "matched_scope_name": None,
                "matched_property_name": "비공개 경로",
                "final_scope_name": None,
                "final_setting_name": "비공개 경로",
                "before_value": "비교 전 값",
                "base_world_setting_version": 1,
                "reviewed_at": BASE_TIME + timedelta(days=10),
                "applied_world_setting_version": 2,
                "id": UUID("40000000-0000-0000-0000-000000000001"),
            }
        ],
    )

    dataset = load_replay_dataset(session, WORK_ID)

    assert [episode.episode_no for episode in dataset.episodes] == [1, 2, 3, 4]
    assert dataset.candidate_count == 4
    assert len(dataset.dataset_hash) == 64
    for episode in dataset.episodes:
        assert episode.targets[0].properties == (
            WorkerWorldSettingProperty(
                scope_name=None,
                setting_name="비공개 경로",
                value="비교 전 값",
            ),
        )
        assert episode.targets[0].version == 1
    assert not any("eligible-works" in sql for sql in session.sql)
    assert session.sql[0].strip() == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    assert any("mutation-capabilities" in sql for sql in session.sql)
    assert any(
        "candidate.id AS mutation_key" in sql and "COALESCE" not in sql
        for sql in session.sql
    )
    assert all(
        re.search(
            r"^\s*(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE)\b",
            sql,
            re.IGNORECASE,
        )
        is None
        for sql in session.sql
    )


def test_cli_requires_explicit_external_provider_transfer_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "replay_cli",
            "--model",
            "test-model",
            "--output",
            "/private/tmp/report.json",
        ],
    )
    with pytest.raises(SystemExit):
        _parse_args()

    monkeypatch.setattr(
        "sys.argv",
        [
            "replay_cli",
            "--model",
            "test-model",
            "--confirm-external-provider-data-transfer",
            "--output",
            "/private/tmp/report.json",
        ],
    )
    args = _parse_args()

    assert args.confirm_external_provider_data_transfer is True


def test_loader_omits_future_value_when_before_snapshot_is_missing() -> None:
    session = FakeReadSession(
        candidates=_candidate_rows(),
        targets=[
            {
                "id": TARGET_ID,
                "category": "RACE",
                "subject_name": "비공개 종족명",
                "properties_json": {"비공개 경로": "현재 값"},
                "version": 2,
                "created_at": BASE_TIME - timedelta(days=30),
            }
        ],
        mutations=[
            {
                "mutation_key": UUID("30000000-0000-0000-0000-000000000001"),
                "target_world_setting_id": TARGET_ID,
                "final_operation": "UPDATE",
                "matched_scope_name": None,
                "matched_property_name": "비공개 경로",
                "final_scope_name": None,
                "final_setting_name": "비공개 경로",
                "before_value": None,
                "base_world_setting_version": 1,
                "reviewed_at": BASE_TIME + timedelta(days=10),
                "applied_world_setting_version": 2,
                "id": UUID("40000000-0000-0000-0000-000000000001"),
            }
        ],
    )

    dataset = load_replay_dataset(session, WORK_ID)

    assert dataset.reconstruction_fallback_count == 4
    assert all(
        episode.reconstruction_fallback_count == 1
        and episode.targets[0].properties == ()
        and episode.targets[0].version == 1
        for episode in dataset.episodes
    )

    report = asyncio.run(
        WorldSettingComparisonReplayRunner(
            delegate=DeterministicComparisonClient(),
            model="test-model",
            monotonic_ns=StepClock(),
        ).run(dataset)
    )

    assert report["dataset"]["contextReconstructionExact"] is False
    assert report["dataset"]["contextReconstructionFallbackCount"] == 4


def test_loader_reverses_backend_equivalent_property_paths() -> None:
    nfc_scope = "영역"
    nfd_scope = unicodedata.normalize("NFD", nfc_scope)
    session = FakeReadSession(
        candidates=_candidate_rows(),
        targets=[
            {
                "id": TARGET_ID,
                "category": "RACE",
                "subject_name": "비공개 종족명",
                "properties_json": {nfc_scope: {"Habitat": "현재 값"}},
                "version": 2,
                "created_at": BASE_TIME - timedelta(days=30),
            }
        ],
        mutations=[
            {
                "mutation_key": UUID("30000000-0000-0000-0000-000000000001"),
                "target_world_setting_id": TARGET_ID,
                "final_operation": "UPDATE",
                "matched_scope_name": nfc_scope,
                "matched_property_name": "Habitat",
                "final_scope_name": f" {nfd_scope} ",
                "final_setting_name": " habitat ",
                "before_value": "비교 전 값",
                "base_world_setting_version": 1,
                "reviewed_at": BASE_TIME + timedelta(days=10),
                "applied_world_setting_version": 2,
                "id": UUID("40000000-0000-0000-0000-000000000001"),
            }
        ],
    )

    dataset = load_replay_dataset(session, WORK_ID)

    assert dataset.reconstruction_fallback_count == 0
    assert all(
        episode.targets[0].properties
        == (
            WorkerWorldSettingProperty(
                scope_name=nfc_scope,
                setting_name="Habitat",
                value="비교 전 값",
            ),
        )
        for episode in dataset.episodes
    )


def test_backend_duplicate_key_matches_java_lower_not_casefold() -> None:
    assert backend_duplicate_key(" Straße ") == "straße"
    assert backend_duplicate_key("STRASSE") == "strasse"
    assert backend_duplicate_key(" Straße ") != backend_duplicate_key("STRASSE")
    assert backend_duplicate_key("\u00a0Mana\u00a0") == "\u00a0mana\u00a0"


def test_loader_auto_selection_requires_exactly_one_eligible_work() -> None:
    session = FakeReadSession(eligible_work_ids=[WORK_ID, UUID(int=99)])

    with pytest.raises(ValueError, match="exactly one eligible work; found 2"):
        load_replay_dataset(session)

    assert len(session.sql) == 2
    assert session.sql[0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    assert "eligible-works" in session.sql[1]


def test_loader_uses_shared_decision_key_when_v38_column_exists() -> None:
    session = FakeReadSession(
        candidates=_candidate_rows(),
        has_comparison_decision_id=True,
    )

    load_replay_dataset(session, WORK_ID)

    assert any(
        "COALESCE(candidate.comparison_decision_id, candidate.id) AS mutation_key" in sql
        for sql in session.sql
    )


def test_loader_reverses_applied_root_property_moves_from_decision_snapshot() -> None:
    session = FakeReadSession(
        candidates=_candidate_rows(),
        targets=[
            {
                "id": TARGET_ID,
                "category": "RACE",
                "subject_name": "바바리안",
                "properties_json": {
                    "신체 능력": {
                        "생명력": "기존 생명력 값",
                        "근력 기댓값": "100이다.",
                    }
                },
                "version": 4,
                "created_at": BASE_TIME - timedelta(days=30),
            }
        ],
        mutations=[
            {
                "mutation_key": UUID("30000000-0000-0000-0000-000000000001"),
                "target_world_setting_id": TARGET_ID,
                "final_operation": "ADD",
                "matched_scope_name": None,
                "matched_property_name": None,
                "final_scope_name": "신체 능력",
                "final_setting_name": "근력 기댓값",
                "before_value": None,
                "base_world_setting_version": 3,
                "reviewed_at": BASE_TIME + timedelta(days=10),
                "applied_world_setting_version": 4,
                "root_move_snapshots": [
                    {"settingName": "생명력", "beforeValue": "기존 생명력 값"}
                ],
                "root_moves_applied_version": 4,
                "root_moves_disabled": False,
                "id": UUID("40000000-0000-0000-0000-000000000001"),
            }
        ],
        has_comparison_decision_id=True,
        has_root_move_snapshots=True,
        has_root_move_state=True,
    )

    dataset = load_replay_dataset(session, WORK_ID)

    assert dataset.reconstruction_fallback_count == 0
    assert all(
        episode.targets[0].properties
        == (
            WorkerWorldSettingProperty(
                scope_name=None,
                setting_name="생명력",
                value="기존 생명력 값",
            ),
        )
        and episode.targets[0].version == 3
        for episode in dataset.episodes
    )
    assert any(
        "LEFT JOIN world_setting_comparison_decisions AS decision" in sql
        and "root_property_moves_applied_world_setting_version" in sql
        for sql in session.sql
    )


def test_loader_marks_root_move_version_mismatch_as_inexact() -> None:
    session = FakeReadSession(
        candidates=_candidate_rows(),
        targets=[
            {
                "id": TARGET_ID,
                "category": "RACE",
                "subject_name": "바바리안",
                "properties_json": {
                    "신체 능력": {
                        "생명력": "기존 생명력 값",
                        "근력 기댓값": "100이다.",
                    }
                },
                "version": 4,
                "created_at": BASE_TIME - timedelta(days=30),
            }
        ],
        mutations=[
            {
                "mutation_key": UUID("30000000-0000-0000-0000-000000000001"),
                "target_world_setting_id": TARGET_ID,
                "final_operation": "ADD",
                "matched_scope_name": None,
                "matched_property_name": None,
                "final_scope_name": "신체 능력",
                "final_setting_name": "근력 기댓값",
                "before_value": None,
                "base_world_setting_version": 3,
                "reviewed_at": BASE_TIME + timedelta(days=10),
                "applied_world_setting_version": 4,
                "root_move_snapshots": [
                    {"settingName": "생명력", "beforeValue": "기존 생명력 값"}
                ],
                "root_moves_applied_version": 5,
                "root_moves_disabled": False,
                "id": UUID("40000000-0000-0000-0000-000000000001"),
            }
        ],
        has_comparison_decision_id=True,
        has_root_move_snapshots=True,
        has_root_move_state=True,
    )

    dataset = load_replay_dataset(session, WORK_ID)

    assert dataset.reconstruction_fallback_count == 4
    assert all(
        episode.targets[0].properties[0].scope_name is None
        and episode.targets[0].properties[0].setting_name == "생명력"
        for episode in dataset.episodes
    )


def test_runner_compares_same_snapshots_and_emits_only_private_aggregate() -> None:
    dataset = _runner_dataset()
    provider = DeterministicComparisonClient()
    clock = StepClock()

    report = asyncio.run(
        WorldSettingComparisonReplayRunner(
            delegate=provider,
            model="test-model",
            monotonic_ns=clock,
        ).run(dataset)
    )
    repeated_report = asyncio.run(
        WorldSettingComparisonReplayRunner(
            delegate=DeterministicComparisonClient(),
            model="test-model",
            monotonic_ns=StepClock(),
        ).run(dataset)
    )

    assert repeated_report == report
    assert report["dataset"] == {
        "datasetHash": dataset.dataset_hash,
        "episodeRange": {"from": 1, "to": 4},
        "episodeCount": 4,
        "candidateCount": 5,
        "contextReconstructionExact": True,
        "contextReconstructionFallbackCount": 0,
    }
    assert report["arms"]["single"]["providerRequestCount"] == 6
    assert report["arms"]["batch"]["providerRequestCount"] == 5
    assert report["arms"]["single"]["providerLatencyMs"] == 6
    assert report["arms"]["batch"]["providerLatencyMs"] == 5
    assert report["arms"]["single"]["inputTokenCount"] == 600
    assert report["arms"]["batch"]["inputTokenCount"] == 500
    assert report["arms"]["single"]["cachedInputTokenCount"] == 60
    assert report["arms"]["batch"]["cachedInputTokenCount"] == 50
    assert report["arms"]["single"]["outputTokenCount"] == 120
    assert report["arms"]["batch"]["outputTokenCount"] == 100
    assert report["arms"]["single"]["batchCount"] == 5
    assert report["arms"]["batch"]["batchCount"] == 4
    assert report["arms"]["single"]["decisionCount"] == 5
    assert report["arms"]["batch"]["decisionCount"] == 4
    assert report["arms"]["batch"]["clusterCount"] == 4
    assert report["arms"]["batch"]["consolidationStatusCounts"] == {
        "SINGLE": 3,
        "MERGED": 1,
        "CONFLICT": 0,
    }
    assert report["arms"]["single"]["stateChangeCounts"] == {
        "addedPathCount": 5,
        "removedPathCount": 0,
        "changedPathCount": 0,
    }
    assert report["arms"]["batch"]["stateChangeCounts"] == {
        "addedPathCount": 4,
        "removedPathCount": 0,
        "changedPathCount": 0,
    }
    assert report["delta"]["providerRequestCount"] == -1
    assert report["delta"]["decisionCount"] == -1

    subject_prompts = [
        request
        for request in provider.requests
        if "subjects" in json.loads(request["user_prompt"])
    ]
    assert len(subject_prompts) == 2
    assert {request["model"] for request in provider.requests} == {"test-model"}
    assert any(
        len(json.loads(request["user_prompt"]).get("candidates", [])) == 2
        for request in provider.requests
    )
    assert sum(
        request["user_prompt"].count("비밀 근거") for request in provider.requests
    ) == 10
    assert sum(
        request["user_prompt"].count("기존 값") for request in provider.requests
    ) == 9
    serialized = json.dumps(report, ensure_ascii=False)
    for private_value in (
        str(WORK_ID),
        str(TARGET_ID),
        "고블린",
        "고블린 무리",
        "비밀 근거",
        "비밀 값",
        "비밀 설정",
    ):
        assert private_value not in serialized


def test_batch_overflow_holds_entire_cluster_without_provider_comparison() -> None:
    dataset = _runner_dataset(first_episode_candidate_count=21)
    provider = DeterministicComparisonClient()

    report = asyncio.run(
        WorldSettingComparisonReplayRunner(
            delegate=provider,
            model="test-model",
            monotonic_ns=StepClock(),
        ).run(dataset)
    )

    # single: 24 candidate comparisons. batch: episodes 2~4 only; episode 1 is held.
    assert report["arms"]["single"]["providerRequestCount"] == 24
    assert report["arms"]["batch"]["providerRequestCount"] == 3
    assert report["arms"]["batch"]["batchCount"] == 4
    assert report["arms"]["batch"]["operationCounts"]["REVIEW_REQUIRED"] == 21
    batch_comparison_prompts = [
        json.loads(request["user_prompt"])
        for request in provider.requests[24:]
        if "candidates" in json.loads(request["user_prompt"])
    ]
    assert all(len(prompt["candidates"]) == 1 for prompt in batch_comparison_prompts)


def test_batch_character_limit_holds_entire_cluster_without_provider_comparison() -> None:
    dataset = _runner_dataset(
        first_episode_candidate_count=1,
        first_evidence_quote="x" * 30_001,
    )
    provider = DeterministicComparisonClient()

    report = asyncio.run(
        WorldSettingComparisonReplayRunner(
            delegate=provider,
            model="test-model",
            monotonic_ns=StepClock(),
        ).run(dataset)
    )

    assert report["arms"]["single"]["providerRequestCount"] == 4
    assert report["arms"]["batch"]["providerRequestCount"] == 3
    assert report["arms"]["batch"]["operationCounts"]["REVIEW_REQUIRED"] == 1


def test_ambiguous_candidates_stay_in_separate_batches_with_stable_target_order() -> None:
    negative_target_id = UUID("80000000-0000-0000-0000-000000000001")
    positive_target_id = UUID("00000000-0000-0000-0000-000000000002")
    assert sorted(
        [positive_target_id, negative_target_id],
        key=_java_uuid_sort_key,
    ) == [negative_target_id, positive_target_id]

    targets = (
        ReplayTarget(
            world_setting_id=positive_target_id,
            category="RACE",
            subject_name="STRASSE",
            properties=(
                WorkerWorldSettingProperty(
                    scope_name=None,
                    setting_name="기존 설정",
                    value="기존 값",
                ),
            ),
            version=1,
            created_at=BASE_TIME - timedelta(days=1),
        ),
        ReplayTarget(
            world_setting_id=negative_target_id,
            category="RACE",
            subject_name="Straße",
            properties=(
                WorkerWorldSettingProperty(
                    scope_name=None,
                    setting_name="기존 설정",
                    value="기존 값",
                ),
            ),
            version=1,
            created_at=BASE_TIME - timedelta(days=1),
        ),
    )
    candidates = tuple(
        ReplayCandidate(
            episode_no=1,
            created_at=BASE_TIME,
            compared_at=BASE_TIME + timedelta(hours=1),
            payload=WorkerWorldSettingCandidatePayload(
                candidate_id=UUID(
                    f"90000000-0000-0000-0000-{index:012d}"
                ),
                work_id=WORK_ID,
                source_episode_id=UUID("60000000-0000-0000-0000-000000000001"),
                category="RACE",
                subject_name="straße",
                scope_name=None,
                setting_name=f"새 설정 {index}",
                extracted_value=f"새 값 {index}",
                evidence_spans=[WorkerEvidenceSpan(quote="비밀 근거")],
                extraction_confidence=0.95,
            ),
        )
        for index in range(1, 3)
    )
    dataset = ReplayDataset(
        work_id=WORK_ID,
        episodes=tuple(
            ReplayEpisode(
                episode_no=episode_no,
                candidates=candidates if episode_no == 1 else (),
                targets=targets,
            )
            for episode_no in range(1, 5)
        ),
        dataset_hash="private-hash",
    )
    provider = DeterministicComparisonClient()

    report = asyncio.run(
        WorldSettingComparisonReplayRunner(
            delegate=provider,
            model="test-model",
            monotonic_ns=StepClock(),
        ).run(dataset)
    )

    assert report["arms"]["batch"]["batchCount"] == 2
    batch_prompts = [
        json.loads(request["user_prompt"])
        for request in provider.requests
        if "candidates" in json.loads(request["user_prompt"])
    ]
    assert len(batch_prompts) == 2
    assert all(len(prompt["candidates"]) == 1 for prompt in batch_prompts)
    assert all(
        prompt["targets"][0]["subject_name"] == "Straße"
        and prompt["targets"][1]["subject_name"] == "STRASSE"
        for prompt in batch_prompts
    )


def test_batch_candidate_tie_break_uses_java_uuid_order() -> None:
    target = ReplayTarget(
        world_setting_id=TARGET_ID,
        category="RACE",
        subject_name="고블린",
        properties=(
            WorkerWorldSettingProperty(
                scope_name=None,
                setting_name="기존 설정",
                value="기존 값",
            ),
        ),
        version=1,
        created_at=BASE_TIME - timedelta(days=1),
    )

    def replay_candidate(candidate_id: UUID, setting_name: str) -> ReplayCandidate:
        return ReplayCandidate(
            episode_no=1,
            created_at=BASE_TIME,
            compared_at=BASE_TIME + timedelta(hours=1),
            payload=WorkerWorldSettingCandidatePayload(
                candidate_id=candidate_id,
                work_id=WORK_ID,
                source_episode_id=UUID("60000000-0000-0000-0000-000000000001"),
                category="RACE",
                subject_name="고블린",
                scope_name=None,
                setting_name=setting_name,
                extracted_value="새 값",
                evidence_spans=[WorkerEvidenceSpan(quote="비밀 근거")],
                extraction_confidence=0.95,
            ),
        )

    positive_candidate = replay_candidate(
        UUID("00000000-0000-0000-0000-000000000001"),
        "양수 후보",
    )
    negative_candidate = replay_candidate(
        UUID("80000000-0000-0000-0000-000000000001"),
        "음수 후보",
    )
    dataset = ReplayDataset(
        work_id=WORK_ID,
        episodes=(
            ReplayEpisode(
                episode_no=1,
                candidates=(positive_candidate, negative_candidate),
                targets=(target,),
            ),
        ),
        dataset_hash="private-hash",
    )
    provider = DeterministicComparisonClient()

    asyncio.run(
        WorldSettingComparisonReplayRunner(
            delegate=provider,
            model="test-model",
            monotonic_ns=StepClock(),
        ).run(dataset)
    )

    batch_prompt = next(
        json.loads(request["user_prompt"])
        for request in provider.requests
        if "candidates" in json.loads(request["user_prompt"])
    )
    assert [
        candidate["setting_name"] for candidate in batch_prompt["candidates"]
    ] == ["음수 후보", "양수 후보"]


def test_conflict_proposal_does_not_change_simulated_final_state() -> None:
    state = {("RACE", "TARGET:private", None, "비밀 설정"): "기존 값"}
    proposal = ReplayProposal(
        episode_no=1,
        category="RACE",
        source_candidate_ids=(UUID(int=1), UUID(int=2)),
        canonical_subject_key="TARGET:private",
        canonical_subject_name="비공개",
        target_world_setting_id=None,
        matched_scope_name=None,
        matched_setting_name="비밀 설정",
        consolidation_status=WorldSettingConsolidationStatus.CONFLICT.value,
        operation=WorldSettingOperation.UPDATE.value,
        proposed_scope_name=None,
        proposed_setting_name="비밀 설정",
        proposed_value="충돌 값",
    )

    _apply_proposal(state, proposal)

    assert state == {
        ("RACE", "TARGET:private", None, "비밀 설정"): "기존 값"
    }


def test_new_subjects_with_same_name_and_path_stay_separate_by_category() -> None:
    state: dict[tuple[str, str, str | None, str], str] = {}
    proposals = [
        ReplayProposal(
            episode_no=1,
            category=category,
            source_candidate_ids=(UUID(int=index),),
            canonical_subject_key="NEW:same-subject",
            canonical_subject_name="비공개",
            target_world_setting_id=None,
            matched_scope_name=None,
            matched_setting_name=None,
            consolidation_status=WorldSettingConsolidationStatus.SINGLE.value,
            operation=WorldSettingOperation.ADD.value,
            proposed_scope_name=None,
            proposed_setting_name="같은 경로",
            proposed_value=f"값 {index}",
        )
        for index, category in enumerate(("RACE", "LOCATION"), start=1)
    ]

    for proposal in proposals:
        _apply_proposal(state, proposal)

    assert state == {
        ("RACE", "NEW:same-subject", None, "같은 경로"): "값 1",
        ("LOCATION", "NEW:same-subject", None, "같은 경로"): "값 2",
    }


def test_add_moves_existing_root_property_before_adding_new_sibling() -> None:
    state = {("RACE", "TARGET:private", None, "생명력"): "기존 생명력 값"}
    proposal = ReplayProposal(
        episode_no=3,
        category="RACE",
        source_candidate_ids=(UUID(int=1),),
        canonical_subject_key="TARGET:private",
        canonical_subject_name="바바리안",
        target_world_setting_id=None,
        matched_scope_name=None,
        matched_setting_name=None,
        consolidation_status=WorldSettingConsolidationStatus.SINGLE.value,
        operation=WorldSettingOperation.ADD.value,
        proposed_scope_name="신체 능력",
        proposed_setting_name="근력 기댓값",
        proposed_value="근력 기댓값은 100이다.",
        existing_root_property_names_to_move=("생명력",),
    )

    _apply_proposal(state, proposal)

    assert state == {
        ("RACE", "TARGET:private", "신체 능력", "생명력"): "기존 생명력 값",
        ("RACE", "TARGET:private", "신체 능력", "근력 기댓값"): (
            "근력 기댓값은 100이다."
        ),
    }
    assert _proposal_hash_payload(proposal)["existingRootPropertyNamesToMove"] == ["생명력"]


def test_replay_proposal_preserves_existing_root_property_moves() -> None:
    decision = WorldSettingComparisonBatchDecision(
        source_candidate_refs=["C1"],
        existing_root_property_names_to_move=["생명력"],
        consolidation_status="SINGLE",
        operation="ADD",
        target_ref="T1",
        proposed_scope_name="신체 능력",
        proposed_setting_name="근력 기댓값",
        proposed_value="근력 기댓값은 100이다.",
        comparison_reason="기존 생명력과 같은 범위에 추가한다.",
    )

    proposal = _proposal_from_decision(
        episode_no=3,
        category="RACE",
        source_candidate_ids=(UUID(int=1),),
        resolution=SimpleNamespace(
            canonical_subject_key=f"TARGET:{TARGET_ID}",
            canonical_subject_name="바바리안",
        ),
        decision=decision,
        targets=[SimpleNamespace(world_setting_id=TARGET_ID)],
    )

    assert proposal.existing_root_property_names_to_move == ("생명력",)


def test_simulated_update_uses_backend_equivalent_path_and_value() -> None:
    target = ReplayTarget(
        world_setting_id=TARGET_ID,
        category="RACE",
        subject_name="비공개",
        properties=(
            WorkerWorldSettingProperty(
                scope_name="Café",
                setting_name="Habitat",
                value="기존 값",
            ),
        ),
        version=1,
        created_at=BASE_TIME - timedelta(days=1),
    )
    dataset = ReplayDataset(
        work_id=WORK_ID,
        episodes=(ReplayEpisode(episode_no=1, candidates=(), targets=(target,)),),
        dataset_hash="private-hash",
    )
    proposal = ReplayProposal(
        episode_no=1,
        category="RACE",
        source_candidate_ids=(UUID(int=1),),
        canonical_subject_key=f"TARGET:{TARGET_ID}",
        canonical_subject_name="비공개",
        target_world_setting_id=TARGET_ID,
        matched_scope_name=" cafe\u0301 ",
        matched_setting_name=" habitat ",
        consolidation_status=WorldSettingConsolidationStatus.SINGLE.value,
        operation=WorldSettingOperation.UPDATE.value,
        proposed_scope_name="CAFE\u0301",
        proposed_setting_name="HABITAT",
        proposed_value=" Cafe\u0301 ",
    )

    state = _simulated_episode_state(dataset, (proposal,))

    assert state == {
        (1, "RACE", f"TARGET:{TARGET_ID}", "café", "habitat"): "Café"
    }


class FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self.rows


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> FakeMappings:
        return FakeMappings(self.rows)


class FakeReadSession:
    def __init__(
        self,
        *,
        eligible_work_ids: list[UUID] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        targets: list[dict[str, Any]] | None = None,
        mutations: list[dict[str, Any]] | None = None,
        has_comparison_decision_id: bool = False,
        has_root_move_snapshots: bool = False,
        has_root_move_state: bool = False,
    ) -> None:
        self.eligible_work_ids = eligible_work_ids or []
        self.candidates = candidates or []
        self.targets = targets or []
        self.mutations = mutations or []
        self.has_comparison_decision_id = has_comparison_decision_id
        self.has_root_move_snapshots = has_root_move_snapshots
        self.has_root_move_state = has_root_move_state
        self.sql: list[str] = []

    def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> FakeResult:
        sql = str(statement)
        self.sql.append(sql)
        if "eligible-works" in sql:
            return FakeResult(
                [{"work_id": work_id} for work_id in self.eligible_work_ids]
            )
        if "replay:candidates" in sql:
            return FakeResult(self.candidates)
        if "replay:targets" in sql:
            return FakeResult(self.targets)
        if "mutation-capabilities" in sql:
            return FakeResult(
                [
                    {
                        "has_comparison_decision_id": (
                            self.has_comparison_decision_id
                        ),
                        "has_root_move_snapshots": self.has_root_move_snapshots,
                        "has_root_move_state": self.has_root_move_state,
                    }
                ]
            )
        if "replay:mutations" in sql:
            return FakeResult(self.mutations)
        return FakeResult([])


class StepClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 1_000_000
        return self.value


class DeterministicComparisonClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create_text_response(self, **kwargs: Any) -> LlmTextResponse:
        self.requests.append(kwargs)
        payload = json.loads(kwargs["user_prompt"])
        if "subjects" in payload:
            response = {"selected_subject_refs": ["S1"]}
        elif "candidate" in payload:
            candidate = payload["candidate"]
            response = {
                "consolidation_status": "SINGLE",
                "operation": "ADD",
                "target_ref": "T1",
                "matched_scope_name": None,
                "matched_property_name": None,
                "proposed_scope_name": candidate["scope_name"],
                "proposed_setting_name": candidate["setting_name"],
                "proposed_value": candidate["extracted_value"],
                "comparison_reason": "새로운 설정을 추가할 수 있다.",
            }
        else:
            candidates = payload["candidates"]
            if len(candidates) > 1:
                decision = {
                    "source_candidate_refs": [candidate["ref"] for candidate in candidates],
                    "consolidation_status": "MERGED",
                    "operation": "ADD",
                    "target_ref": "T1",
                    "matched_scope_name": None,
                    "matched_property_name": None,
                    "proposed_scope_name": None,
                    "proposed_setting_name": "통합 설정",
                    "proposed_value": "통합 값",
                    "comparison_reason": "여러 근거가 하나의 설정을 보완한다.",
                }
            else:
                candidate = candidates[0]
                decision = {
                    "source_candidate_refs": [candidate["ref"]],
                    "consolidation_status": "SINGLE",
                    "operation": "ADD",
                    "target_ref": "T1",
                    "matched_scope_name": None,
                    "matched_property_name": None,
                    "proposed_scope_name": candidate["scope_name"],
                    "proposed_setting_name": candidate["setting_name"],
                    "proposed_value": candidate["extracted_value"],
                    "comparison_reason": "새로운 설정을 추가할 수 있다.",
                }
            response = {"decisions": [decision]}
        return LlmTextResponse(
            text=json.dumps(response, ensure_ascii=False),
            input_token_count=100,
            cached_input_token_count=10,
            output_token_count=20,
        )


def _candidate_rows() -> list[dict[str, Any]]:
    return [
        {
            "episode_no": episode_no,
            "candidate_id": UUID(f"50000000-0000-0000-0000-{episode_no:012d}"),
            "work_id": WORK_ID,
            "source_episode_id": UUID(
                f"60000000-0000-0000-0000-{episode_no:012d}"
            ),
            "category": "RACE",
            "subject_name": "비공개 종족명",
            "scope_name": None,
            "setting_name": f"비공개 설정 {episode_no}",
            "extracted_value": f"비공개 값 {episode_no}",
            "evidence_spans": [{"quote": f"비공개 근거 {episode_no}"}],
            "extraction_confidence": 0.95,
            "created_at": BASE_TIME + timedelta(days=episode_no),
            "compared_at": BASE_TIME + timedelta(days=episode_no, hours=1),
        }
        for episode_no in range(1, 5)
    ]


def _runner_dataset(
    first_episode_candidate_count: int = 2,
    first_evidence_quote: str = "비밀 근거",
) -> ReplayDataset:
    target = ReplayTarget(
        world_setting_id=TARGET_ID,
        category="RACE",
        subject_name="고블린",
        properties=(
            WorkerWorldSettingProperty(
                scope_name=None,
                setting_name="기존 설정",
                value="기존 값",
            ),
        ),
        version=1,
        created_at=BASE_TIME - timedelta(days=1),
    )
    episodes: list[ReplayEpisode] = []
    candidate_sequence = 0
    for episode_no in range(1, 5):
        episode_candidate_count = (
            first_episode_candidate_count if episode_no == 1 else 1
        )
        candidates: list[ReplayCandidate] = []
        for index in range(episode_candidate_count):
            candidate_sequence += 1
            subject_name = (
                "고블린 무리"
                if episode_no == 1 and index == episode_candidate_count - 1
                and first_episode_candidate_count == 2
                else "고블린"
            )
            payload = WorkerWorldSettingCandidatePayload(
                candidate_id=UUID(
                    f"70000000-0000-0000-0000-{candidate_sequence:012d}"
                ),
                work_id=WORK_ID,
                source_episode_id=UUID(
                    f"80000000-0000-0000-0000-{episode_no:012d}"
                ),
                category="RACE",
                subject_name=subject_name,
                scope_name=None,
                setting_name=f"비밀 설정 {candidate_sequence}",
                extracted_value=f"비밀 값 {candidate_sequence}",
                evidence_spans=[
                    WorkerEvidenceSpan(
                        quote=(
                            first_evidence_quote
                            if episode_no == 1 and index == 0
                            else "비밀 근거"
                        )
                    )
                ],
                extraction_confidence=0.95,
            )
            candidates.append(
                ReplayCandidate(
                    episode_no=episode_no,
                    created_at=BASE_TIME + timedelta(days=episode_no),
                    compared_at=BASE_TIME + timedelta(days=episode_no, hours=1),
                    payload=payload,
                )
            )
        episodes.append(
            ReplayEpisode(
                episode_no=episode_no,
                candidates=tuple(candidates),
                targets=(target,),
            )
        )
    episode_tuple = tuple(episodes)
    return ReplayDataset(
        work_id=WORK_ID,
        episodes=episode_tuple,
        dataset_hash=privacy_safe_hash(
            WORK_ID,
            {
                "candidateIds": [
                    str(candidate.payload.candidate_id)
                    for episode in episode_tuple
                    for candidate in episode.candidates
                ]
            },
            "test-dataset",
        ),
    )
