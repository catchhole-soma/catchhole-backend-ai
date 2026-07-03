from dataclasses import dataclass
import re
from uuid import UUID

from app.analysis.schemas import ExtractedSettingCandidate
from app.domain.enums import SettingCandidateMatchStatus


# 이미 DB에 존재하는 캐릭터 정보.
# LLM이 추출한 후보의 이름이 이 목록 중 누구와 매칭되는지 판단한다.
@dataclass(frozen=True)
class KnownCharacter:
    # 기존 캐릭터 ID
    character_id: UUID

    # 대표 이름
    name: str

    # 별명/이명/다른 표기
    # 기본값은 빈 튜플
    aliases: tuple[str, ...] = ()


# 캐릭터 이름 매칭 결과.
@dataclass(frozen=True)
class CharacterNameMatch:
    # 매칭된 캐릭터 ID.
    # 매칭 실패 또는 애매한 경우 None.
    matched_character_id: UUID | None

    # MATCHED / AMBIGUOUS / UNRESOLVED 같은 매칭 상태.
    match_status: SettingCandidateMatchStatus


# 특정 인물을 명확히 가리킨다고 보기 어려운 표현들.
# 이런 표현은 기존 캐릭터와 바로 연결하지 않고 AMBIGUOUS 처리한다.
AMBIGUOUS_MENTIONS = {
    "나",
    "내",
    "나의",
    "내 캐릭터",
    "주인공",
    "그",
    "그녀",
    "그 남자",
    "그 여자",
    "그 아이",
    "그 사람",
    "이 사람",
    "저 사람",
}


def resolve_candidate_character(
    candidate: ExtractedSettingCandidate,
    known_characters: list[KnownCharacter],
) -> CharacterNameMatch:
    # LLM이 추출한 후보에서 매칭에 사용할 이름 표현을 고른다.
    # raw_entity_mention이 있으면 원문 표현을 우선 사용하고,
    # 없으면 entity_name을 사용한다.
    mention = _resolve_match_source(candidate)

    # 비교하기 쉽도록 이름을 정규화한다.
    # 예: '  “김 철수”  ' -> '김 철수'
    normalized_mention = normalize_character_name(mention)

    # "그", "그녀", "주인공" 같은 표현은 특정 캐릭터를 확정하기 어렵다.
    if normalized_mention in AMBIGUOUS_MENTIONS:
        return CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.AMBIGUOUS,
        )

    # 이름이 비어 있으면 매칭할 수 없으므로 UNRESOLVED 처리한다.
    if not normalized_mention:
        return CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.UNRESOLVED,
        )

    # 정규화된 mention을 기존 캐릭터 이름/별명 목록과 비교한다.
    matches = _find_matches(normalized_mention, known_characters)

    # 정확히 한 명만 매칭되면 성공.
    if len(matches) == 1:
        return CharacterNameMatch(
            matched_character_id=matches[0],
            match_status=SettingCandidateMatchStatus.MATCHED,
        )

    # 여러 명이 매칭되면 누구인지 확정할 수 없으므로 AMBIGUOUS.
    if len(matches) > 1:
        return CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.AMBIGUOUS,
        )

    # 아무도 매칭되지 않으면 UNRESOLVED.
    return CharacterNameMatch(
        matched_character_id=None,
        match_status=SettingCandidateMatchStatus.UNRESOLVED,
    )


def normalize_character_name(value: str | None) -> str:
    # None이면 비교할 이름이 없으므로 빈 문자열 반환.
    if value is None:
        return ""

    # 앞뒤 공백 제거.
    normalized = value.strip()

    # 이름 앞뒤에 붙은 따옴표, 괄호, 꺾쇠 등을 제거한다.
    # 예: "김철수", (김철수), 《김철수》 -> 김철수
    normalized = normalized.strip("\"'`“”‘’()[]{}<>〈〉《》")

    # 연속된 공백, 탭, 줄바꿈 등을 공백 하나로 줄인다.
    # 예: "김   철수" -> "김 철수"
    normalized = re.sub(r"\s+", " ", normalized)

    # 대소문자 차이를 없앤다.
    # lower()보다 유니코드 대응이 더 강한 casefold() 사용.
    return normalized.casefold()


def _resolve_match_source(candidate: ExtractedSettingCandidate) -> str:
    # raw_entity_mention은 원문에 실제로 나온 표현이다.
    # 예: "그 남자", "흑발의 소년", "철수"
    #
    # entity_name은 LLM이 정리한 이름일 수 있다.
    # raw_entity_mention이 있으면 대명사/수식어 여부 판단에 더 좋으므로 우선 사용한다.
    return candidate.raw_entity_mention or candidate.entity_name


def _find_matches(
    normalized_mention: str,
    known_characters: list[KnownCharacter],
) -> list[UUID]:
    # 중복 매칭을 막기 위해 set 사용.
    matched_ids: set[UUID] = set()

    # 기존 캐릭터 목록을 하나씩 확인한다.
    for character in known_characters:
        # 대표 이름과 별명을 모두 후보 이름으로 본다.
        for name in _candidate_names(character):
            # 기존 캐릭터 이름도 같은 방식으로 정규화한다.
            normalized_name = normalize_character_name(name)

            # 빈 이름은 비교하지 않는다.
            if not normalized_name:
                continue

            # 1차: 완전 일치 매칭.
            # 예: mention="김철수", name="김철수"
            if normalized_mention == normalized_name:
                matched_ids.add(character.character_id)
                continue

            # 2차: 포함 관계 매칭.
            # 예: mention="철수", name="김철수"
            # 예: mention="김철수 검사", name="김철수"
            if _is_containment_match(normalized_mention, normalized_name):
                matched_ids.add(character.character_id)

    # set을 list로 바꿔 반환한다.
    return list(matched_ids)


def _candidate_names(character: KnownCharacter) -> tuple[str, ...]:
    # 캐릭터의 대표 이름과 별명들을 하나의 튜플로 합친다.
    # *character.aliases는 튜플 안의 원소들을 펼치는 문법이다.
    #
    # 예:
    # character.name = "김철수"
    # character.aliases = ("철수", "검은 검사")
    #
    # 반환:
    # ("김철수", "철수", "검은 검사")
    return (character.name, *character.aliases)


def _is_containment_match(
    normalized_mention: str,
    normalized_name: str,
) -> bool:
    # 너무 짧은 문자열은 포함 매칭을 하지 않는다.
    # 예: "김", "이" 같은 한 글자는 오탐이 많기 때문.
    if len(normalized_mention) < 2 or len(normalized_name) < 2:
        return False

    # 한쪽이 다른 쪽에 포함되어 있으면 같은 캐릭터일 가능성이 있다고 본다.
    #
    # 예:
    # normalized_mention = "철수"
    # normalized_name = "김철수"
    # -> "철수" in "김철수" 이므로 True
    #
    # normalized_mention = "김철수 검사"
    # normalized_name = "김철수"
    # -> "김철수" in "김철수 검사" 이므로 True
    return normalized_mention in normalized_name or normalized_name in normalized_mention