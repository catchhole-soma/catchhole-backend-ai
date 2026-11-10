"""Deterministic stand-in for the BOOK stage-1 character selection model in offline tests."""
import json

from app.analysis.book_character_selection import BOOK_CHARACTER_SELECTION_SCHEMA_NAME


def is_book_selection_request(kwargs):
    schema = kwargs.get("response_schema")
    return schema is not None and schema.name == BOOK_CHARACTER_SELECTION_SCHEMA_NAME


def fake_book_selection(kwargs, *, refs=None, ambiguous=(), unmatched=()):
    """Return a valid selection payload for a selection request, or None for other requests.

    Without explicit `refs`, every catalogue character whose name or alias occurs in the chunk is
    selected by NAME. Tests that need indirect references pass `refs` themselves.
    """
    if not is_book_selection_request(kwargs):
        return None
    payload = json.loads(kwargs["user_prompt"])
    if refs is None:
        text = payload["chunk_text"]
        refs = [row["ref"] for row in payload["characters"]
                if any(value and value in text for value in (row["name"], *row["aliases"]))]
    return {
        "selected": [{"ref": ref, "mention": ref, "basis": "NAME"} for ref in refs],
        "ambiguous": [{"mention": mention, "candidate_refs": list(candidates)} for mention, candidates in ambiguous],
        "unmatched_mentions": list(unmatched),
    }
