from collections.abc import Iterable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError

from app.analysis.character_name_resolver import (
    AMBIGUOUS_MENTIONS,
    PLACEHOLDER_ENTITY_NAMES,
    KnownCharacter,
    normalize_character_name,
)
from app.analysis.exceptions import LlmExtractionError
from app.analysis.schemas import ExtractedSettingCandidate
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.responses import LlmTextResponse

DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "character_subject_resolution.md"
)


class TextGenerationClient(Protocol):
    # 테스트에서 OpenAI 클라이언트를 대체 주입하기 위한 최소 규격.
    # system/user prompt를 받아 텍스트 응답 하나를 생성한다.
    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
    ) -> LlmTextResponse:
        pass


@dataclass(frozen=True)
class SubjectResolutionChunkContext:
    # 후보가 실제 추출된 current chunk와, 주체 판단을 도울 앞뒤 문맥.
    # previous/next chunk는 판단 참고용이며 evidence/offset 기준은 current chunk에 남는다.
    previous_chunk_text: str | None
    current_chunk_text: str
    next_chunk_text: str | None


@dataclass(frozen=True)
class SubjectResolutionResult:
    # 저장 흐름으로 넘길 최종 후보들과 Worker summary에 남길 fallback 처리 개수.
    candidates: list[ExtractedSettingCandidate]
    fallback_call_count: int = 0
    fallback_resolved_count: int = 0
    fallback_discarded_count: int = 0


class SubjectResolutionItem(BaseModel):
    # LLM이 candidate_id 하나에 대해 반환해야 하는 주체 해소 결과.
    # match status와 matched_character_id는 기존 character_name_resolver가 나중에 계산한다.
    candidate_id: str = Field(min_length=1, max_length=50)
    resolved_entity_name: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=500)


class SubjectResolutionResponse(BaseModel):
    # LLM 응답 최상위 JSON 객체. 후보별 resolution 목록만 받는다.
    resolutions: list[SubjectResolutionItem] = Field(default_factory=list)


class CharacterSubjectResolver:
    # "나", "그녀", "미상"처럼 현재 chunk만으로 주체가 풀리지 않은 후보만
    # LLM에 다시 보내고, 설정 후보 자체를 재추출하지는 않는다.
    def __init__(
        self,
        llm_client: TextGenerationClient | None = None,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
        model: str | None = None,
    ) -> None:
        # 운영에서는 OpenAI client를 기본 사용하고, 테스트에서는 fake client를 주입한다.
        self.llm_client = llm_client or OpenAIResponsesClient.from_settings()
        self.prompt_path = prompt_path
        self.model = model

    def resolve_candidates(
        self,
        context: SubjectResolutionChunkContext,
        candidates: list[ExtractedSettingCandidate],
        known_characters: list[KnownCharacter],
    ) -> SubjectResolutionResult:
        # 명확한 캐릭터명 후보는 기존 name resolver로 충분하므로 fallback 대상에서 제외한다.
        fallback_targets = _build_fallback_targets(candidates)
        if not fallback_targets:
            return SubjectResolutionResult(candidates=candidates)

        # 같은 current chunk에서 나온 fallback 대상들은 한 번의 LLM 호출로 같이 판단한다.
        resolution_response = self._request_resolution(
            context=context,
            targets=fallback_targets,
            known_characters=known_characters,
        )
        # LLM 응답을 candidate_id 기준 dict로 바꿔 원래 후보와 다시 연결한다.
        resolution_by_candidate_id = {
            resolution_item.candidate_id: resolution_item
            for resolution_item in resolution_response.resolutions
        }

        resolved_candidate_by_id: dict[str, ExtractedSettingCandidate] = {}
        resolved_count = 0
        discarded_count = 0

        for fallback_target in fallback_targets:
            # 프롬프트 계약상 모든 candidate_id가 돌아와야 한다.
            # 그래도 누락되면 해소 실패로 보고 미상 후보를 저장하지 않는다.
            resolution = resolution_by_candidate_id.get(fallback_target.candidate_id)
            if resolution is None:
                discarded_count += 1
                continue

            resolved_entity_name = _usable_resolved_entity_name(resolution.resolved_entity_name)
            if resolved_entity_name:
                # entity_name만 치환하고 attribute/evidence/source_chunk 정보는 기존 후보를 유지한다.
                resolved_candidate_by_id[fallback_target.candidate_id] = (
                    fallback_target.candidate.model_copy(
                        update={"entity_name": resolved_entity_name}
                    )
                )
                resolved_count += 1
            else:
                discarded_count += 1

        fallback_target_ids = {target.candidate_id for target in fallback_targets}
        # 최종적으로 저장 흐름에 넘길 후보 목록(기존 순서도 보장한다.)
        final_candidates: list[ExtractedSettingCandidate] = []
        # 원래 candidates 리스트에 다시 같은 임시표를 붙여서 LLM 응답과 대조한다.
        for indexed_candidate in _index_candidates(candidates):
            # fallback 대상이 아니었다면 그대로 넣는다.
            if indexed_candidate.candidate_id not in fallback_target_ids:
                final_candidates.append(indexed_candidate.candidate)
                continue

            # fallback 대상이라면 바뀐 entity_name을 가진 수정본으로 치환한다.
            resolved_candidate = resolved_candidate_by_id.get(indexed_candidate.candidate_id)
            # fallback에 성공해서 entity_name이 치환된 후보가 있다면 넣는다.
            if resolved_candidate is not None:
                final_candidates.append(resolved_candidate)

        return SubjectResolutionResult(
            candidates=final_candidates,
            fallback_call_count=1,
            fallback_resolved_count=resolved_count,
            fallback_discarded_count=discarded_count,
        )

    def _request_resolution(
        self,
        context: SubjectResolutionChunkContext,
        targets: list["_IndexedCandidate"],
        known_characters: list[KnownCharacter],
    ) -> SubjectResolutionResponse:
        # prompt 파일은 resolver 호출 시점에 읽어 최신 프롬프트 내용을 사용한다.
        system_prompt = self.prompt_path.read_text(encoding="utf-8")
        user_prompt = _build_user_prompt(
            context=context,
            targets=targets,
            known_characters=known_characters,
        )

        try:
            # fallback은 같은 문맥으로 한 번만 판단한다.(같은 문맥으로 2번 이상 판단할 이유가 없다고 생각)
            return self._request_once(system_prompt, user_prompt)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LlmExtractionError(
                f"LLM subject resolution failed: {_error_message(exc)}"
            ) from exc

    def _request_once(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> SubjectResolutionResponse:
        # subject fallback은 후보 재추출이 아니라 주체 해소만 하므로 출력 토큰을 작게 제한한다.
        response = self.llm_client.create_text_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self.model,
            max_output_tokens=1000,
        )
        return SubjectResolutionResponse.model_validate(_parse_json_object(response.text))


@dataclass(frozen=True)
class _IndexedCandidate:
    # LLM 응답과 원래 후보를 다시 연결하기 위한 내부 식별자.
    candidate_id: str
    candidate: ExtractedSettingCandidate


def _build_fallback_targets(
    candidates: list[ExtractedSettingCandidate],
) -> list[_IndexedCandidate]:
    # 전체 후보 중 subject fallback이 필요한 후보만 고른다.
    return [
        indexed_candidate
        for indexed_candidate in _index_candidates(candidates)
        if _is_fallback_target(indexed_candidate.candidate)
    ]


def _index_candidates(candidates: Iterable[ExtractedSettingCandidate]) -> list[_IndexedCandidate]:
    # 원본 후보에는 임시 ID가 없으므로 chunk 내부 순서를 기반으로 안정적인 candidate_id를 만든다.
    return [
        _IndexedCandidate(candidate_id=f"candidate-{index}", candidate=candidate)
        for index, candidate in enumerate(candidates)
    ]


def _is_fallback_target(candidate: ExtractedSettingCandidate) -> bool:
    # raw/entity 표현은 같은 정규화 규칙으로 맞춘 뒤 지칭어/placeholder 여부를 판단한다.
    normalized_raw_mention = normalize_character_name(candidate.raw_entity_mention)
    normalized_entity_name = normalize_character_name(candidate.entity_name)

    # raw가 지칭어가 아니면 앞뒤 chunk를 보지 않아도 기존 매칭 로직으로 처리할 수 있다.
    if normalized_raw_mention not in AMBIGUOUS_MENTIONS:
        return False

    # 지칭어 raw에 entity_name까지 미상/지칭어라면 현재 chunk만으로 주체가 풀리지 않은 상태다.
    return (
        not normalized_entity_name
        or normalized_entity_name in PLACEHOLDER_ENTITY_NAMES
        or normalized_entity_name in AMBIGUOUS_MENTIONS
    )


def _usable_resolved_entity_name(resolved_entity_name: str | None) -> str | None:
    # null/빈 문자열은 해소 실패로 본다.
    if resolved_entity_name is None:
        return None

    stripped_name = resolved_entity_name.strip()
    normalized_name = normalize_character_name(stripped_name)
    if not normalized_name:
        return None

    # LLM이 "미상", "그녀", "나"처럼 주체가 다시 불분명한 값을 반환하면
    # 실제 해소가 아니므로 저장 후보에서 제외한다.
    if normalized_name in PLACEHOLDER_ENTITY_NAMES or normalized_name in AMBIGUOUS_MENTIONS:
        return None

    return stripped_name


def _build_user_prompt(
    context: SubjectResolutionChunkContext,
    targets: list[_IndexedCandidate],
    known_characters: list[KnownCharacter],
) -> str:
    # LLM이 후보를 새로 만들지 못하도록, 해소 대상 candidate_id와 필요한 문맥만 전달한다.
    payload = {
        "known_characters": [
            {
                "character_id": str(character.character_id),
                "name": character.name,
            }
            for character in known_characters
        ],
        "context": {
            "previous_chunk": context.previous_chunk_text,
            "current_chunk": context.current_chunk_text,
            "next_chunk": context.next_chunk_text,
        },
        "candidates": [
            {
                "candidate_id": target.candidate_id,
                "raw_entity_mention": target.candidate.raw_entity_mention,
                "entity_name": target.candidate.entity_name,
                "attribute_name": target.candidate.attribute_name,
                "attribute_value": target.candidate.attribute_value,
                "evidence_quotes": [
                    evidence.quote
                    for evidence in target.candidate.evidence_spans
                ],
            }
            for target in targets
        ],
    }
    return (
        "다음 JSON 입력을 보고 fallback 대상 후보들의 주체만 해소하세요.\n"
        "전체 설정 후보를 다시 추출하지 말고 candidates에 있는 candidate_id만 반환하세요.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_json_object(text: str) -> dict:
    # 모델이 실수로 코드블록이나 앞뒤 설명을 붙여도 JSON 객체만 최대한 회수한다.
    content = text.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    if not content.startswith("{"):
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end >= start:
            content = content[start : end + 1]

    return json.loads(content)


def _error_message(exc: Exception | None) -> str:
    # 최종 예외 메시지가 너무 길어지지 않도록 짧게 자른다.
    if exc is None:
        return "unknown error"
    message = str(exc) or exc.__class__.__name__
    return message[:500]
