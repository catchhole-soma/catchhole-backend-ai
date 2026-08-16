import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.exceptions import LlmExtractionError
from app.analysis.json_response import compact_error_message, parse_json_object
from app.analysis.schemas import CharacterSettingExtractionResult
from app.core.config import get_settings
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import TextGenerationClient

# 기본 prompt 파일 위치, analysis 패키지 기준으로 app/llm/prompts 아래 파일을 찾는다.
DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "character_setting_extraction.md"
)
# 이 파일 전용 로그 객체를 만든다
logger = logging.getLogger(__name__)
SETTING_EXTRACTION_CACHE_KEY_VERSION = "setting-extraction:v6"


@dataclass(frozen=True)
class CharacterSettingSchemaHint:
    schema_key: str
    display_name: str
    attribute_pattern: str | None
    aliases: tuple[str, ...]
    value_type: str


# 청크 하나를 캐릭터 설정 후보 추출 결과로 바꾸는 분석 유스케이스
class CharacterSettingExtractor:
    def __init__(
        self,
        llm_client: TextGenerationClient | None = None,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
        model: str | None = None,
        max_attempts: int | None = None,
        max_output_tokens: int | None = None,
        truncation_retry_max_output_tokens: int | None = None,
    ) -> None:
        settings = get_settings()
        # 실제 실행에서는 OpenAI client를 쓰고, 테스트에서는 fake client를 주입
        self.llm_client = llm_client or OpenAIResponsesClient.from_settings()
        self.prompt_path = prompt_path
        self.model = model or settings.effective_llm_extraction_model
        self.max_attempts = (
            settings.llm_extraction_max_attempts if max_attempts is None else max_attempts
        )
        self.max_output_tokens = (
            settings.llm_setting_extraction_max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        self.truncation_retry_max_output_tokens = (
            settings.llm_setting_extraction_retry_max_output_tokens
            if truncation_retry_max_output_tokens is None
            else truncation_retry_max_output_tokens
        )
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least 1.")
        if self.truncation_retry_max_output_tokens < self.max_output_tokens:
            raise ValueError(
                "truncation_retry_max_output_tokens must be at least max_output_tokens."
            )

    async def extract_from_chunk(
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
        current_max_output_tokens = self.max_output_tokens
        truncation_retry_used = False
        for attempt in range(1, self.max_attempts + 1):
            while True:
                try:
                    # 예외가 없다면 정상적으로 return
                    return await self._extract_once(
                        system_prompt,
                        user_prompt,
                        source_chunk_id,
                        prompt_cache_key,
                        current_max_output_tokens,
                    )
                except LlmOutputTruncatedError as exc:
                    can_expand_once = (
                        not truncation_retry_used
                        and self.truncation_retry_max_output_tokens > current_max_output_tokens
                    )
                    if not can_expand_once:
                        raise
                    truncation_retry_used = True
                    current_max_output_tokens = self.truncation_retry_max_output_tokens
                    logger.warning(
                        "Setting extraction output truncated; increasing cap once. "
                        "attempt=%s/%s max_output_tokens=%s next_max_output_tokens=%s "
                        "output_tokens=%s reason=%s",
                        attempt,
                        self.max_attempts,
                        exc.max_output_tokens,
                        current_max_output_tokens,
                        exc.output_token_count,
                        exc.incomplete_reason,
                    )
                except (json.JSONDecodeError, ValidationError) as exc:
                    last_error = exc
                    if attempt < self.max_attempts:
                        logger.warning(
                            "LLM extraction response validation failed. retrying "
                            "attempt=%s/%s error=%s",
                            attempt,
                            self.max_attempts,
                            compact_error_message(exc),
                        )
                    break

        raise LlmExtractionError(
            "LLM extraction failed after "
            f"{self.max_attempts} attempts: {compact_error_message(last_error)}"
        ) from last_error

    async def _extract_once(
        self,
        system_prompt: str,
        user_prompt: str,
        source_chunk_id: UUID,
        prompt_cache_key: str,
        max_output_tokens: int,
    ) -> CharacterSettingExtractionResult:
        # 시스템 프롬프트 + 사용자 프롬프트를 조합하여 LLM에 요청
        response = await self.llm_client.create_text_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self.model,
            max_output_tokens=max_output_tokens,
            prompt_cache_key=prompt_cache_key,
        )
        # source_chunk_id는 Worker가 이미 알고 있는 식별자이므로 LLM 응답을 신뢰하지 않고
        # 현재 입력 chunk ID로 강제한 뒤 내부 schema를 검증한다.
        payload = parse_json_object(response.text)
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
