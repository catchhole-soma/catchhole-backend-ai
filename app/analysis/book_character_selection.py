"""Pick the catalogue characters a setting-book chunk is about before sending their CURRENT.

Mirrors the error-report flow (subject_catalogue -> subject_refs -> selected CURRENT): a light
catalogue with identity hints goes to the model first, and only the chosen characters' details
reach the extraction request. There is no full-catalogue fallback: narrative text is selected by
the model, and a selection that still fails after retries fails the chunk like any other
provider response failure.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.analysis.character_name_resolver import normalize_character_name
from app.analysis.json_response import request_validated_model
from app.analysis.schemas import _to_openai_strict_json_schema
from app.llm.protocols import LlmResponseSchema, TextGenerationClient

logger = logging.getLogger(__name__)

DEFAULT_SELECTION_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "llm" / "prompts" / "book_character_selection.md"
)
BOOK_CHARACTER_SELECTION_SCHEMA_NAME = "book_character_selection"
BOOK_CHARACTER_SELECTION_CACHE_KEY = "book-character-selection:v1"
# PROFILE keys whose values help resolve titles, roles, pronouns and kinship in prose.
_HINT_KEY_PARTS = (
    "position", "occupation", "title", "rank", "affiliation", "spouse", "children", "siblings",
    "family", "lineage", "alias", "species", "gender", "role", "parent", "mother", "father",
    "직책", "직업", "소속", "가문", "배우자", "자녀", "형제", "호칭", "별명", "계급", "신분", "관계",
    "부모", "어머니", "아버지", "종족", "성별", "직위",
)
MAX_HINTS_PER_CHARACTER = 5
MAX_HINT_CHARS = 30
MIN_EXACT_NAME_CHARS = 2


class _Selected(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ref: str
    mention: str
    basis: Literal["NAME", "ALIAS", "TITLE", "PRONOUN", "FAMILY", "OTHER"]


class _Ambiguous(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mention: str
    candidate_refs: list[str]


class BookCharacterSelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    selected: list[_Selected]
    ambiguous: list[_Ambiguous]
    unmatched_mentions: list[str]


@dataclass(frozen=True)
class BookCharacterSelection:
    """Refs whose CURRENT details the extraction request may contain."""

    refs: frozenset[str]
    unmatched_mentions: tuple[str, ...]
    method: Literal["NUMERIC_ROWS", "MODEL", "EMPTY_CATALOG"]
    model_refs: frozenset[str] = frozenset()
    exact_name_refs: frozenset[str] = frozenset()


def selection_response_schema(refs):
    schema = _to_openai_strict_json_schema(BookCharacterSelectionResponse.model_json_schema())
    ref_schema = {"type": "string", "enum": list(refs)}
    defs = schema["$defs"]
    defs["_Selected"]["properties"]["ref"] = ref_schema
    defs["_Ambiguous"]["properties"]["candidate_refs"] = {"type": "array", "items": dict(ref_schema)}
    return LlmResponseSchema(name=BOOK_CHARACTER_SELECTION_SCHEMA_NAME, schema=schema)


def _hint_key(fact_key):
    lowered = fact_key.casefold()
    return any(part in lowered for part in _HINT_KEY_PARTS)


def book_character_hint_catalog(catalog, details):
    """Light rows for the selection request: identity plus role/kinship hints, never full CURRENT."""
    by_ref = {row["ref"]: row for row in details}
    rows = []
    for row in catalog:
        hints = {}
        for slot in by_ref.get(row["ref"], {}).get("current_slots", ()):
            if slot.get("fact_type") != "PROFILE" or not _hint_key(slot["fact_key"]):
                continue
            value = slot.get("fact_value")
            if not isinstance(value, str) or not value.strip():
                continue
            key = slot["fact_key"].removeprefix("profile.")
            if key not in hints:
                hints[key] = value.strip()[:MAX_HINT_CHARS]
            if len(hints) >= MAX_HINTS_PER_CHARACTER:
                break
        light = {key: row[key] for key in ("ref", "name", "aliases", "confirmation_status")}
        if hints:
            light["hints"] = hints
        rows.append(light)
    return rows


def exact_name_refs(chunk_text, catalog):
    """Deterministic safety net: whole names/aliases literally present in the chunk are always kept."""
    text = normalize_character_name(chunk_text).casefold()
    refs = set()
    for row in catalog:
        for value in (row["name"], *row["aliases"]):
            name = normalize_character_name(value).casefold()
            if len(name) >= MIN_EXACT_NAME_CHARS and name in text:
                refs.add(row["ref"])
                break
    return refs


def _validate_selection(response, allowed):
    for item in response.selected:
        if item.ref not in allowed:
            raise ValueError("Book character selection returned an unknown ref.")
    for item in response.ambiguous:
        if not item.candidate_refs or any(ref not in allowed for ref in item.candidate_refs):
            raise ValueError("Book character selection returned an unknown ambiguous ref.")


def _retry_prompt(original, error):
    payload = json.loads(original)
    payload["validation_feedback"] = {
        "reason_code": "BOOK_CHARACTER_SELECTION_INVALID",
        "correction": "입력 characters의 ref만 사용하고 schema 그대로 selected, ambiguous, "
                      "unmatched_mentions를 모두 채우세요. 확신 없는 인물은 ambiguous에 넣으세요.",
    }
    return json.dumps(payload, ensure_ascii=False)


def _unresolved_names(unresolved_references):
    names = []
    for item in unresolved_references:
        name = item.get("subject_name") if item.get("domain") == "characters" else None
        if name and name not in names:
            names.append(name)
    return names


async def select_book_characters(
    chunk_text,
    catalog,
    details,
    unresolved_references,
    *,
    client: TextGenerationClient,
    model: str | None,
    max_attempts: int,
    max_output_tokens: int,
    truncation_retry_max_output_tokens: int | None = None,
    prompt_path: Path = DEFAULT_SELECTION_PROMPT_PATH,
    numeric_refs=None,
) -> BookCharacterSelection:
    """Resolve which catalogue refs this chunk concerns.

    `numeric_refs` is the deterministic result for pure `name: level=12` rows; when present the
    model is not called. Otherwise the model selects, and whole-name matches are unioned in.
    """
    if not catalog:
        return BookCharacterSelection(frozenset(), (), "EMPTY_CATALOG")
    if numeric_refs is not None:
        return BookCharacterSelection(frozenset(numeric_refs), (), "NUMERIC_ROWS")
    refs = [row["ref"] for row in catalog]
    payload = {
        "characters": book_character_hint_catalog(catalog, details),
        "unresolved_references": _unresolved_names(unresolved_references),
        "chunk_text": chunk_text,
    }
    schema = selection_response_schema(refs)
    allowed = set(refs)
    response = await request_validated_model(
        client=client,
        response_model=BookCharacterSelectionResponse,
        system_prompt=prompt_path.read_text(encoding="utf-8"),
        user_prompt=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        model=model,
        max_output_tokens=max_output_tokens,
        max_attempts=max_attempts,
        prompt_cache_key=BOOK_CHARACTER_SELECTION_CACHE_KEY,
        operation_name="Book character selection",
        logger=logger,
        response_schema=schema,
        validate_model=lambda value: _validate_selection(value, allowed),
        retry_user_prompt_builder=_retry_prompt,
        truncation_retry_max_output_tokens=truncation_retry_max_output_tokens,
    )
    model_refs = {item.ref for item in response.selected}
    model_refs.update(ref for item in response.ambiguous for ref in item.candidate_refs)
    exact = exact_name_refs(chunk_text, catalog)
    unmatched = tuple(dict.fromkeys(m.strip() for m in response.unmatched_mentions if m.strip()))
    return BookCharacterSelection(
        refs=frozenset(model_refs | exact), unmatched_mentions=unmatched, method="MODEL",
        model_refs=frozenset(model_refs), exact_name_refs=frozenset(exact),
    )


def selection_summary(selection: BookCharacterSelection, total: int) -> dict:
    """Safe diagnostics: counts and method only, never names or source text."""
    return {
        "method": selection.method, "total": total, "selected": len(selection.refs),
        "model_selected": len(selection.model_refs), "exact_name_added": len(selection.exact_name_refs - selection.model_refs),
        "unmatched_mentions": len(selection.unmatched_mentions),
    }


__all__ = [
    "BOOK_CHARACTER_SELECTION_CACHE_KEY", "BOOK_CHARACTER_SELECTION_SCHEMA_NAME",
    "BookCharacterSelection", "BookCharacterSelectionResponse", "book_character_hint_catalog",
    "exact_name_refs", "select_book_characters", "selection_response_schema", "selection_summary",
]
