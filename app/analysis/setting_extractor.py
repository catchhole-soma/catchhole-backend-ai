import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.exceptions import LlmExtractionError
from app.analysis.json_response import parse_json_object
from app.analysis.schemas import (
    CharacterSettingExtractionResult,
    CharacterSettingProviderResponse,
    character_setting_provider_json_schema,
)
from app.core.config import get_settings
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import LlmResponseSchema, TextGenerationClient

# 기본 prompt 파일 위치, analysis 패키지 기준으로 app/llm/prompts 아래 파일을 찾는다.
DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "character_setting_extraction.md"
)
# 이 파일 전용 로그 객체를 만든다
logger = logging.getLogger(__name__)
SETTING_EXTRACTION_CACHE_KEY_VERSION = "setting-extraction:v10"
SETTING_EXTRACTION_RESPONSE_SCHEMA = LlmResponseSchema(
    name="character_setting_extraction",
    schema=character_setting_provider_json_schema(),
    strict=True,
)
_SAFE_VALIDATION_PATH_FIELDS = frozenset(
    {
        "candidates",
        "entity_type",
        "entity_name",
        "raw_entity_mention",
        "evidence_spans",
        "quote",
        "start_offset",
        "end_offset",
        "confidence",
        "candidate_kind",
        "attribute_name",
        "attribute_value",
        "value_type",
        "value_json",
        "extra_json",
        "value",
        "active",
    }
)
_UNEXPECTED_VALIDATION_PATH_FIELD = "unexpected_field"


@dataclass(frozen=True)
class CharacterSettingSchemaHint:
    schema_key: str
    display_name: str
    attribute_pattern: str | None
    aliases: tuple[str, ...]
    value_type: str
    # Worker claim에는 아직 포함되지 않지만, 평가 fixture는 Java DB schema의
    # 명시적인 CharacterFactType을 보존할 수 있다. Prompt 직렬화에는 넣지 않아
    # 운영 extractor 입력과 cache key는 그대로 유지한다.
    canonical_fact_type: str | None = None


@dataclass(frozen=True)
class _SafeValidationFeedback:
    reason_code: str
    field_locs: tuple[str, ...]

    def prompt_json(self) -> str:
        return json.dumps(
            {
                "reasonCode": self.reason_code,
                "fieldLocs": list(self.field_locs),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


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
        analysis_job_id: UUID | None = None,
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

        # Provider schema와 저장 경계 schema 검증 실패만 안전한 사유와 함께 재시도한다.
        last_feedback: _SafeValidationFeedback | None = None
        repeated_reason_counts: Counter[str] = Counter()
        current_user_prompt = user_prompt
        current_max_output_tokens = self.max_output_tokens
        truncation_retry_used = False
        for attempt in range(1, self.max_attempts + 1):
            while True:
                try:
                    # 예외가 없다면 정상적으로 return
                    return await self._extract_once(
                        system_prompt,
                        current_user_prompt,
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
                except (json.JSONDecodeError, TypeError, ValidationError) as exc:
                    last_feedback = _safe_validation_feedback(exc)
                    repeated_reason_counts[last_feedback.reason_code] += 1
                    logger.warning(
                        "LLM extraction response validation failed. "
                        "attempt=%s/%s analysis_job_id=%s source_chunk_id=%s "
                        "reason_code=%s field_locs=%s repeated_reason_count=%s will_retry=%s",
                        attempt,
                        self.max_attempts,
                        analysis_job_id,
                        source_chunk_id,
                        last_feedback.reason_code,
                        ",".join(last_feedback.field_locs),
                        repeated_reason_counts[last_feedback.reason_code],
                        attempt < self.max_attempts,
                    )
                    if attempt < self.max_attempts:
                        current_user_prompt = _build_retry_user_prompt(
                            user_prompt,
                            last_feedback,
                        )
                    break

        final_feedback = last_feedback or _SafeValidationFeedback(
            reason_code="RESPONSE_SCHEMA_INVALID",
            field_locs=("response",),
        )
        raise LlmExtractionError(
            "LLM extraction failed after "
            f"{self.max_attempts} attempts: reasonCode={final_feedback.reason_code} "
            f"fieldLocs={','.join(final_feedback.field_locs)}"
        ) from None

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
            response_schema=SETTING_EXTRACTION_RESPONSE_SCHEMA,
        )
        # source_chunk_id는 Provider schema에 넣지 않고 Worker 입력으로만 결합한다.
        payload = parse_json_object(response.text)
        candidates = payload.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    candidate.pop("source_chunk_id", None)
        provider_result = CharacterSettingProviderResponse.model_validate(payload)
        return provider_result.to_extraction_result(source_chunk_id)

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
            "active_character_statuses:\n"
            f"{_serialize_active_character_statuses(known_characters)}\n\n"
            f"metadata:\n{json.dumps(metadata, ensure_ascii=False, sort_keys=True)}\n\n"
            f"chunk_text:\n{chunk_text}"
        )


def _build_retry_user_prompt(
    original_user_prompt: str,
    feedback: _SafeValidationFeedback,
) -> str:
    # 이전 Provider 응답은 절대 재주입하지 않고 최초 입력에 안전한 수정 지시만 붙인다.
    return (
        f"{original_user_prompt}\n\n"
        "previous_response_correction:\n"
        f"{feedback.prompt_json()}\n"
        "위 reasonCode와 fieldLocs만 고쳐 전체 응답을 다시 생성하세요."
    )


def _safe_validation_feedback(exc: Exception) -> _SafeValidationFeedback:
    if isinstance(exc, json.JSONDecodeError):
        return _SafeValidationFeedback("RESPONSE_JSON_INVALID", ("response",))
    if not isinstance(exc, ValidationError):
        return _SafeValidationFeedback("RESPONSE_SCHEMA_INVALID", ("response",))

    errors = exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    reason_code = _validation_reason_code(errors)
    field_locs = tuple(
        dict.fromkeys(_safe_field_loc(error.get("loc"), reason_code) for error in errors)
    )
    return _SafeValidationFeedback(
        reason_code=reason_code,
        field_locs=field_locs or ("response",),
    )


def _validation_reason_code(errors: list[dict]) -> str:
    locations = [tuple(error.get("loc") or ()) for error in errors]
    error_types = {error.get("type") for error in errors}

    if any("CHARACTER_DISCOVERY" in location for location in locations):
        return "DISCOVERY_SETTING_FIELD_FORBIDDEN"

    setting_locations = [location for location in locations if "SETTING" in location]
    if setting_locations:
        if (
            any(
                "attribute_name" in location
                or "value_type" in location
                or (location and location[-1] == "value_json")
                for location in setting_locations
            )
            or "union_tag_not_found" in error_types
        ):
            return "SETTING_REQUIRED_FIELD_MISSING"
        if any("NUMBER" in location for location in setting_locations):
            return "NUMBER_TYPED_VALUE_INVALID"
        if any("BOOLEAN" in location for location in setting_locations):
            return "BOOLEAN_TYPED_VALUE_INVALID"

    if "number_typed_value_invalid" in error_types:
        return "NUMBER_TYPED_VALUE_INVALID"
    if "boolean_typed_value_invalid" in error_types:
        return "BOOLEAN_TYPED_VALUE_INVALID"
    if "status_active_value_invalid" in error_types:
        return "STATUS_ACTIVE_VALUE_INVALID"
    if "discovery_setting_field_forbidden" in error_types:
        return "DISCOVERY_SETTING_FIELD_FORBIDDEN"
    if "setting_required_field_missing" in error_types:
        return "SETTING_REQUIRED_FIELD_MISSING"
    return "RESPONSE_SCHEMA_INVALID"


def _safe_field_loc(raw_loc: object, reason_code: str) -> str:
    location = tuple(raw_loc) if isinstance(raw_loc, (tuple, list)) else ()
    ignored_segments = {
        "SETTING",
        "CHARACTER_DISCOVERY",
        "STRING",
        "NUMBER",
        "BOOLEAN",
        "JSON",
        "UNKNOWN",
        "int",
        "float",
        "str",
        "bool",
        "nullable",
    }
    safe_segments: list[str] = []
    for segment in location:
        if segment in ignored_segments:
            continue
        if isinstance(segment, int):
            safe_segments.append(str(segment))
        elif isinstance(segment, str) and segment in _SAFE_VALIDATION_PATH_FIELDS:
            safe_segments.append(segment)
        else:
            safe_segments.append(_UNEXPECTED_VALIDATION_PATH_FIELD)
    if (
        reason_code == "SETTING_REQUIRED_FIELD_MISSING"
        and len(safe_segments) == 2
        and safe_segments[0] == "candidates"
        and safe_segments[1].isdigit()
    ):
        safe_segments.append("value_type")
    return ".".join(safe_segments) or "response"


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


def _serialize_active_character_statuses(
    known_characters: tuple[KnownCharacter, ...],
) -> str:
    """LLM에는 캐릭터명·Fact key·표시값만 전달하고 내부 ID/이력은 숨긴다."""

    statuses = [
        {
            "characterName": character.name,
            "factKey": status.fact_key,
            "factValue": status.fact_value,
        }
        for character in known_characters
        for status in character.active_statuses
    ]
    return json.dumps(
        sorted(
            statuses,
            key=lambda item: (
                item["characterName"],
                item["factKey"],
                item["factValue"] or "",
            ),
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _build_schema_cache_key(schema_summary_json: str) -> str:
    schema_fingerprint = hashlib.sha256(schema_summary_json.encode("utf-8")).hexdigest()[:16]
    return f"{SETTING_EXTRACTION_CACHE_KEY_VERSION}:{schema_fingerprint}"
