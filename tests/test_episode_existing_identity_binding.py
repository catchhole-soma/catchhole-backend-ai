"""Existing actual homonyms in a single EP chunk use the existing typed binding."""

import asyncio
import json
from dataclasses import replace

import pytest

from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.exceptions import LlmExtractionError
from app.analysis.ordered_character_subjects import resolve_ordered_character_subjects
from app.llm.responses import LlmTextResponse
from tests.test_ordered_subject_rules import RecordingClient, _known, _setting
from tests.test_ordered_subject_rules import no_network as no_network

NAME = "백솔"
PROFILES = ("백솔은 기록관이며 별칭은 은등이다.", "백솔은 선원이며 별칭은 청돛이다.")


def actual_subjects():
    return [replace(_known(NAME), evidence=(profile,)) for profile in PROFILES]


def resolve(client, *, subjects=None, attempts=1):
    return asyncio.run(
        resolve_ordered_character_subjects(
            client=client,
            model="offline-test-model",
            max_attempts=attempts,
            max_output_tokens=2000,
            context=SubjectResolutionChunkContext(None, "기록관 백솔은 장부를 고쳤다.", None),
            candidates=[_setting(NAME, raw="기록관 백솔", quote="기록관 백솔은 장부를 고쳤다.")],
            subjects=actual_subjects() if subjects is None else subjects,
            episode_no=1,
        )
    )


@pytest.mark.parametrize("target", ["K1", "K2", None])
def test_typed_existing_actual_choice_is_requested_even_without_adjacent_chunk(target):
    subjects = actual_subjects()
    client = RecordingClient({"resolutions": [{"candidate_ref": "C1", "target_ref": target}]})
    rows, updates = resolve(client, subjects=subjects)
    assert len(client.requests) == 1
    expected = subjects[int(target[1:]) - 1].actual_character_id if target else None
    assert rows[0].binding.actual_character_id == expected
    assert rows[0].binding.provisional_subject_key is None
    assert rows[0].binding.book_identity_proof is None
    assert rows[0].binding.match_status == ("MATCHED" if target else "AMBIGUOUS")
    assert not updates
    prompt = client.requests[0]["user_prompt"]
    assert all(str(subject.actual_character_id) not in prompt for subject in subjects)
    assert all(profile in prompt for profile in PROFILES)
    assert client.requests[0]["max_output_tokens"] == 2000


def test_existing_unique_name_keeps_its_rule_without_an_extra_call():
    subject = actual_subjects()[0]
    client = RecordingClient()
    rows, _ = resolve(client, subjects=[subject])
    assert rows[0].binding.actual_character_id == subject.actual_character_id
    assert client.requests == []


def test_name_only_duplicate_context_stays_ambiguous_without_guessing():
    client = RecordingClient()
    rows, _ = resolve(client, subjects=[_known(NAME), _known(NAME)])
    assert rows[0].binding.match_status == "AMBIGUOUS"
    assert client.requests == []


def test_invalid_existing_ref_retries_with_same_actual_choices_and_safe_feedback():
    class Client:
        def __init__(self):
            self.requests = []
            self.responses = ["K999", "K2"]

        async def create_text_response(self, **kwargs):
            self.requests.append(kwargs)
            return LlmTextResponse(
                text=json.dumps(
                    {"resolutions": [{"candidate_ref": "C1", "target_ref": self.responses.pop(0)}]}
                )
            )

    client = Client()
    subjects = actual_subjects()
    rows, _ = resolve(client, subjects=subjects, attempts=2)
    assert rows[0].binding.actual_character_id == subjects[1].actual_character_id
    assert len(client.requests) == 2
    assert "K999" not in client.requests[-1]["user_prompt"]
    assert rows[0].binding.book_identity_proof is None


def test_episode_response_cannot_inject_book_identity_groups():
    client = RecordingClient(
        {"resolutions": [{"candidate_ref": "C1", "target_ref": "K1"}], "identity_groups": []}
    )
    with pytest.raises(LlmExtractionError):
        resolve(client)
