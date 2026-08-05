from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.exceptions import LlmExtractionError
from app.analysis.schemas import CharacterSettingExtractionResult
from app.core.config import get_settings
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.responses import LlmTextResponse

# 기본 prompt 파일 위치, analysis 패키지 기준으로 app/llm/prompts 아래 파일을 찾는다.
DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "character_setting_extraction.md"
)
# 이 파일 전용 로그 객체를 만든다 
logger = logging.getLogger(__name__)
SETTING_EXTRACTION_CACHE_KEY_VERSION = "setting-extraction:v3"


@dataclass(frozen=True)
class CharacterSettingSchemaHint:
    schema_key: str
    display_name: str
    attribute_pattern: str | None
    aliases: tuple[str, ...]
    value_type: str


# CharacterSettingExtractor가 기대하는 LLM client 규격
class TextGenerationClient(Protocol):
    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        pass


# 청크 하나를 캐릭터 설정 후보 추출 결과로 바꾸는 분석 유스케이스
class CharacterSettingExtractor:
    def __init__(
        self,
        llm_client: TextGenerationClient | None = None,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
        model: str | None = None,
        max_attempts: int | None = None,
    ) -> None:
        # 실제 실행에서는 OpenAI client를 쓰고, 테스트에서는 fake client를 주입
        self.llm_client = llm_client or OpenAIResponsesClient.from_settings()
        self.prompt_path = prompt_path
        # 특정 추출 작업에서만 모델을 바꾸고 싶을 때 사용한다, 없으면 LLM client 기본 모델을 쓴다
        self.model = model
        self.max_attempts = (
            get_settings().llm_extraction_max_attempts if max_attempts is None else max_attempts
        )
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")

    def extract_from_chunk(
        self,
        source_chunk_id: UUID,
        chunk_text: str,
        episode_no: int | None = None,
        episode_title: str | None = None,
        schema_hints: tuple[CharacterSettingSchemaHint, ...] = (),
        known_characters: tuple[KnownCharacter, ...] = (),
    ) -> CharacterSettingExtractionResult:
        # Schema가 없으면 모든 설정 후보를 제외하라는 prompt가 만들어지므로,
        # 비용이 발생하는 LLM 호출 전에 잘못된 직접 호출을 차단한다.
        if not schema_hints:
            raise ValueError("schema_hints must include at least one character setting schema.")

        system_prompt = self._load_system_prompt()
        schema_summary_json = _serialize_schema_hints(schema_hints)
        user_prompt = self._build_user_prompt(
            chunk_text=chunk_text,
            episode_no=episode_no,
            episode_title=episode_title,
            schema_summary_json=schema_summary_json,
            known_characters=known_characters,
        )
        prompt_cache_key = _build_schema_cache_key(schema_summary_json)

        # LLM 응답은 JSON 형식을 항상 지키지 않을 수 있으므로 파싱/검증 실패만 재시도
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                # 예외가 없다면 정상적으로 return 
                return self._extract_once(
                    system_prompt,
                    user_prompt,
                    source_chunk_id,
                    prompt_cache_key,
                )
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    # 최대 반복횟수가 되면 for문 종료 후 아래의 LlmExtractionError을 만든다.
                    break
                logger.warning(
                    "LLM extraction response validation failed. retrying attempt=%s/%s error=%s",
                    attempt,
                    self.max_attempts,
                    _error_message(exc),
                )

        raise LlmExtractionError(
            "LLM extraction failed after "
            f"{self.max_attempts} attempts: {_error_message(last_error)}"
        ) from last_error

    def _extract_once(
        self,
        system_prompt: str,
        user_prompt: str,
        source_chunk_id: UUID,
        prompt_cache_key: str,
    ) -> CharacterSettingExtractionResult:
        # 시스템 프롬프트 + 사용자 프롬프트를 조합하여 LLM에 요청
        response = self.llm_client.create_text_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self.model,
            max_output_tokens=4000,
            prompt_cache_key=prompt_cache_key,
        )
        # source_chunk_id는 Worker가 이미 알고 있는 식별자이므로 LLM 응답을 신뢰하지 않고
        # 현재 입력 chunk ID로 강제한 뒤 내부 schema를 검증한다.
        payload = _parse_json_object(response.text)
        candidates = payload.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    candidate["source_chunk_id"] = str(source_chunk_id)
        return CharacterSettingExtractionResult.model_validate(payload)

    def _load_system_prompt(self) -> str:
        return self.prompt_path.read_text(encoding="utf-8")

    def _build_user_prompt(
        self,
        chunk_text: str,
        episode_no: int | None,
        episode_title: str | None,
        schema_summary_json: str,
        known_characters: tuple[KnownCharacter, ...],
    ) -> str:
        metadata = {
            "episode_no": episode_no,
            "episode_title": episode_title,
        }
        # 고정 규칙과 canonical schema를 앞에 두고 회차별 metadata·원문은 뒤에 둔다.
        return (
            "다음 회차 청크에서 캐릭터 설정 후보를 추출하세요.\n\n"
            "character_setting_schema_rules:\n"
            "- attributePattern이 null인 schema의 schemaKey, displayName 또는 aliases와 명확히 "
            "대응하면 attribute_name에는 canonical schemaKey를, value_type에는 valueType을 "
            "사용하세요.\n"
            "- attributePattern이 있는 동적 설정은 schemaKey가 아니라 pattern의 *를 구체 "
            "명칭으로 바꾼 attribute_name과 schema의 valueType을 사용하세요.\n"
            "- character_setting_schemas의 schemaKey, displayName, aliases 또는 "
            "attributePattern과 대응하지 "
            "않는 설정은 가까운 schema로 추측하지 말고 후보에서 제외하세요.\n\n"
            "character_setting_schemas:\n"
            f"{schema_summary_json}\n\n"
            "known_character_names:\n"
            f"{_serialize_known_character_names(known_characters)}\n\n"
            f"metadata:\n{json.dumps(metadata, ensure_ascii=False, sort_keys=True)}\n\n"
            f"chunk_text:\n{chunk_text}"
        )


def _serialize_schema_hints(
    schema_hints: tuple[CharacterSettingSchemaHint, ...],
) -> str:
    # DB 조회 순서와 aliases 순서가 달라도 논리적으로 같은 schema는 같은 prefix를 만든다.
    schema_summary = [
        {
            "schemaKey": hint.schema_key,
            "displayName": hint.display_name,
            "attributePattern": hint.attribute_pattern,
            "aliases": sorted(hint.aliases),
            "valueType": hint.value_type,
        }
        for hint in sorted(
            schema_hints,
            key=lambda hint: (
                hint.schema_key,
                hint.display_name,
                hint.attribute_pattern is not None,
                hint.attribute_pattern or "",
                tuple(sorted(hint.aliases)),
                hint.value_type,
            ),
        )
    ]
    return json.dumps(
        schema_summary,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _serialize_known_character_names(known_characters: tuple[KnownCharacter, ...]) -> str:
    return json.dumps(
        [character.name for character in known_characters],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _build_schema_cache_key(schema_summary_json: str) -> str:
    schema_fingerprint = hashlib.sha256(schema_summary_json.encode("utf-8")).hexdigest()[:16]
    return f"{SETTING_EXTRACTION_CACHE_KEY_VERSION}:{schema_fingerprint}"


def _parse_json_object(text: str) -> dict:
    content = text.strip() # 앞뒤 공백 제거
    # LLM이 ```json 코드블록으로 감싸서 답하는 경우를 대비해 바깥 fence를 제거
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    # 앞뒤에 설명 문장이 섞여도 첫 JSON 객체 부분만 잘라서 파싱한다.
    if not content.startswith("{"):
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end >= start:
            content = content[start : end + 1]

    return json.loads(content)


def _error_message(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    message = str(exc) or exc.__class__.__name__
    return message[:500]
