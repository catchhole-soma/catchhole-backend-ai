import json
from uuid import UUID

from app.analysis.setting_extractor import CharacterSettingExtractor


def main() -> None:
    # 실제 OpenAI API key가 .env에 있을 때만 실행하는 수동 smoke 확인용 script
    extractor = CharacterSettingExtractor()
    result = extractor.extract_from_chunk(
        source_chunk_id=UUID("00000000-0000-0000-0000-000000000001"),
        episode_no=1,
        episode_title="샘플 회차",
        chunk_text=(
            "카엘은 12레벨 검사였다. 그는 오래된 화염검을 장비하고 있었고, "
            "근력은 80, 민첩은 65라고 알려져 있었다."
        ),
    )
    # key나 원본 OpenAI 응답 전체는 출력하지 않고, schema 검증이 끝난 후보 JSON만 출력한다.
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
