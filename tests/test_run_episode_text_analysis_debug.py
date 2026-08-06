import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.schemas import CharacterSettingExtractionResult
from app.analysis.setting_extractor import CharacterSettingSchemaHint
import scripts.run_episode_text_analysis_debug as debug_runner
from scripts.run_episode_text_analysis_debug import (
    _load_character_setting_schema_hints,
    _parse_args,
)


def test_load_character_setting_schema_hints_accepts_spring_claim_shape(tmp_path) -> None:
    schema_path = tmp_path / "character-setting-schemas.json"
    schema_path.write_text(
        json.dumps(
            [
                {
                    "schemaKey": "items.item",
                    "displayName": "아이템",
                    "attributePattern": "item.*",
                    "aliases": [],
                    "valueType": "JSON",
                }
            ]
        ),
        encoding="utf-8",
    )

    hints = _load_character_setting_schema_hints(schema_path)

    assert len(hints) == 1
    assert hints[0].schema_key == "items.item"
    assert hints[0].attribute_pattern == "item.*"
    assert hints[0].aliases == ()
    assert hints[0].value_type == "JSON"


def test_load_character_setting_schema_hints_rejects_empty_array(tmp_path) -> None:
    schema_path = tmp_path / "character-setting-schemas.json"
    schema_path.write_text("[]", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="--character-setting-schemas-json must not be empty",
    ):
        _load_character_setting_schema_hints(schema_path)


def test_parse_args_requires_character_setting_schemas_json(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_episode_text_analysis_debug.py",
            "--text-file",
            "episode.txt",
        ],
    )

    with pytest.raises(SystemExit):
        _parse_args()


def test_debug_runner_passes_known_characters_to_extractor(monkeypatch, tmp_path) -> None:
    captured_known_characters = []

    class FakeExtractor:
        def __init__(self, *, model=None) -> None:
            pass

        def extract_from_chunk(self, *, known_characters=(), **kwargs):
            captured_known_characters.extend(known_characters)
            return CharacterSettingExtractionResult(candidates=[])

    class FakeSubjectResolver:
        def __init__(self, *, model=None) -> None:
            pass

        def resolve_candidates(self, **kwargs):
            return SimpleNamespace(
                candidates=[],
                fallback_call_count=0,
                fallback_resolved_count=0,
                fallback_unresolved_count=0,
            )

    monkeypatch.setattr(debug_runner, "CharacterSettingExtractor", FakeExtractor)
    monkeypatch.setattr(debug_runner, "CharacterSubjectResolver", FakeSubjectResolver)
    text_file = tmp_path / "episode.txt"
    text_file.write_text("기존 캐릭터가 등장한다.", encoding="utf-8")
    known_character = KnownCharacter(character_id=uuid4(), name="비요른 얀델")

    debug_runner.run_episode_text_analysis_debug(
        text_file=text_file,
        episode_id=uuid4(),
        work_id=uuid4(),
        analysis_job_id=uuid4(),
        episode_no=2,
        episode_title=None,
        model_name=None,
        max_chunks=None,
        known_characters=[known_character],
        output_json=None,
        schema_hints=(
            CharacterSettingSchemaHint(
                schema_key="profile.species",
                display_name="종족",
                attribute_pattern=None,
                aliases=(),
                value_type="STRING",
            ),
        ),
    )

    assert captured_known_characters == [known_character]
