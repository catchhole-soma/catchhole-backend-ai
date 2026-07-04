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


# 매칭 비교에 사용할 수 있도록 이름을 미리 정규화한 캐릭터 정보.
@dataclass(frozen=True)
class NormalizedKnownCharacter:
    # 기존 캐릭터 ID
    character_id: UUID

    # 대표 이름
    name: str

    # normalize_character_name(name) 결과
    normalized_name: str


# 캐릭터 이름 매칭 결과.
@dataclass(frozen=True)
class CharacterNameMatch:
    # 매칭된 캐릭터 ID.
    # 매칭 실패 또는 애매한 경우 None.
    matched_character_id: UUID | None

    # MATCHED / AMBIGUOUS / UNRESOLVED 같은 매칭 상태.
    match_status: SettingCandidateMatchStatus


# 특정 인물을 명확히 가리킨다고 보기 어려운 표현들.
# 단, entity_name이 기존 캐릭터 1명과 유일하게 매칭되면 LLM의 문맥 추론을 살린다.
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

PLACEHOLDER_ENTITY_NAMES = {
    "미상",
    "불명",
    "불명확",
    "알 수 없음",
    "추론 불가",
    "unknown",
}


def normalize_known_characters(
    known_characters: list[KnownCharacter],
) -> list[NormalizedKnownCharacter]:
    # 대표 이름 정규화는 한 번만 수행하고 재사용한다.
    normalized_characters: list[NormalizedKnownCharacter] = []
    for character in known_characters:
        normalized_name = normalize_character_name(character.name)
        if not normalized_name:
            continue
        normalized_characters.append(
            NormalizedKnownCharacter(
                character_id=character.character_id,
                name=character.name,
                normalized_name=normalized_name,
            )
        )
    return normalized_characters


def resolve_candidate_character(
    candidate: ExtractedSettingCandidate,
    known_characters: list[NormalizedKnownCharacter],
) -> CharacterNameMatch:
    # raw_entity_mention은 원문에 실제 나온 표현이고,
    # entity_name은 LLM이 같은 청크 문맥에서 정리한 후보 이름이다.
    # 둘을 모두 매칭해보고, 서로 충돌하지 않을 때만 기존 캐릭터와 연결한다.
    #
    # 기본 우선순위는 raw를 더 강하게 보지만,
    # raw가 "나", "그" 같은 지칭어라면 raw 자체로는 캐릭터를 특정할 수 없다.
    # 이때는 LLM이 정리한 entity_name이 기존 캐릭터 1명과만 연결되는지 확인한다.
    normalized_raw_mention = normalize_character_name(candidate.raw_entity_mention)
    normalized_entity_name = normalize_character_name(candidate.entity_name)

    # raw mention이 없고 entity_name 자체도 대명사성 표현이면 확정할 수 없다.
    if not normalized_raw_mention and _is_ambiguous_mention(normalized_entity_name):
        return CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.AMBIGUOUS,
        )

    raw_matches = _find_matches(normalized_raw_mention, known_characters)
    entity_matches = _find_matches(normalized_entity_name, known_characters)

    # "나", "그녀", "주인공" 같은 원문 표현은 raw만으로는 확정할 수 없다.
    # 다만 프롬프트가 같은 청크 문맥에서 entity_name을 구체화했다면 그 추론을 활용한다.
    if _is_ambiguous_mention(normalized_raw_mention):
        # 지칭어라도 entity_name이 기존 캐릭터 1명과 유일하게 매칭되면,
        # LLM이 청크 문맥에서 주체를 해소했다고 보고 MATCHED로 둔다.
        if len(entity_matches) == 1:
            return CharacterNameMatch(
                matched_character_id=entity_matches[0],
                match_status=SettingCandidateMatchStatus.MATCHED,
            )

        # entity_name도 여러 기존 캐릭터에 걸리거나, "미상"/지칭어처럼
        # 아직 주체가 풀리지 않은 값이면 사용자 검토 또는 후속 fallback 대상으로 남긴다.
        if (
            len(entity_matches) > 1
            or _is_unresolved_placeholder(normalized_entity_name)
            or _is_ambiguous_mention(normalized_entity_name)
        ):
            return CharacterNameMatch(
                matched_character_id=None,
                match_status=SettingCandidateMatchStatus.AMBIGUOUS,
            )

        # 지칭어의 주체를 LLM이 구체 이름으로 정리했지만 기존 캐릭터 목록에는 없을 수 있다.
        # 이 경우는 새 캐릭터 후보 가능성이 있으므로 UNRESOLVED로 저장한다.
        return CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.UNRESOLVED,
        )

    # raw mention이 여러 기존 캐릭터에 걸리면 어느 인물인지 확정할 수 없다.
    # 예: raw="비요른"이 "비요른 얀델", "비요른 라프손" 모두에 포함되는 경우.
    if len(raw_matches) > 1:
        return CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.AMBIGUOUS,
        )

    if len(raw_matches) == 1:
        raw_match_id = raw_matches[0]

        # raw와 entity_name이 서로 다른 기존 캐릭터로 해석되면 충돌이다.
        if len(entity_matches) == 1 and entity_matches[0] != raw_match_id:
            return CharacterNameMatch(
                matched_character_id=None,
                match_status=SettingCandidateMatchStatus.AMBIGUOUS,
            )

        # raw가 한 명으로 확정되면 entity_name이 없거나 덜 정확해도 raw를 우선한다.
        return CharacterNameMatch(
            matched_character_id=raw_match_id,
            match_status=SettingCandidateMatchStatus.MATCHED,
        )

    # raw로는 못 찾았지만 entity_name이 여러 기존 캐릭터에 걸리면 애매하다.
    # raw가 설명형 표현이고 entity_name만으로도 여러 후보가 나오면 자동 연결하지 않는다.
    if len(entity_matches) > 1:
        return CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.AMBIGUOUS,
        )

    # raw가 불분명한 지시어(나, 그, 그녀)가 아니고 entity_name만 정확히 한 명과 매칭되면 살린다.
    # 예: raw="프넬린의 두 번째 딸", entity_name="아이나르".
    if len(entity_matches) == 1:
        return CharacterNameMatch(
            matched_character_id=entity_matches[0],
            match_status=SettingCandidateMatchStatus.MATCHED,
        )

    # raw와 entity_name 모두 비어 있거나, 기존 캐릭터 중 누구와도 연결되지 않는다.
    if not normalized_raw_mention and not normalized_entity_name:
        return CharacterNameMatch(
            matched_character_id=None,
            match_status=SettingCandidateMatchStatus.UNRESOLVED,
        )

    # 둘 다 매칭 실패.
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


def _is_ambiguous_mention(normalized_mention: str) -> bool:
    return normalized_mention in AMBIGUOUS_MENTIONS


def _is_unresolved_placeholder(normalized_name: str) -> bool:
    return not normalized_name or normalized_name in PLACEHOLDER_ENTITY_NAMES


def _find_matches(
    normalized_mention: str,
    known_characters: list[NormalizedKnownCharacter],
) -> list[UUID]:
    # 중복 매칭을 막기 위해 set 사용.
    matched_ids: set[UUID] = set()

    # 기존 캐릭터 목록을 하나씩 확인한다.
    for character in known_characters:
        # 1차: 완전 일치 매칭.
        # 예: mention="김철수", name="김철수"
        if normalized_mention == character.normalized_name:
            matched_ids.add(character.character_id)
            continue

        # 2차: 포함 관계 매칭.
        # 예: mention="철수", name="김철수"
        # 예: mention="김철수 검사", name="김철수"
        if _is_containment_match(normalized_mention, character.normalized_name):
            matched_ids.add(character.character_id)

    # set을 list로 바꿔 반환한다.
    return list(matched_ids)


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
