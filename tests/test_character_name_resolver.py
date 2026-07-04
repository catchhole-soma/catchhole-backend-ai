from uuid import UUID

from app.analysis.character_name_resolver import (
    KnownCharacter,
    NormalizedKnownCharacter,
    normalize_known_characters,
    normalize_character_name,
    resolve_candidate_character,
)
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.domain.enums import SettingCandidateMatchStatus

AINAR_ID = UUID("00000000-0000-0000-0000-000000000101")
BJORN_ID = UUID("00000000-0000-0000-0000-000000000102")
OTHER_BJORN_ID = UUID("00000000-0000-0000-0000-000000000103")
CHUNK_ID = UUID("00000000-0000-0000-0000-000000000201")


def test_resolve_candidate_character_matches_exact_name() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="아이나르"),
        _known_characters(KnownCharacter(character_id=AINAR_ID, name="아이나르")),
    )

    assert match.match_status == SettingCandidateMatchStatus.MATCHED
    assert match.matched_character_id == AINAR_ID


def test_resolve_candidate_character_uses_raw_mention_for_long_source_expression() -> None:
    # 원문 mention에 긴 수식어가 붙어도 기존 캐릭터명이 그 안에 하나만 포함되면 매칭한다.
    match = resolve_candidate_character(
        _candidate(
            entity_name="아이나르",
            raw_entity_mention="프넬린의 두 번째 딸 아이나르",
        ),
        _known_characters(KnownCharacter(character_id=AINAR_ID, name="아이나르")),
    )

    assert match.match_status == SettingCandidateMatchStatus.MATCHED
    assert match.matched_character_id == AINAR_ID


def test_resolve_candidate_character_uses_entity_name_when_raw_mention_is_descriptive() -> None:
    # raw mention은 원문 표현이라 기존 이름과 직접 매칭되지 않을 수 있다.
    # 이때 raw가 대명사성 표현이 아니고 entity_name이 한 명과만 맞으면 LLM의 정리명을 살린다.
    match = resolve_candidate_character(
        _candidate(
            entity_name="아이나르",
            raw_entity_mention="프넬린의 두 번째 딸",
        ),
        _known_characters(KnownCharacter(character_id=AINAR_ID, name="아이나르")),
    )

    assert match.match_status == SettingCandidateMatchStatus.MATCHED
    assert match.matched_character_id == AINAR_ID


def test_resolve_candidate_character_marks_pronouns_ambiguous() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="비요른 얀델", raw_entity_mention="나"),
        _known_characters(KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_marks_conflicting_raw_and_entity_matches_ambiguous() -> None:
    # raw mention과 entity_name이 서로 다른 기존 캐릭터로 매칭되면 자동 연결하지 않는다.
    match = resolve_candidate_character(
        _candidate(entity_name="아이나르", raw_entity_mention="비요른"),
        _known_characters(
            KnownCharacter(character_id=AINAR_ID, name="아이나르"),
            KnownCharacter(character_id=BJORN_ID, name="비요른"),
        ),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_marks_multiple_matches_ambiguous() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="비요른"),
        _known_characters(
            KnownCharacter(character_id=BJORN_ID, name="비요른 얀델"),
            KnownCharacter(character_id=OTHER_BJORN_ID, name="비요른 라프손"),
        ),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_marks_missing_match_unresolved() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="새 인물"),
        _known_characters(KnownCharacter(character_id=AINAR_ID, name="아이나르")),
    )

    assert match.match_status == SettingCandidateMatchStatus.UNRESOLVED
    assert match.matched_character_id is None


def test_normalize_character_name_trims_wrapping_punctuation_and_spaces() -> None:
    assert normalize_character_name("  “비요른   얀델”  ") == "비요른 얀델"


def test_normalize_known_characters_prepares_names_once() -> None:
    known_characters = normalize_known_characters(
        [
            KnownCharacter(character_id=BJORN_ID, name="  “비요른   얀델”  "),
            KnownCharacter(character_id=OTHER_BJORN_ID, name="   "),
        ]
    )

    assert len(known_characters) == 1
    assert known_characters[0].character_id == BJORN_ID
    assert known_characters[0].normalized_name == "비요른 얀델"


def _known_characters(*characters: KnownCharacter) -> list[NormalizedKnownCharacter]:
    return normalize_known_characters(list(characters))


def _candidate(
    entity_name: str,
    raw_entity_mention: str | None = None,
) -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        entity_type="CHARACTER",
        entity_name=entity_name,
        raw_entity_mention=raw_entity_mention,
        attribute_name="level",
        attribute_value="1",
        value_type="NUMBER",
        value_json={"value": 1},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote="비요른은 1레벨 바바리안이다.",
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=0.9,
    )
