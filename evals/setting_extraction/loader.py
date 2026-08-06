import json
from pathlib import Path
from typing import Any

from app.domain.enums import SettingCandidateMatchStatus
from evals.setting_extraction.models import (
    CharacterSettingSchemaSnapshot,
    GoldDataset,
    PredictionBundle,
    PredictionEpisode,
)


def load_gold_dataset(path: Path, source_root: Path | None = None) -> GoldDataset:
    payload = _read_json(path)
    dataset = GoldDataset.model_validate(payload)
    resolved_episodes = []
    base_dir = source_root or path.parent
    for episode in dataset.episodes:
        if episode.source_file is None:
            resolved_episodes.append(episode)
            continue
        # 저작권 원고는 정답 JSON과 분리하고 실행 시점에만 메모리로 결합한다.
        source_path = (base_dir / episode.source_file).resolve()
        if not source_path.is_file():
            raise ValueError(
                f"Source file for episode {episode.episode_no} was not found: {source_path}"
            )
        resolved_episodes.append(
            episode.model_copy(
                update={
                    "source_file": None,
                    "source_text": source_path.read_text(encoding="utf-8"),
                }
            )
        )
    return dataset.model_copy(update={"episodes": resolved_episodes})


def load_prediction_bundle(paths: list[Path]) -> PredictionBundle:
    episodes: list[PredictionEpisode] = []
    seen_episode_numbers: set[int] = set()
    for path in paths:
        payload = _read_json(path)
        for episode in _parse_prediction_payload(payload, path):
            if episode.episode_no in seen_episode_numbers:
                raise ValueError(
                    f"Duplicate prediction episodeNo={episode.episode_no} across input files."
                )
            seen_episode_numbers.add(episode.episode_no)
            episodes.append(episode)
    return PredictionBundle(episodes=sorted(episodes, key=lambda item: item.episode_no))


def load_setting_schema_snapshot(path: Path) -> list[CharacterSettingSchemaSnapshot]:
    payload = _read_json(path)
    raw_schemas = payload.get("characterSettingSchemas") if isinstance(payload, dict) else payload
    if not isinstance(raw_schemas, list):
        raise ValueError(
            "Setting schema snapshot must be an array or contain characterSettingSchemas."
        )
    if not raw_schemas:
        raise ValueError("Setting schema snapshot must contain at least one schema.")
    return [CharacterSettingSchemaSnapshot.model_validate(item) for item in raw_schemas]


def _parse_prediction_payload(payload: Any, path: Path) -> list[PredictionEpisode]:
    if not isinstance(payload, dict):
        raise ValueError(f"Prediction JSON must be an object: {path}")
    if "episodes" in payload:
        return PredictionBundle.model_validate(payload).episodes
    if "summary" in payload and "settingCandidates" in payload:
        # 실제 분석 디버그 스크립트의 결과를 별도 변환 파일 없이 바로 읽는다.
        summary = payload["summary"]
        episode_no = summary.get("episodeNo") if isinstance(summary, dict) else None
        episode = PredictionEpisode.model_validate(
            {
                "episodeNo": episode_no,
                "candidates": payload["settingCandidates"],
            }
        )
        return [_attach_matched_character_names(episode, payload, path)]
    if "episodeNo" in payload and "candidates" in payload:
        return [PredictionEpisode.model_validate(payload)]
    raise ValueError(
        "Prediction JSON must be a debug analysis result, an episode object, "
        f"or an episodes bundle: {path}"
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _attach_matched_character_names(
    episode: PredictionEpisode,
    payload: dict[str, Any],
    path: Path,
) -> PredictionEpisode:
    """디버그 결과의 MATCHED ID를 기존 캐릭터 대표 이름으로 변환한다."""

    raw_characters = payload.get("knownCharacters", [])
    if not isinstance(raw_characters, list):
        raise ValueError(f"knownCharacters must be an array: {path}")

    name_by_id: dict[str, str] = {}
    for index, raw_character in enumerate(raw_characters):
        if not isinstance(raw_character, dict):
            raise ValueError(f"knownCharacters[{index}] must be an object: {path}")
        character_id = raw_character.get("characterId") or raw_character.get("character_id")
        name = raw_character.get("name")
        if character_id is None or not isinstance(name, str) or not name.strip():
            raise ValueError(f"knownCharacters[{index}] requires characterId and name: {path}")
        name_by_id[str(character_id)] = name

    candidates = []
    for candidate in episode.candidates:
        matched_name = None
        if candidate.match_status == SettingCandidateMatchStatus.MATCHED:
            if candidate.matched_character_id is None:
                raise ValueError(f"MATCHED candidate requires matched_character_id: {path}")
            matched_name = name_by_id.get(candidate.matched_character_id)
            if matched_name is None:
                raise ValueError(
                    "MATCHED candidate references an unknown character ID "
                    f"{candidate.matched_character_id}: {path}"
                )
        candidates.append(candidate.model_copy(update={"matched_character_name": matched_name}))
    return episode.model_copy(update={"candidates": candidates})
