import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from evals.setting_extraction.models import GoldDataset, GoldEpisode
from scripts.run_episode_text_analysis_debug import (
    load_character_setting_schema_hints,
    load_known_characters,
    run_episode_text_analysis_debug,
)


def main() -> None:
    args = _parse_args()
    dataset = GoldDataset.model_validate(
        json.loads(args.gold.read_text(encoding="utf-8"))
    )
    schema_hints = load_character_setting_schema_hints(args.setting_schemas)
    known_characters = load_known_characters(args.known_characters)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    work_id = _stable_uuid(dataset.dataset_version, "work")
    for episode in dataset.episodes:
        text_file = resolve_episode_source_file(episode, args.source_root)
        output_file = args.output_dir / f"episode-{episode.episode_no}.json"
        # Actions 로그에 후보 원문과 근거 미리보기가 남지 않도록 기존 디버그 러너의
        # 표준 출력만 가리고, 예외는 그대로 전파해 실행 실패를 숨기지 않는다.
        with redirect_stdout(StringIO()):
            result = run_episode_text_analysis_debug(
                text_file=text_file,
                episode_id=_stable_uuid(
                    dataset.dataset_version,
                    f"episode:{episode.episode_no}",
                ),
                work_id=work_id,
                analysis_job_id=_stable_uuid(
                    dataset.dataset_version,
                    f"analysis-job:{episode.episode_no}",
                ),
                episode_no=episode.episode_no,
                episode_title=episode.title,
                model_name=args.model_name,
                max_chunks=None,
                known_characters=known_characters,
                output_json=output_file,
                schema_hints=schema_hints,
            )
        print(
            f"episode={episode.episode_no} "
            f"candidateCount={result['summary']['candidateCount']}"
        )


def resolve_episode_source_file(episode: GoldEpisode, source_root: Path) -> Path:
    """GoldDataset의 상대 원고 경로가 지정한 private source 폴더 안에 있는지 확인한다."""

    if episode.source_file is None:
        raise ValueError(
            f"Episode {episode.episode_no} requires sourceFile for live analysis."
        )
    root = source_root.resolve()
    source_file = (root / episode.source_file).resolve()
    if not source_file.is_relative_to(root):
        raise ValueError(
            f"Episode {episode.episode_no} sourceFile escapes the source root: "
            f"{episode.source_file}"
        )
    if not source_file.is_file():
        raise ValueError(
            f"Episode {episode.episode_no} source file was not found: {source_file}"
        )
    return source_file


def _stable_uuid(dataset_version: str, purpose: str) -> UUID:
    # 평가 실행마다 식별자가 바뀌어 결과 diff가 흐려지지 않도록 입력에서 결정적으로 만든다.
    return uuid5(NAMESPACE_URL, f"catchhole:{dataset_version}:{purpose}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live setting extraction for every episode in a GoldDataset snapshot."
    )
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--setting-schemas", type=Path, required=True)
    parser.add_argument("--known-characters", type=Path, default=None)
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    main()
