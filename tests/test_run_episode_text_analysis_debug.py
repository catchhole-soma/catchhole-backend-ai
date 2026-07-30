import json
import sys

import pytest

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
