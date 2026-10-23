import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.relationship_observations import ground_relationship_observations
from app.analysis.schemas import character_setting_provider_json_schema
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.llm.responses import LlmTextResponse
from evals.multi_stage_setting.relationship_observations import RecordingRelationshipExtractor

CHUNK_ID = UUID("00000000-0000-0000-0000-000000000001")
TEXT = "라온과 미루는 함께 출구로 가는 길을 찾았다. 라온은 12레벨이다."
HINTS = (CharacterSettingSchemaHint("level", "레벨", None, (), "NUMBER"),)


def observation(**updates):
    return {
        "source": {"name": "라온", "mention": "라온"},
        "target": {"name": "미루", "mention": "미루"},
        "kind": "COMPANION", "direction": "MUTUAL", "basis": "CONTEXTUAL",
        "temporal_scope": "PRESENT", "summary": "두 사람이 함께 길을 찾는다.",
        "evidence_quotes": ["라온과 미루는 함께 출구로 가는 길을 찾았다."],
        **updates,
    }


def setting():
    return {
        "candidate_kind": "SETTING", "entity_type": "CHARACTER", "entity_name": "라온",
        "raw_entity_mention": "라온", "attribute_name": "level", "attribute_value": "12",
        "value_type": "NUMBER", "value_json": {"value": 12, "extra_json": None},
        "evidence_spans": [{"quote": "라온은 12레벨이다.", "start_offset": None, "end_offset": None}],
        "confidence": 0.9,
    }


class Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def create_text_response(self, **kwargs):
        self.calls.append(kwargs)
        return LlmTextResponse(text=json.dumps(self.payload), raw_response={})


def extract(extractor):
    return asyncio.run(extractor.extract_from_chunk(
        source_chunk_id=CHUNK_ID, chunk_text=TEXT, schema_hints=HINTS, episode_no=2,
    ))


def test_opt_in_preserves_facts_and_adds_grounded_side_channel_in_one_call():
    client = Client({"candidates": [setting()], "relationship_observations": [observation()]})
    result = extract(CharacterSettingExtractor(llm_client=client, relationship_observations=True))
    assert len(client.calls) == 1
    assert result.candidates[0].value_json == {"value": 12}
    value, = result.relationship_observations
    assert value.source_chunk_id == CHUNK_ID
    assert value.basis == "CONTEXTUAL"
    assert value.kind == "COMPANION"
    span, = value.evidence_spans
    assert TEXT[span.start_offset:span.end_offset] == span.quote
    assert value.evidence_spans[0].start_offset == 0
    assert "relationship-observations:v1" in client.calls[0]["prompt_cache_key"]


def test_default_request_and_serialized_result_remain_unchanged():
    client = Client({"candidates": [setting()]})
    baseline = extract(CharacterSettingExtractor(llm_client=client))
    request = client.calls[0]
    assert "relationship_observations" not in request["system_prompt"]
    assert "relationship" not in request["prompt_cache_key"]
    assert "relationship_observations" not in request["response_schema"].schema["properties"]
    assert set(baseline.model_dump()) == {"candidates", "time_context"}
    extended = character_setting_provider_json_schema(relationship_observations=True)
    assert "relationship_observations" in extended["required"]
    # Building the extended schema must not mutate the cached default schema or candidate types.
    default = character_setting_provider_json_schema()
    assert "relationship_observations" not in default["properties"]
    assert extended["properties"]["candidates"] == default["properties"]["candidates"]
    assert all(extended["$defs"][key] == value for key, value in default["$defs"].items())


@pytest.mark.parametrize("raw,reason", [
    (None, "MISSING_ARRAY"), ({}, "INVALID_ARRAY"), ([{"key": "invalid"}], "INVALID_ITEM"),
    ([observation(evidence_quotes=["없는 문장"])], "EVIDENCE_NOT_FOUND"),
    ([observation(target={"name": "라온", "mention": "미루"})], "SELF_OR_AMBIGUOUS_ENDPOINTS"),
    ([observation(target={"name": "다온", "mention": "다온"})], "ENDPOINT_NOT_IN_EVIDENCE"),
    ([observation(direction="BOTH_WAYS")], "INVALID_ITEM"),
])
def test_bad_observation_keeps_valid_fact_without_retry(raw, reason):
    client = Client({"candidates": [setting()], "relationship_observations": raw})
    result = extract(CharacterSettingExtractor(llm_client=client, relationship_observations=True))
    assert len(client.calls) == 1
    assert result.candidates[0].value_json == {"value": 12}
    assert result.relationship_observations == []
    assert result.relationship_observation_rejections[0].reason == reason


def test_ambiguous_repeated_quote_is_not_assigned_an_arbitrary_location():
    values, rejected = ground_relationship_observations([observation()], TEXT + TEXT, CHUNK_ID)
    assert values == []
    assert rejected[0].reason == "EVIDENCE_NOT_UNIQUE"


def test_valid_neighbors_survive_invalid_observation_and_duplicate():
    values, rejected = ground_relationship_observations(
        [observation(), {}, observation()], TEXT, CHUNK_ID,
    )
    assert len(values) == 1
    assert [(r.index, r.reason) for r in rejected] == [(1, "INVALID_ITEM"), (2, "DUPLICATE")]


@pytest.mark.parametrize("basis", ["UNCERTAIN", "NEGATED", "FIGURATIVE", "CONTEXTUAL"])
def test_uncertainty_direction_and_past_are_preserved_not_promoted(basis):
    values, rejected = ground_relationship_observations([
        observation(basis=basis, temporal_scope="PAST", direction="SOURCE_TO_TARGET"),
    ], TEXT, CHUNK_ID)
    assert not rejected
    assert values[0].basis == basis
    assert values[0].direction == "SOURCE_TO_TARGET"
    assert values[0].temporal_scope == "PAST"


def test_an_unnamed_person_remains_unresolved():
    values, rejected = ground_relationship_observations([
        observation(source={"name": None, "mention": "라온"}),
    ], TEXT, CHUNK_ID)
    assert not rejected
    assert values[0].source.name is None


def test_evidence_quotes_are_not_silently_normalized():
    values, rejected = ground_relationship_observations([
        observation(evidence_quotes=["라온과 미루는  함께 출구로 가는 길을 찾았다."]),
    ], TEXT, CHUNK_ID)
    assert not values
    assert rejected[0].reason == "EVIDENCE_NOT_FOUND"


def test_invalid_fact_still_fails_existing_contract():
    from app.analysis.exceptions import LlmExtractionError

    bad = setting()
    bad["value_json"] = {"value": "12", "extra_json": None}
    client = Client({"candidates": [bad], "relationship_observations": [observation()]})
    with pytest.raises(LlmExtractionError):
        extract(CharacterSettingExtractor(
            llm_client=client, relationship_observations=True, max_attempts=1,
        ))


def test_private_sidecar_persists_observations_without_fact_scoring_changes(tmp_path):
    path = tmp_path / "observations.json"
    client = Client({"candidates": [setting()], "relationship_observations": [observation()]})
    recorder = RecordingRelationshipExtractor(
        CharacterSettingExtractor(llm_client=client, relationship_observations=True), path,
    )
    result = extract(recorder)
    assert len(result.candidates) == 1
    data = json.loads(path.read_text())
    assert data["status"] == "UNCONFIRMED_OBSERVATIONS"
    assert data["chunks"][0]["episodeNo"] == 2
    assert data["chunks"][0]["observations"][0]["kind"] == "COMPANION"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        RecordingRelationshipExtractor(recorder, path)


@pytest.mark.parametrize("mode,domains", [
    ("ORACLE", "CHARACTER"), ("ROLLING", "CHARACTER"), ("FIXED", "WORLD"),
    ("FIXED", "CHARACTER,WORLD"),
])
def test_cli_rejects_unsupported_experiments_before_loading_or_calling(monkeypatch, tmp_path, mode, domains):
    from evals.multi_stage_setting.runtime_cli import main

    monkeypatch.setattr("sys.argv", [
        "runtime", "--gold", str(tmp_path / "absent-gold.json"),
        "--mode", mode, "--domains", domains,
        "--output", str(tmp_path / "predictions.json"),
        "--relationship-observations-output", str(tmp_path / "observations.json"),
    ])
    with pytest.raises(ValueError, match="requires FIXED and CHARACTER"):
        main()
    assert not (tmp_path / "observations.json").exists()
