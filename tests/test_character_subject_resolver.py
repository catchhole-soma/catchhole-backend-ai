from pathlib import Path
from uuid import UUID

from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.character_subject_resolver import (
    CharacterSubjectResolver,
    SubjectResolutionChunkContext,
)
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.llm.responses import LlmTextResponse

BJORN_ID = UUID("00000000-0000-0000-0000-000000000101")
AINAR_ID = UUID("00000000-0000-0000-0000-000000000102")
CHUNK_ID = UUID("00000000-0000-0000-0000-000000000201")


def test_resolve_candidates_skips_llm_when_fallback_targets_do_not_exist(tmp_path: Path) -> None:
    # 지칭어 + placeholder 후보가 없으면 subject resolver LLM을 추가 호출하지 않는다.
    llm_client = FakeSubjectResolutionClient(response_text='{"resolutions":[]}')
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )
    candidates = [_candidate(entity_name="비요른", raw_entity_mention="비요른")]

    result = resolver.resolve_candidates(
        context=_context(),
        candidates=candidates,
        known_characters=[KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")],
    )

    assert llm_client.call_count == 0
    assert result.candidates == candidates
    assert result.fallback_call_count == 0
    assert result.fallback_resolved_count == 0
    assert result.fallback_discarded_count == 0


def test_resolve_candidates_batches_targets_and_discards_unresolved_items(tmp_path: Path) -> None:
    # 같은 current chunk 안에서 나온 fallback 대상 후보들은 한 번의 LLM 호출로 함께 해소한다.
    llm_client = FakeSubjectResolutionClient(
        response_text="""
        {
          "resolutions": [
            {
              "candidate_id": "candidate-0",
              "resolved_entity_name": "비요른 얀델",
              "reason": "1인칭 서술 흐름이 비요른 얀델에게 이어진다."
            },
            {
              "candidate_id": "candidate-1",
              "resolved_entity_name": null,
              "reason": "그녀가 아이나르인지 다른 인물인지 확정할 수 없다."
            },
            {
              "candidate_id": "candidate-2",
              "resolved_entity_name": "미상",
              "reason": "주체를 특정할 수 없다."
            }
          ]
        }
        """
    )
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )
    candidates = [
        _candidate(entity_name="미상", raw_entity_mention="나는", attribute_name="level"),
        _candidate(entity_name="미상", raw_entity_mention="그녀는", attribute_name="items.검"),
        _candidate(entity_name="미상", raw_entity_mention="주인공", attribute_name="status.각성"),
        _candidate(entity_name="비요른", raw_entity_mention="비요른", attribute_name="status.부상"),
    ]

    result = resolver.resolve_candidates(
        context=_context(),
        candidates=candidates,
        known_characters=[
            KnownCharacter(character_id=BJORN_ID, name="비요른 얀델"),
            KnownCharacter(character_id=AINAR_ID, name="아이나르"),
        ],
    )

    assert llm_client.call_count == 1
    assert "candidate-0" in llm_client.last_user_prompt
    assert "candidate-1" in llm_client.last_user_prompt
    assert "candidate-2" in llm_client.last_user_prompt
    assert "status.부상" not in llm_client.last_user_prompt
    assert [candidate.attribute_name for candidate in result.candidates] == ["level", "status.부상"]
    assert result.candidates[0].entity_name == "비요른 얀델"
    assert result.candidates[1].entity_name == "비요른"
    assert result.fallback_call_count == 1
    assert result.fallback_resolved_count == 1
    assert result.fallback_discarded_count == 2


class FakeSubjectResolutionClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.call_count = 0
        self.last_system_prompt = ""
        self.last_user_prompt = ""

    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
    ) -> LlmTextResponse:
        self.call_count += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return LlmTextResponse(text=self.response_text)


def _context() -> SubjectResolutionChunkContext:
    return SubjectResolutionChunkContext(
        previous_chunk_text="비요른 얀델은 던전 입구에서 도끼를 점검했다.",
        current_chunk_text="나는 더 이상 물러설 수 없었다. 그녀는 검을 뽑았다.",
        next_chunk_text="비요른은 괴물의 팔을 피했고, 아이나르는 뒤를 엄호했다.",
    )


def _candidate(
    entity_name: str,
    raw_entity_mention: str | None,
    attribute_name: str = "level",
) -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        entity_type="CHARACTER",
        entity_name=entity_name,
        raw_entity_mention=raw_entity_mention,
        attribute_name=attribute_name,
        attribute_value="1",
        value_type="NUMBER",
        value_json={"value": 1},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote="나는 더 이상 물러설 수 없었다.",
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=0.9,
    )


def _prompt_path(tmp_path: Path) -> Path:
    prompt_path = tmp_path / "subject_prompt.md"
    prompt_path.write_text("주체만 해소하고 JSON만 반환하세요.", encoding="utf-8")
    return prompt_path
