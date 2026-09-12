"""Semantic subject choices remain explicit, with no network or state writes."""
import asyncio
import json
import socket
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analysis import ordered_context, world_setting_comparator
from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerWorldSettingSubjectPageResponse, WorkerWorldSettingSubjectResolutionCandidate,
    WorkerWorldSettingSubjectResolutionPendingResponse, WorkerWorldSettingSubjectResolutionResponse,
)
from tests.test_ordered_analysis_runtime import _input_context


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    class Encoding:
        def encode(self, value, **kwargs):
            return list(value.encode())
    def denied(*args, **kwargs):
        raise AssertionError("No network allowed")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(ordered_context.tiktoken, "get_encoding", lambda *args: Encoding())
    monkeypatch.setattr(world_setting_comparator, "get_settings", lambda: SimpleNamespace())


def run_selection(outputs, *, names=("캐릭터", "캐릭터", "캐릭터"), categories=None, evidence_quotes=None):
    context = _input_context()
    candidates = [WorkerWorldSettingSubjectResolutionCandidate(
        candidate_id=uuid4(), source_episode_id=uuid4(), source_episode_no=18,
        category=(categories or ["POWER_SYSTEM"] * len(names))[index], subject_name=name,
        evidence_spans=[{"quote": value}],
    ) for index, (name, value) in enumerate(zip(names, evidence_quotes or [
        "일반 게임 캐릭터의 장비 판매 시 종합 아이템 레벨이 하락한다.",
        "일반 게임 캐릭터의 종합 전투 지수는 스톤을 사용해 높인다.",
        "게임 캐릭터의 종합 수치는 같은 시스템의 측정값이다.",
    ]))]
    before = [candidate.model_dump() for candidate in candidates]
    calls, saved, pages = [], [], []
    class Client:
        async def create_text_response(self, **kwargs):
            calls.append(kwargs)
            return LlmTextResponse(text=json.dumps(outputs[len(calls) - 1]))
    class Spring:
        async def get_pending_world_setting_subject_resolutions(self, *args):
            return WorkerWorldSettingSubjectResolutionPendingResponse(
                candidates=[] if saved else candidates, analysis_context=context,
            )
        async def get_world_setting_subjects(self, job, lease, category, page):
            pages.append(category)
            return WorkerWorldSettingSubjectPageResponse(subjects=[], page=0, has_next=False)
        async def complete_world_setting_subject_resolutions(self, job, lease, request):
            saved.append(request)
            return WorkerWorldSettingSubjectResolutionResponse(resolutions=[{
                "candidate_id": row.candidate_id,
                "resolution_type": "AMBIGUOUS" if row.ambiguous else "NEW",
                "canonical_subject_key": "pending" if row.ambiguous else row.provisional_subject_keys[0],
                "canonical_subject_name": names[index], "target_world_setting_ids": [],
                "provisional_subject_keys": row.provisional_subject_keys,
            } for index, row in enumerate(request.resolutions)])
    resolver = WorldSettingSubjectResolver(llm_client=Client(), model="offline", max_attempts=1,
                                           max_output_tokens=2000)
    asyncio.run(WorldSettingComparisonPipeline(Spring(), resolver, object())._prepare_subject_resolutions(
        uuid4(), uuid4(), context,
    ))
    assert [candidate.model_dump() for candidate in candidates] == before
    return candidates, calls, saved[0].resolutions, pages


def test_same_game_concept_reuses_first_provisional_only_after_explicit_selection():
    choice = {"selected_subject_refs": ["S1"], "ambiguous": False}
    sources, calls, results, pages = run_selection([choice, choice])
    anchor = f"provisional-world:{sources[0].candidate_id}"
    assert [row.provisional_subject_keys for row in results] == [[anchor]] * 3
    assert pages == ["POWER_SYSTEM"] and len(calls) == 2
    assert "그 참조를 재사용" in calls[0]["system_prompt"]
    assert "자동 연결의 충분한 근거가 아닙니다" in calls[0]["system_prompt"]
    second_input = json.loads(calls[1]["user_prompt"])
    assert len(second_input["subjects"]) == 1
    assert second_input["subjects"][0]["identity_evidence"] == [
        sources[0].evidence_spans[0].quote, sources[1].evidence_spans[0].quote,
    ]
    assert all(str(source.candidate_id) not in str(calls) for source in sources)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_same_name_does_not_force_reuse_when_model_selects_new_or_uncertain(ambiguous):
    sources, calls, results, _ = run_selection(
        [{"selected_subject_refs": [], "ambiguous": ambiguous}], names=("백탑", "백탑"),
    )
    assert len(calls) == 1
    assert results[0].provisional_subject_keys == [f"provisional-world:{sources[0].candidate_id}"]
    assert results[1].provisional_subject_keys == (
        [] if ambiguous else [f"provisional-world:{sources[1].candidate_id}"]
    )
    assert results[1].ambiguous is ambiguous


def test_same_name_in_another_category_is_not_a_selectable_identity():
    sources, calls, results, pages = run_selection([], names=("캐릭터", "캐릭터"),
        categories=["POWER_SYSTEM", "FACTION"])
    assert calls == []
    assert pages == ["POWER_SYSTEM", "FACTION"]
    assert results[0].provisional_subject_keys != results[1].provisional_subject_keys


def test_ambiguous_selection_is_held_without_polluting_later_subject_evidence():
    sources, calls, results, _ = run_selection(
        [{"selected_subject_refs": ["S1"], "ambiguous": True},
         {"selected_subject_refs": ["S1"], "ambiguous": False}],
        names=("마석", "스톤", "마석"), categories=["IMPORTANT_ITEM"] * 3,
        evidence_quotes=["마석은 마물에서 얻는다.", "스톤으로 가격을 표시한다.",
                         "마석을 모아 환전한다."],
    )
    anchor = f"provisional-world:{sources[0].candidate_id}"
    assert len(results) == 3 and len(calls) == 2
    assert [row.provisional_subject_keys for row in results] == [[anchor], [], [anchor]]
    assert [row.ambiguous for row in results] == [False, True, False]
    assert results[1].target_world_setting_ids == []
    next_input = json.loads(calls[1]["user_prompt"])
    assert len(next_input["subjects"]) == 1
    assert next_input["subjects"][0]["identity_evidence"] == [sources[0].evidence_spans[0].quote]


def test_distinct_monster_classifications_remain_separate_when_model_selects_new():
    names = ("변이종", "상위종", "희귀종", "상위 변이종")
    sources, calls, results, _ = run_selection(
        [{"selected_subject_refs": [], "ambiguous": False}] * 3,
        names=names, categories=["MONSTER"] * 4,
        evidence_quotes=[f"가상 분류 목록의 {index}번은 {name}을 별도로 설명한다."
                         for index, name in enumerate(names, 1)],
    )
    assert len(calls) == 3  # No additional model or mandatory-review step.
    assert [row.provisional_subject_keys for row in results] == [
        [f"provisional-world:{source.candidate_id}"] for source in sources
    ]
    assert "연관된 분류나 상위·하위 종류는 같은 대상의 별칭이 아닙니다" in calls[0]["system_prompt"]
    assert "변이종·상위종·희귀종·상위 변이종" in calls[0]["system_prompt"]


def test_different_names_with_explicit_alias_evidence_can_still_select_same_subject():
    sources, calls, results, _ = run_selection(
        [{"selected_subject_refs": ["S1"], "ambiguous": False}],
        names=("상위 변이종", "네임드"), categories=["MONSTER"] * 2,
        evidence_quotes=["이 가상 작품에서는 상위 변이종을 네임드라고도 부른다."] * 2,
    )
    assert len(calls) == 1
    assert results[0].provisional_subject_keys == results[1].provisional_subject_keys
    assert results[0].provisional_subject_keys == [f"provisional-world:{sources[0].candidate_id}"]


@pytest.mark.parametrize("names", [
    ("어둠의 근원", "영생자"),
    ("일반 정수", "수호자의 정수"),
    ("뱀파이어의 노랑 정수", "뱀파이어의 빨강 정수"),
])
def test_related_names_remain_distinct_with_explicit_synthetic_distinction(names):
    # Episode 44 provides these names, not the lost provider response. The
    # distinction below is controlled evidence, not a claim about the live text.
    sources, calls, results, _ = run_selection(
        [{"selected_subject_refs": [], "ambiguous": False}],
        names=names, categories=["POWER_SYSTEM"] * 2,
        evidence_quotes=[f"합성 예시에서는 {names[0]}와 {names[1]}를 별개의 종류로 구분한다."] * 2,
    )
    assert len(calls) == 1
    assert [row.provisional_subject_keys for row in results] == [
        [f"provisional-world:{source.candidate_id}"] for source in sources
    ]
    assert "연관된 분류나 상위·하위 종류는 같은 대상의 별칭이 아닙니다" in calls[0]["system_prompt"]
