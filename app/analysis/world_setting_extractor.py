import json
import logging
from pathlib import Path

from app.analysis.json_response import request_validated_model
from app.analysis.world_setting_schemas import WorldSettingExtractionResult
from app.core.config import get_settings
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import TextGenerationClient

DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "world_setting_extraction.md"
)
WORLD_SETTING_EXTRACTION_CACHE_KEY = "world-setting-extraction:v2"
logger = logging.getLogger(__name__)


class WorldSettingExtractor:
    def __init__(
        self,
        llm_client: TextGenerationClient | None = None,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
        model: str | None = None,
        max_attempts: int | None = None,
    ) -> None:
        settings = get_settings()
        self.llm_client = llm_client or OpenAIResponsesClient.from_settings()
        self.prompt_path = prompt_path
        self.model = model or settings.effective_llm_extraction_model
        self.max_attempts = (
            settings.llm_extraction_max_attempts if max_attempts is None else max_attempts
        )
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")

    def extract_from_chunk(
        self,
        chunk_text: str,
        episode_no: int | None = None,
        episode_title: str | None = None,
    ) -> WorldSettingExtractionResult:
        system_prompt = self.prompt_path.read_text(encoding="utf-8")
        metadata = json.dumps(
            {"episode_no": episode_no, "episode_title": episode_title},
            ensure_ascii=False,
            sort_keys=True,
        )
        user_prompt = f"metadata:\n{metadata}\n\nchunk_text:\n{chunk_text}"

        return request_validated_model(
            client=self.llm_client,
            response_model=WorldSettingExtractionResult,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self.model,
            max_output_tokens=3000,
            max_attempts=self.max_attempts,
            prompt_cache_key=WORLD_SETTING_EXTRACTION_CACHE_KEY,
            operation_name="World-setting extraction",
            logger=logger,
        )
