"""Exact basis sentences of a decision (`basis_quotes`, 오류 리포트 가독성 2026-11-02). Synthetic text only."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from app.validation.contracts import (
    BasisQuoteDraft,
    JudgeFinalAnswer,
    JudgeOpenAnswer,
    ValidationDecision,
)
from app.validation.judging import normalize_basis_quotes
from app.validation.models import JUDGE_SYSTEM
from app.validation.projection import setting_value_leaves
from app.validation.schemas import judge_schema
from app.validation.service import SettingValidationService
from tests.support.validation_synthetic import (
    ROLES,
    TARGET,
    FakeEmbedding,
    FakeSearch,
    ScriptedModel,
    SyntheticData,
    budget,
    decided,
    draft,
    follow_up,
)


def contract():
    path = Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json"
    return json.loads(path.read_text())["execution"]


def drafts(*pairs):
    return [BasisQuoteDraft(ref=ref, quote=quote) for ref, quote in pairs]


@pytest.mark.parametrize("vector", contract()["judgeFlow"]["basisQuotes"]["vectors"], ids=lambda row: row["name"])
def test_shared_contract_vectors(vector):
    stats = Counter()
    evidence = [vector["evidence"]] if vector["evidence"] else []
    settings = [vector["setting"]] if vector["setting"] else []
    kept = normalize_basis_quotes(drafts((vector["answer"]["ref"], vector["answer"]["quote"])), evidence, settings,
                                  stats)
    if vector["published"] is None:
        assert kept == [] and stats["BASIS_QUOTES_DROPPED"] == 1
    else:
        assert [row.model_dump() for row in kept] == [vector["published"]] and not stats


def test_contract_constants_match_the_worker():
    from app.validation import contracts

    rule = contract()["judgeFlow"]["basisQuotes"]
    assert rule["maxItems"] == contracts.MAX_BASIS_QUOTES == 3
    assert rule["maxQuoteCodePoints"] == contracts.BASIS_QUOTE_LIMIT == 300
    assert "basisQuotes" in contract()["payloadCitationPaths"]


CHUNK = "앞 문장이다. 민수는 의수로 검을 쥐었다. 뒤 문장이다."
EVIDENCE = [{"ref": "E1", "source_ref": "R2", "source_kind": "EPISODE", "start": 500, "end": 500 + len(CHUNK),
             "quote": CHUNK, "method": "VECTOR", "source_context": {"episodeNo": 2}}]
SETTINGS = [{"entry_ref": "B1", "subject_ref": "S1", "domain": "characters", "name": "민수",
             "state": {"slots": {"STATUS:손": {"factValue": "왼손 상실"}, "LEVEL:레벨": {"factValue": 3}}}}]


def test_evidence_quote_becomes_an_absolute_code_point_span_of_its_source():
    (quote,) = normalize_basis_quotes(drafts(("E1", "민수는 의수로 검을 쥐었다.")), EVIDENCE, SETTINGS)
    index = CHUNK.index("민수는")
    assert quote.model_dump() == {"ref": "E1", "source_ref": "R2", "start": 500 + index,
                                  "end": 500 + index + len("민수는 의수로 검을 쥐었다."), "quote": "민수는 의수로 검을 쥐었다."}
    # Code points, not UTF-8 bytes or UTF-16 units: a supplementary character before the quote counts once.
    wide = [{**EVIDENCE[0], "quote": "😀가나", "end": 503}]
    (quote,) = normalize_basis_quotes(drafts(("E1", "가나")), wide, [])
    assert (quote.start, quote.end) == (501, 503)


def test_invalid_quotes_are_dropped_and_counted_never_raised():
    stats = Counter()
    kept = normalize_basis_quotes(drafts(
        ("E9", "앞 문장이다."),           # no such evidence in this question
        ("Q1", "앞 문장이다."),           # not an E/B ref
        ("T1", "앞 문장이다."),           # the target is never a basis
        ("E1", "없는 문장"),              # not in the chunk
        ("E1", "가" * 301),               # longer than the limit
        ("E1", "   "),                    # empty after stripping
        ("B1", "3"),                      # a non-string value is not a leaf
        ("B1", "왼손 상실"),
        ("B1", " 왼손 상실 "),             # the same published quote again
    ), EVIDENCE, SETTINGS, stats)
    assert [(row.ref, row.quote) for row in kept] == [("B1", "왼손 상실")]
    assert stats["BASIS_QUOTES_DROPPED"] == 8


def test_at_most_three_quotes_are_kept_in_model_order():
    stats = Counter()
    kept = normalize_basis_quotes(drafts(("E1", "앞"), ("E1", "민수는"), ("B1", "왼손"), ("E1", "뒤")),
                                  EVIDENCE, SETTINGS, stats)
    assert [row.quote for row in kept] == ["앞", "민수는", "왼손"] and stats["BASIS_QUOTES_DROPPED"] == 1


def test_crlf_chunk_is_retried_with_crlf_and_the_chunk_substring_is_stored():
    evidence = [{**EVIDENCE[0], "quote": "첫 줄.\r\n둘째 줄.", "end": 500 + len("첫 줄.\r\n둘째 줄.")}]
    (quote,) = normalize_basis_quotes(drafts(("E1", "첫 줄.\n둘째 줄.")), evidence, [])
    assert quote.quote == "첫 줄.\r\n둘째 줄." and (quote.start, quote.end) == (500, 500 + len("첫 줄.\r\n둘째 줄."))


def test_setting_value_leaves_follow_the_shared_rule():
    world = {"domain": "worldSettings", "state": {"name": "성문", "propertiesJson": {
        "개방": {"시간": "낮", "예외": ["축제", {"밤": "횃불"}]}, "규모": 3, "별칭": "북문"}}}
    assert sorted(setting_value_leaves(world)) == sorted(["낮", "축제", "횃불", "북문"])
    assert setting_value_leaves({"domain": "worldSettings", "state": {"propertiesJson": "통째로 글"}}) == ["통째로 글"]
    assert setting_value_leaves(SETTINGS[0]) == ["왼손 상실"]
    assert setting_value_leaves({"domain": "worldHistory", "state": {"value": "밤에도 개방"}}) == []
    assert setting_value_leaves({"domain": "characters", "state": {"name": "민수", "aliases": ["민"]}}) == []


def test_old_decisions_without_basis_quotes_still_load():
    payload = contract()["requests"]["settingUnit"]["payload"]
    decision = ValidationDecision.model_validate({key: value for key, value in payload.items()
                                                  if key not in {"evidence_bundle", "basis_quotes"}})
    assert decision.basis_quotes == []
    assert JudgeOpenAnswer.model_fields["basis_quotes"].default_factory() == []
    assert JudgeFinalAnswer.model_fields["basis_quotes"].default_factory() == []


def test_all_three_judge_schemas_require_basis_quotes_with_kind_pattern_and_limits():
    for schema in (judge_schema(entry_refs=(), final=False, search_available=True),
                   judge_schema(entry_refs=(), final=False, search_available=False),
                   judge_schema(entry_refs=(), final=True, search_available=True)):
        assert "basis_quotes" in schema["required"]
        assert schema["properties"]["basis_quotes"] == {
            "type": "array", "items": {"$ref": "#/$defs/BasisQuoteDraft"}, "maxItems": 3}
        item = schema["$defs"]["BasisQuoteDraft"]
        assert item["required"] == ["ref", "quote"] and item["additionalProperties"] is False
        assert item["properties"]["ref"] == {"type": "string", "pattern": "^(B[1-9][0-9]*|E[1-9][0-9]*)$"}
        assert item["properties"]["quote"] == {"type": "string", "minLength": 1, "maxLength": 300}


def test_prompt_asks_for_basis_quotes_and_plain_author_sentences():
    assert "basis_quotes" in JUDGE_SYSTEM and "80자" in JUDGE_SYSTEM and "이번 원고" in JUDGE_SYSTEM
    assert "저장된 설정" in JUDGE_SYSTEM and "영수증" in JUDGE_SYSTEM and "FOLLOW_UP이면 basis_quotes는 빈 배열" in JUDGE_SYSTEM


def inspect(model, data=None, checkpoint=None):
    data = data or SyntheticData()
    service = SettingValidationService(data, model, budget(), ROLES, search=FakeSearch(data),
                                       embedding_client=FakeEmbedding(), checkpoint=checkpoint)
    return asyncio.run(service.inspect())


def searching_judge(quotes):
    def judge(payload, n):
        if n == 1:
            answer = follow_up(payload, searches=["민수가 의수를 얻은 장면"])
            answer["basis_quotes"] = [{"ref": "B1", "quote": "왼손"}]  # a FOLLOW_UP never carries a decision
            return answer
        return decided(payload, "NEEDS_CONTEXT", basis_quotes=quotes(payload))
    return judge


def test_judge_publishes_checked_quotes_and_ignores_follow_up_quotes():
    def quotes(payload):
        (row,) = payload["evidence"]
        return [{"ref": row["ref"], "quote": "의수를 얻었고"}, {"ref": "B1", "quote": "왼손 상실"},
                {"ref": row["ref"], "quote": "원고에 없는 문장"}]

    result = inspect(ScriptedModel({"TSEG1": [draft()]}, searching_judge(quotes)))
    assert result.status == "COMPLETED"
    prior = SyntheticData().texts["R2"]
    start = prior.index("의수를 얻었고")
    assert [row.model_dump() for row in result.decisions["Q1"].basis_quotes] == [
        {"ref": "E1", "source_ref": "R2", "start": start, "end": start + len("의수를 얻었고"), "quote": "의수를 얻었고"},
        {"ref": "B1", "source_ref": None, "start": None, "end": None, "quote": "왼손 상실"}]
    assert result.normalizations["BASIS_QUOTES_DROPPED"] == 1
    assert result.normalizations["FOLLOW_UP_DECISION_FIELDS_IGNORED"] == 1


def test_published_unit_cites_evidence_quotes_and_never_setting_quotes():
    from app.validation.contracts import split_setting_segments
    from tests.test_validation_checkpoint import RecordingJava, execution_claim, real_checkpoint

    segments = split_setting_segments(TARGET, unit_chars=10000, context_chars=100)
    java = RecordingJava(segments)

    def quotes(payload):
        return [{"ref": payload["evidence"][0]["ref"], "quote": "의수를 얻었고"}, {"ref": "B1", "quote": "왼손 상실"}]

    result = inspect(ScriptedModel({"TSEG1": [draft()]}, searching_judge(quotes)),
                     checkpoint=real_checkpoint(java, execution_claim()))
    assert result.status == "COMPLETED"
    (unit,) = java.published("units")
    published = unit["payload"]["basis_quotes"]
    assert published[1] == {"ref": "B1", "source_ref": None, "start": None, "end": None, "quote": "왼손 상실"}
    cited = {(row["sourceRef"], row["start"], row["end"], row["quote"]) for row in unit["citations"]}
    evidence_quote = published[0]
    assert (evidence_quote["source_ref"], evidence_quote["start"], evidence_quote["end"], evidence_quote["quote"]) in cited
    assert all(row["quote"] != "왼손 상실" for row in unit["citations"])
