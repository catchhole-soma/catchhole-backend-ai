"""World comparisons must not spend their input budget on character-only holds."""

import asyncio
import json
from dataclasses import replace

import pytest

from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_manuscript import WorldSettingManuscriptComparator
from app.llm.responses import LlmTextResponse
from app.schemas.analysis_context import WorkerAnalysisReference
from tests.test_world_setting_manuscript import decision, group


class RecordingProvider:
    def __init__(self):
        self.requests = []

    async def create_text_response(self, **request):
        self.requests.append(request)
        payload = json.loads(request["user_prompt"])
        result = {"decisions": [decision(ordered=True)]}
        if "groups" in payload:
            result = {"groups": {item["group_ref"]: result for item in payload["groups"]}}
        return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))


@pytest.mark.parametrize("mode", ["standalone", "packed", "recovery"])
@pytest.mark.parametrize("character_size", [1, 10000], ids=["mixed", "large-character-hold"])
def test_world_requests_keep_world_holds_without_character_input_or_budget_overflow(
    mode, character_size,
):
    refs = (
        WorkerAnalysisReference(
            domain="worldSettings", subjectName="물약1", sourceEpisodeNo=1,
            reason="SCOPE_UNRESOLVED", settingName="효과", scopeName="희석 상태",
            matchedPropertyName="효과", value="회복 효과는 불명확하다.",
            evidenceSpans=[{"quote": "회복 효과는 불명확하다.", "startOffset": 3, "endOffset": 18}],
        ),
        WorkerAnalysisReference(
            domain="characters", subjectName="물약1", sourceEpisodeNo=1,
            reason="GENERAL_UNCERTAINTY", value="CHARACTER_HOLD_ONLY " * character_size,
            evidenceSpans=[{"quote": "인물의 상태는 확인되지 않았다."}],
        ),
        WorkerAnalysisReference(
            domain="worldSettings", subjectName="다른 지역", sourceEpisodeNo=2,
            reason="GENERAL_UNCERTAINTY", settingName="환경", value="환경은 불명확하다.",
        ),
    )
    original = [ref.model_dump(mode="json") for ref in refs]
    expected = [
        {"domain": "worldSettings", "subject_name": "물약1", "source_episode_no": 1,
         "reason": "SCOPE_UNRESOLVED", "setting_name": "효과", "value": "회복 효과는 불명확하다.",
         "evidence": ["회복 효과는 불명확하다."], "confirmation_status": "UNRESOLVED",
         "applied_to_current_state": False, "scope_name": "희석 상태", "matched_property_name": "효과"},
        {"domain": "worldSettings", "subject_name": "다른 지역", "source_episode_no": 2,
         "reason": "GENERAL_UNCERTAINTY", "setting_name": "환경", "value": "환경은 불명확하다.",
         "evidence": [], "confirmation_status": "UNRESOLVED", "applied_to_current_state": False},
    ]
    provider = RecordingProvider()
    comparator = WorldSettingComparator(llm_client=provider, model="offline-test", max_attempts=1)
    comparator.batch_max_input_tokens = 32000
    first = group(1, ordered=True)

    async def compare():
        if mode == "packed":
            runner = WorldSettingManuscriptComparator(comparator, max_input_tokens=32000)
            outcomes = await runner.compare_manuscript([
                replace(group(i, ordered=True), unresolved_references=refs) for i in (1, 2)
            ])
            assert all(outcome.error is None for outcome in outcomes.values())
            return
        result, _ = await comparator.compare_batch(
            first.category, first.candidates, first.targets,
            ordered_context=True, unresolved_references=refs,
            **({"preserve_source_paths": True, "max_attempts_override": 1}
               if mode == "recovery" else {}),
        )
        assert len(result.decisions) == 1

    asyncio.run(compare())
    assert len(provider.requests) == 1
    payload = json.loads(provider.requests[0]["user_prompt"])
    payloads = payload["groups"] if mode == "packed" else [payload]
    assert all(item["unresolved_references"] == expected for item in payloads)
    assert "CHARACTER_HOLD_ONLY" not in provider.requests[0]["user_prompt"]
    assert [ref.model_dump(mode="json") for ref in refs] == original
