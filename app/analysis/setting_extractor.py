import json
import logging
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

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


# CharacterSettingExtractor가 기대하는 LLM client 규격
class TextGenerationClient(Protocol):
    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
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
    ) -> CharacterSettingExtractionResult:
        system_prompt = self._load_system_prompt()
        user_prompt = self._build_user_prompt(
            source_chunk_id=source_chunk_id,
            chunk_text=chunk_text,
            episode_no=episode_no,
            episode_title=episode_title,
        )

        # LLM 응답은 JSON 형식을 항상 지키지 않을 수 있으므로 파싱/검증 실패만 재시도
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                # 예외가 없다면 정상적으로 return 
                return self._extract_once(system_prompt, user_prompt)
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

    def _extract_once(self, system_prompt: str, user_prompt: str) -> CharacterSettingExtractionResult:
        # 시스템 프롬프트 + 사용자 프롬프트를 조합하여 LLM에 요청
        response = self.llm_client.create_text_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self.model,
        )
        # LLM이 준 텍스트를 JSON으로 파싱한 뒤, 우리 내부 schema에 맞는지 검증한다.
        return CharacterSettingExtractionResult.model_validate(_parse_json_object(response.text))

    def _load_system_prompt(self) -> str:
        return self.prompt_path.read_text(encoding="utf-8")

    def _build_user_prompt(
        self,
        source_chunk_id: UUID,
        chunk_text: str,
        episode_no: int | None,
        episode_title: str | None,
    ) -> str:
        # source_chunk_id는 이후 setting_candidates.source_chunk_id로 이어질 근거 식별값이다.
        metadata = {
            "source_chunk_id": str(source_chunk_id),
            "episode_no": episode_no,
            "episode_title": episode_title,
        }
        return (
            "다음 회차 청크에서 캐릭터 설정 후보를 추출하세요.\n\n"
            f"metadata:\n{json.dumps(metadata, ensure_ascii=False)}\n\n" # Python dict를 JSON 문자열로 바꿈
            f"chunk_text:\n{chunk_text}"
        )


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
