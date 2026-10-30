"""Independent source-span relationship extraction, isolated from core fact calls."""

import json
from pathlib import Path
from uuid import UUID

from app.analysis.exceptions import LlmExtractionError
from app.analysis.relationship_observations import (
    RELATIONSHIP_OBSERVATION_VERSION,
    RelationshipExtractionResult,
    RelationshipInputTooLargeError,
    RelationshipProviderResponse,
    ground_relationship_observations,
    relationship_source_spans,
)
from app.llm.protocols import LlmCallOptions, LlmResponseSchema, TextGenerationClient
from app.usage.metering import estimate_text_input_tokens

DEFAULT_RELATIONSHIP_MODEL = "gpt-6.1-terra"
DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "relationship_extraction.md"
)
RELATIONSHIP_RESPONSE_SCHEMA = LlmResponseSchema(
    name="relationship_observations",
    schema=RelationshipProviderResponse.model_json_schema(),
    strict=True,
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


class RelationshipExtractor:
    """One explicit request, with a separately configured client/model and no retries.

    The caller owns transport, reasoning effort, usage accounting and persistence.
    No character IDs, fact schemas or previous extraction output are required, so
    the call can run alongside character/world extraction. Identity linking is later.
    """

    def __init__(
        self,
        llm_client: TextGenerationClient,
        *,
        model: str = DEFAULT_RELATIONSHIP_MODEL,
        max_output_tokens: int = 16000,
        max_input_tokens: int = 64000,
        reasoning_effort: str = "medium",
        prompt_path: Path = DEFAULT_PROMPT_PATH,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 128000:
            raise ValueError("max_output_tokens must be between 1 and 128000")
        if type(max_input_tokens) is not int or max_input_tokens <= 0:
            raise ValueError("max_input_tokens must be positive")
        self.call_options = LlmCallOptions(reasoning_effort=reasoning_effort)
        self.max_input_tokens = max_input_tokens
        self.llm_client = llm_client
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.system_prompt = prompt_path.read_text(encoding="utf-8")

    async def extract_from_chunk(
        self,
        *,
        source_chunk_id: UUID,
        chunk_text: str,
        episode_no: int | None = None,
        episode_title: str | None = None,
    ) -> RelationshipExtractionResult:
        if not chunk_text.strip():
            raise ValueError("chunk_text must not be empty")
        user_prompt = json.dumps({
            "episode_no": episode_no,
            "episode_title": episode_title,
            "source_spans": [{"ref": span.ref, "text": span.text}
                             for span in relationship_source_spans(chunk_text)],
        }, ensure_ascii=False)
        if estimate_text_input_tokens(
            self.system_prompt, user_prompt, self.model, RELATIONSHIP_RESPONSE_SCHEMA,
        ) > self.max_input_tokens:
            raise RelationshipInputTooLargeError("Independent relationship input exceeds its token budget.")
        response = await self.llm_client.create_text_response(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            prompt_cache_key=RELATIONSHIP_OBSERVATION_VERSION,
            response_schema=RELATIONSHIP_RESPONSE_SCHEMA,
            call_options=self.call_options,
        )
        try:
            payload = json.loads(response.text, object_pairs_hook=_unique_object)
            if not isinstance(payload, dict) or set(payload) != {"observations"}:
                raise ValueError("Invalid response envelope")
            raw = payload["observations"]
            if not isinstance(raw, list):
                raise TypeError("Invalid observation array")
        except (ValueError, TypeError):
            # Never expose manuscript-bearing JSON or provider values in an error.
            raise LlmExtractionError("Invalid independent relationship response.") from None
        observations, rejections = ground_relationship_observations(
            raw, chunk_text, source_chunk_id,
        )
        return RelationshipExtractionResult(
            source_chunk_id=source_chunk_id,
            raw_observations=raw,
            observations=observations,
            rejections=rejections,
            response=response,
        )
