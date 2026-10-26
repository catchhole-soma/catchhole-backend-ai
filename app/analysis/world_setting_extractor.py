import json
import logging
from pathlib import Path

from app.analysis.book_world_claims import BOOK_WORLD_CLAIM_INSTRUCTIONS
from app.analysis.json_response import request_validated_model
from app.analysis.ordered_context import (
    SETTING_BOOK_INSTRUCTIONS,
    BoundedOrderedClient,
    ordered_reference_data,
    ordered_state_instructions,
)
from app.analysis.schemas import _to_openai_strict_json_schema
from app.analysis.semantic_current_context import (
    SEMANTIC_CURRENT_EXTRACTION_INSTRUCTIONS,
    SemanticCurrentInput,
)
from app.analysis.setting_book_evidence import (
    book_evidence_retry_prompt,
    book_layout_data,
    ground_setting_book_evidence,
)
from app.analysis.world_setting_schemas import (
    BookWorldSettingExtractionResult,
    WorldSettingExtractionResult,
)
from app.core.config import get_settings
from app.domain.enums import AnalysisSourceKind
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import LlmResponseSchema, TextGenerationClient
from app.schemas.analysis_context import WorkerAnalysisContext
from app.schemas.worker import WorkerAnalysisSettingBookPayload

DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "world_setting_extraction.md"
)
WORLD_SETTING_EXTRACTION_CACHE_KEY = "world-setting-extraction:v6"


def _book_world_response_schema() -> dict:
    schema = _to_openai_strict_json_schema(BookWorldSettingExtractionResult.model_json_schema())

    def close_objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                close_objects(value)
        elif isinstance(node, list):
            for value in node:
                close_objects(value)

    close_objects(schema)
    return schema


BOOK_WORLD_EXTRACTION_SCHEMA = LlmResponseSchema(
    name="book_world_setting_extraction",
    schema=_book_world_response_schema(),
)
logger = logging.getLogger(__name__)


class WorldSettingExtractor:
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
        self.llm_client = llm_client or OpenAIResponsesClient.from_settings()
        self.prompt_path = prompt_path
        self.model = model or settings.effective_llm_extraction_model
        self.max_attempts = (
            settings.llm_extraction_max_attempts if max_attempts is None else max_attempts
        )
        self.max_output_tokens = (
            settings.llm_world_setting_extraction_max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        self.truncation_retry_max_output_tokens = (
            settings.llm_world_setting_extraction_retry_max_output_tokens
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
        chunk_text: str,
        episode_no: int | None = None,
        episode_title: str | None = None,
        analysis_context: WorkerAnalysisContext | None = None,
        setting_book: WorkerAnalysisSettingBookPayload | None = None,
        semantic_current_context: SemanticCurrentInput | None = None,
    ) -> WorldSettingExtractionResult:
        system_prompt = self.prompt_path.read_text(encoding="utf-8")
        metadata = json.dumps(
            (setting_book.prompt_data() if setting_book is not None
             else {"episode_no": episode_no, "episode_title": episode_title}),
            ensure_ascii=False,
            sort_keys=True,
        )
        user_prompt = f"metadata:\n{metadata}\n\nchunk_text:\n{chunk_text}"
        if setting_book is not None and analysis_context is None:
            system_prompt += "\n\n" + SETTING_BOOK_INSTRUCTIONS
        if analysis_context is not None and setting_book is not None:
            system_prompt += "\n\n" + ordered_state_instructions(
                AnalysisSourceKind.SETTING_BOOK if setting_book is not None else None
            )
            user_prompt += "\n\nunresolved_references:\n" + json.dumps(
                ordered_reference_data(analysis_context.unresolved_references), ensure_ascii=False,
            )
        if setting_book is not None:
            system_prompt += "\n\n" + BOOK_WORLD_CLAIM_INSTRUCTIONS
            user_prompt = json.dumps({
                "metadata": setting_book.prompt_data(), "chunk_text": chunk_text,
                "source_layout": book_layout_data(chunk_text),
                "unresolved_references": ordered_reference_data(analysis_context.unresolved_references) if analysis_context else [],
            }, ensure_ascii=False)

        if semantic_current_context is not None and setting_book is not None:
            system_prompt += "\n\n" + SEMANTIC_CURRENT_EXTRACTION_INSTRUCTIONS
            if setting_book is not None:
                payload = json.loads(user_prompt)
                payload["semantic_current_state"] = json.loads(semantic_current_context.world_prompt_json())
                user_prompt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            else:
                user_prompt += "\n\nsemantic_current_state:\n" + semantic_current_context.world_prompt_json()

        def validate_book_result(result):
            grounded = ground_setting_book_evidence(result, chunk_text, world=True)
            result.candidates = grounded.candidates

        return await request_validated_model(
            client=(BoundedOrderedClient(self.llm_client)
                    if analysis_context is not None or semantic_current_context is not None else self.llm_client),
            response_model=BookWorldSettingExtractionResult if setting_book is not None else WorldSettingExtractionResult,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            max_attempts=self.max_attempts,
            prompt_cache_key=(WORLD_SETTING_EXTRACTION_CACHE_KEY
                              + (":ordered-references-v1" if analysis_context and setting_book else "")
                              + (":setting-book-grounded-claims-v2" if setting_book else "")
                              + (":semantic-current-v1" if semantic_current_context is not None and setting_book else "")
                              + (":episode-lean-v1" if setting_book is None else "")),
            operation_name="World-setting extraction",
            logger=logger,
            truncation_retry_max_output_tokens=self.truncation_retry_max_output_tokens,
            **({"response_schema": BOOK_WORLD_EXTRACTION_SCHEMA,
                "validate_model": validate_book_result,
                "retry_user_prompt_builder": book_evidence_retry_prompt} if setting_book is not None else {}),
        )
