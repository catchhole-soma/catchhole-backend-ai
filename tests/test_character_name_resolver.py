from typing import Literal
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


def test_resolve_candidate_character_matches_pronoun_when_entity_name_matches_one_character() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="비요른 얀델", raw_entity_mention="나"),
        _known_characters(KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")),
    )

    assert match.match_status == SettingCandidateMatchStatus.MATCHED
    assert match.matched_character_id == BJORN_ID


def test_resolve_candidate_character_marks_pronoun_ambiguous_when_entity_matches_multiple_characters() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="비요른", raw_entity_mention="그"),
        _known_characters(
            KnownCharacter(character_id=BJORN_ID, name="비요른 얀델"),
            KnownCharacter(character_id=OTHER_BJORN_ID, name="비요른 라프손"),
        ),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_marks_pronoun_unresolved_when_entity_does_not_match_known_character() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="새 인물", raw_entity_mention="그녀"),
        _known_characters(KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")),
    )

    assert match.match_status == SettingCandidateMatchStatus.UNRESOLVED
    assert match.matched_character_id is None


def test_resolve_candidate_character_marks_pronoun_placeholder_ambiguous() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="미상", raw_entity_mention="나"),
        _known_characters(KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_keeps_placeholder_ambiguous_when_raw_exactly_matches() -> None:
    # fallback 이후에도 entity_name이 미상이면 raw가 기존 이름과 같더라도 자동 연결하지 않는다.
    # raw는 원문 표현일 뿐 해소된 캐릭터명은 아니므로 사용자 확인을 우선한다.
    match = resolve_candidate_character(
        _candidate(entity_name="미상", raw_entity_mention="비요른"),
        _known_characters(KnownCharacter(character_id=BJORN_ID, name="비요른")),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_marks_placeholder_with_descriptive_raw_ambiguous() -> None:
    # raw가 예상 밖 서술형 문구여도 "미상"을 새 캐릭터 이름으로 취급하지 않는다.
    match = resolve_candidate_character(
        _candidate(entity_name="미상", raw_entity_mention="내려다 본 손"),
        _known_characters(KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_marks_placeholder_without_raw_ambiguous() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="미상", raw_entity_mention=None),
        _known_characters(KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_marks_pronoun_entity_name_ambiguous() -> None:
    match = resolve_candidate_character(
        _candidate(entity_name="그", raw_entity_mention="나"),
        _known_characters(KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_marks_particle_attached_reference_ambiguous() -> None:
    # entity_name에 조사가 붙어도 지칭어를 새 캐릭터명으로 오해하지 않는다.
    match = resolve_candidate_character(
        _candidate(entity_name="주인공에게는", raw_entity_mention=None),
        _known_characters(KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")),
    )

    assert match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
    assert match.matched_character_id is None


def test_resolve_candidate_character_prefers_exact_name_over_particle_interpretation() -> None:
    # 이름 끝의 "은", "로"를 조사로 잘못 제거해 "나", "그"라는 지칭어로 오인하지 않는다.
    known_characters = _known_characters(
        KnownCharacter(character_id=AINAR_ID, name="나은"),
        KnownCharacter(character_id=BJORN_ID, name="그로"),
    )

    naeun_match = resolve_candidate_character(
        _candidate(entity_name="나은"),
        known_characters,
    )
    gro_match = resolve_candidate_character(
        _candidate(entity_name="그로"),
        known_characters,
    )

    assert naeun_match.match_status == SettingCandidateMatchStatus.MATCHED
    assert naeun_match.matched_character_id == AINAR_ID
    assert gro_match.match_status == SettingCandidateMatchStatus.MATCHED
    assert gro_match.matched_character_id == BJORN_ID


def test_resolve_candidate_character_does_not_match_direct_reference_as_exact_name() -> None:
    # "나", "그", "주인공" 자체는 DB에 같은 이름이 있어도 실제 이름으로 확정하지 않는다.
    match = resolve_candidate_character(
        _candidate(entity_name="나"),
        _known_characters(KnownCharacter(character_id=AINAR_ID, name="나")),
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


def test_character_discovery_ignores_related_known_name_in_raw_mention() -> None:
    match = resolve_candidate_character(
        _candidate(
            entity_name="세룸",
            raw_entity_mention="케닉의 넷째 아들 세룸",
            candidate_kind="CHARACTER_DISCOVERY",
        ),
        _known_characters(KnownCharacter(character_id=AINAR_ID, name="케닉")),
    )

    assert match.match_status == SettingCandidateMatchStatus.UNRESOLVED
    assert match.matched_character_id is None


def test_character_discovery_matches_known_character_by_entity_name() -> None:
    match = resolve_candidate_character(
        _candidate(
            entity_name="세룸",
            raw_entity_mention="케닉의 넷째 아들 세룸",
            candidate_kind="CHARACTER_DISCOVERY",
        ),
        _known_characters(
            KnownCharacter(character_id=AINAR_ID, name="케닉"),
            KnownCharacter(character_id=BJORN_ID, name="세룸"),
        ),
    )

    assert match.match_status == SettingCandidateMatchStatus.MATCHED
    assert match.matched_character_id == BJORN_ID


def test_new_named_setting_ignores_related_known_name_in_raw_mention() -> None:
    match = resolve_candidate_character(
        _candidate(
            entity_name="세룸",
            raw_entity_mention="케닉의 넷째 아들 세룸",
        ),
        _known_characters(KnownCharacter(character_id=AINAR_ID, name="케닉")),
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
    candidate_kind: Literal["SETTING", "CHARACTER_DISCOVERY"] = "SETTING",
) -> ExtractedSettingCandidate:
    is_discovery = candidate_kind == "CHARACTER_DISCOVERY"
    return ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        candidate_kind=candidate_kind,
        entity_type="CHARACTER",
        entity_name=entity_name,
        raw_entity_mention=raw_entity_mention,
        attribute_name=None if is_discovery else "level",
        attribute_value=None if is_discovery else "1",
        value_type=None if is_discovery else "NUMBER",
        value_json=None if is_discovery else {"value": 1},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote="비요른은 1레벨 바바리안이다.",
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=0.9,
    )
