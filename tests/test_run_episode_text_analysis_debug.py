import asyncio
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analysis.character_name_resolver import ActiveCharacterStatus, KnownCharacter
from app.analysis.schemas import CharacterSettingExtractionResult
from app.analysis.setting_extractor import CharacterSettingSchemaHint
import scripts.run_episode_text_analysis_debug as debug_runner
from scripts.run_episode_text_analysis_debug import (
    _load_character_setting_schema_hints,
    _parse_args,
    load_known_characters,
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


def test_load_character_setting_schema_hints_accepts_wrapped_snapshot(tmp_path) -> None:
    schema_path = tmp_path / "character-setting-schemas.json"
    schema_path.write_text(
        json.dumps(
            {
                "characterSettingSchemas": [
                    {
                        "schemaKey": "stats.stat",
                        "displayName": "스탯",
                        "attributePattern": "stats.*",
                        "aliases": [],
                        "valueType": "NUMBER",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    hints = _load_character_setting_schema_hints(schema_path)

    assert len(hints) == 1
    assert hints[0].schema_key == "stats.stat"
    assert hints[0].attribute_pattern == "stats.*"
    assert hints[0].value_type == "NUMBER"


def test_load_character_setting_schema_hints_preserves_eval_fact_type(tmp_path) -> None:
    schema_path = tmp_path / "character-setting-schemas.json"
    schema_path.write_text(
        json.dumps(
            [
                {
                    "schemaKey": "guild.rank",
                    "displayName": "길드 계급",
                    "attributePattern": None,
                    "aliases": ["계급"],
                    "valueType": "STRING",
                    "canonicalFactType": "PROFILE",
                }
            ]
        ),
        encoding="utf-8",
    )

    hints = _load_character_setting_schema_hints(schema_path)

    assert hints[0].canonical_fact_type == "PROFILE"


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

        async def extract_from_chunk(self, *, known_characters=(), **kwargs):
            captured_known_characters.extend(known_characters)
            return CharacterSettingExtractionResult(candidates=[])

    class FakeSubjectResolver:
        def __init__(self, *, model=None) -> None:
            pass

        async def resolve_candidates(self, **kwargs):
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

    asyncio.run(
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
    )

    assert captured_known_characters == [known_character]


def test_load_known_characters_accepts_active_statuses_and_nullable_value(tmp_path) -> None:
    path = tmp_path / "known-characters.json"
    path.write_text(
        json.dumps(
            [
                {
                    "characterId": "00000000-0000-0000-0000-000000000099",
                    "name": "비요른 얀델",
                    "activeStatuses": [
                        {
                            "factKey": "status.오른발_부상",
                            "factValue": "오른발이 크게 다쳐 걷기 어려움",
                        },
                        {"factKey": "status.마비독", "factValue": None},
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    characters = load_known_characters(path)

    assert characters == [
        KnownCharacter(
            character_id=characters[0].character_id,
            name="비요른 얀델",
            active_statuses=(
                ActiveCharacterStatus(
                    fact_key="status.오른발_부상",
                    fact_value="오른발이 크게 다쳐 걷기 어려움",
                ),
                ActiveCharacterStatus(fact_key="status.마비독", fact_value=None),
            ),
        )
    ]


def test_load_known_characters_rejects_active_status_without_fact_value(tmp_path) -> None:
    path = tmp_path / "known-characters.json"
    path.write_text(
        json.dumps(
            [
                {
                    "characterId": "00000000-0000-0000-0000-000000000099",
                    "name": "비요른 얀델",
                    "activeStatuses": [{"factKey": "status.오른발_부상"}],
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="explicit nullable string factValue"):
        load_known_characters(path)
