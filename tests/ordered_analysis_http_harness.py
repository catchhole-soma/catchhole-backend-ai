"""Free deterministic Java HTTP/SQLAlchemy integration harness (explicit local inputs only)."""

import asyncio
import json
import sys
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
from app.analysis.character_subject_resolver import CharacterSubjectResolver
from app.analysis.setting_extractor import CharacterSettingExtractor
from app.analysis.world_setting_comparator import (
    WorldSettingComparator,
    WorldSettingSubjectResolver,
)
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.clients.spring_worker_client import SpringWorkerClient
from app.clients.exceptions import SpringWorkerHttpError
from app.llm.responses import LlmTextResponse
from app.services.episode_chunk_service import EpisodeChunkService
from app.services.episode_s3_chunking_service import EpisodeS3ChunkingService
from app.services.setting_candidate_service import SettingCandidateService
from app.worker.analysis_job_worker import AnalysisJobWorker
from evals.multi_stage_setting.ordered_journal import (
    SealedJournal,
    journal_state_hash,
    load_journal_json,
    replay_sealed_journals,
)
from evals.multi_stage_setting.ordered_state_projection import project_backend_state


def verify_java_journal_replay(engine, run_id, claimed_input_hashes):
    """Read only real Java-sealed rows; never fabricate seals from fake predictions."""
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text("""
            SELECT id, work_id, analysis_run_id, run_generation, run_sequence, source_episode_no,
                   status, journal_status, input_state_hash, run_base_state::text AS base_json,
                   state_journal::text AS journal_json
            FROM analysis_jobs WHERE analysis_run_id=:run_id ORDER BY run_sequence
        """),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
    assert len(rows) == len(claimed_input_hashes) == 10
    assert [row["input_state_hash"] for row in rows] == claimed_input_hashes
    initial = load_journal_json(rows[0]["base_json"])
    journals = []
    for row in rows:
        assert row["status"] == "SUCCEEDED" and row["journal_status"] == "SEALED"
        record = load_journal_json(row["journal_json"])
        journal = SealedJournal.model_validate({**record, "status": row["journal_status"]})
        assert (
            journal.job_id,
            journal.work_id,
            journal.run_id,
            journal.generation,
            journal.sequence,
            journal.episode_no,
            journal.input_state_hash,
        ) == (
            row["id"],
            row["work_id"],
            row["analysis_run_id"],
            row["run_generation"],
            row["run_sequence"],
            row["source_episode_no"],
            row["input_state_hash"],
        )
        journals.append(journal)
    replayed = replay_sealed_journals(
        initial,
        journals,
        run_id=rows[0]["analysis_run_id"],
        work_id=rows[0]["work_id"],
        generation=rows[0]["run_generation"],
        before_sequence=len(rows),
        before_episode_no=rows[-1]["source_episode_no"] + 1,
    )
    assert journal_state_hash(replayed) == journals[-1].output_state_hash
    projected = project_backend_state(
        replayed,
        scenario_id_by_episode={
            row["source_episode_no"]: f"synthetic-episode-{row['source_episode_no']}"
            for row in rows
        },
    )
    assert len(projected.known_characters) == 1 and projected.character_facts == []
    assert len(projected.world_facts) == 1
    assert projected.world_facts[0].value == "색10"
    assert projected.world_facts[0].subject_ref.startswith("provisional-world:")
    print(
        json.dumps(
            {
                "javaSealedJournalsReplayed": len(journals),
                "typedProjectionParity": True,
                "finalStateHash": journals[-1].output_state_hash,
            }
        ),
        flush=True,
    )


ISOLATED_WORLD_VALUES = {"구조": "복잡하다", "출입": "입구를 통한다", "주기": "매달 열린다", "괴물": "늑대가 산다"}


SCOPE_REVIEW_QUOTES = {
    1: "미궁 1층의 수정은 주변을 밝힌다.",
    2: "미궁 외곽 지역은 수정이 줄어들어 어둡다.",
}


PREPARATION_CHARACTER_QUOTE = "그 사람은 왼팔을 다쳤다."
PREPARATION_WORLD_QUOTE = "미궁의 구조는 여러 갈래다."


def source_text(episode_no, automatic_isolation=False, scope_review=False, preparation_failure=False):
    discovery = "세룸이 등장했다. " if episode_no == 1 else ""
    status = "세룸은 오른발을 다쳤다." if episode_no % 2 else "세룸은 완전히 회복했다."
    alias = " 세룸의 본명은 세룸 로안이다." if automatic_isolation and episode_no == 1 else ""
    repeat = " 백탑은 색1로 빛났다." if episode_no == 1 else ""
    failed_world = (" " + " ".join(f"미궁의 {key}는 {value}." for key, value in ISOLATED_WORLD_VALUES.items())
                    if automatic_isolation and episode_no == 1 else "")
    scope_text = " " + SCOPE_REVIEW_QUOTES[episode_no] if scope_review and episode_no in SCOPE_REVIEW_QUOTES else ""
    source = discovery + status + alias + failed_world + f" 백탑의 색상은 색{episode_no}로 바뀌었다." + repeat + scope_text
    if preparation_failure and episode_no == 1:
        # A real second chunk supplies the additional text that enables optional
        # character enhancement. Its empty extraction adds no invented settings.
        source += f" {PREPARATION_CHARACTER_QUOTE} {PREPARATION_WORLD_QUOTE}\n" + "고요" * 3500
    return source


class Storage:
    def __init__(self, automatic_isolation=False, scope_review=False, preparation_failure=False):
        self.automatic_isolation = automatic_isolation
        self.scope_review = scope_review
        self.preparation_failure = preparation_failure

    def get_text(self, key, version_id=None):
        assert key.startswith("gh180-e2e/") and version_id == "v1"
        return source_text(int(key.rsplit("/", 1)[1]), self.automatic_isolation,
                           self.scope_review, self.preparation_failure)


class DeterministicProvider:
    episode_no = 0
    invalid_attribute = False
    oversized_world = False
    dual_world_sources = False
    calls = []

    async def create_text_response(self, **kwargs):
        cache = kwargs["prompt_cache_key"]
        self.calls.append((self.episode_no, cache))
        if cache.startswith("setting-extraction:"):
            quote = "세룸은 오른발을 다쳤다." if self.episode_no % 2 else "세룸은 완전히 회복했다."
            candidate = dict(
                candidate_kind="SETTING",
                entity_type="CHARACTER",
                entity_name="세룸",
                raw_entity_mention="세룸",
                attribute_name="unsupported.부상" if self.invalid_attribute else "status.부상",
                attribute_value=quote,
                value_type="JSON",
                value_json={
                    "extra_json": json.dumps({"name": "부상", "active": bool(self.episode_no % 2)})
                },
                evidence_spans=[{"quote": quote, "start_offset": None, "end_offset": None}],
                confidence=0.95,
            )
            candidates = [candidate]
            if self.episode_no == 1:
                candidates.insert(
                    0,
                    dict(
                        candidate_kind="CHARACTER_DISCOVERY",
                        entity_type="CHARACTER",
                        entity_name="세룸",
                        raw_entity_mention="세룸",
                        attribute_name=None,
                        attribute_value=None,
                        value_type=None,
                        value_json=None,
                        evidence_spans=[
                            {"quote": "세룸이 등장했다.", "start_offset": None, "end_offset": None}
                        ],
                        confidence=0.95,
                    ),
                )
            result = {"candidates": candidates}
        elif cache.startswith("ordered-character-subject-resolution:"):
            payload = json.loads(kwargs["user_prompt"])
            result = {
                "resolutions": [
                    {
                        "candidate_ref": item["ref"],
                        "target_ref": "D1" if self.episode_no == 1 else "K1",
                    }
                    for item in payload["candidates"]
                ]
            }
        elif cache.startswith("character-fact-comparison-batch:"):
            payload = json.loads(kwargs["user_prompt"])
            candidate = payload["candidates"][0]
            removing = self.episode_no % 2 == 0
            assert len(payload["snapshot_entries"]) == (1 if removing else 0)
            result = {
                "decisions": [
                    {
                        "candidate_ref": candidate["candidate_ref"],
                        "resolved_canonical_fact_key": "status.부상",
                        "operation": "REMOVE" if removing else "ADD",
                        "target_ref": None,
                        "removed_snapshot_refs": [x["ref"] for x in payload["snapshot_entries"]],
                        "proposed_fact_value": None if removing else candidate["attribute_value"],
                        "proposed_value_json": None if removing else candidate["value_json"],
                        "temporal_scope": "PRESENT",
                        "comparison_reason": "현재 원문에서 회복했다."
                        if removing
                        else "현재 원문에서 부상을 입었다.",
                    }
                ]
            }
        elif cache.startswith("world-setting-extraction:"):
            result = {
                "candidates": [
                    {
                        "category": "LOCATION",
                        "subject_name": "백탑",
                        "scope_name": None,
                        "setting_name": "색상",
                        "extracted_value": "x" * 31000
                        if self.oversized_world
                        else f"색{self.episode_no}",
                        "evidence_spans": [
                            {"quote": f"백탑의 색상은 색{self.episode_no}로 바뀌었다."}
                        ],
                        "confidence": 0.95,
                    }
                ]
            }
            if self.dual_world_sources and self.episode_no == 1:
                result["candidates"].append(
                    {
                        **result["candidates"][0],
                        "evidence_spans": [{"quote": "백탑은 색1로 빛났다."}],
                    }
                )
        elif cache.startswith("ordered-world-subject-resolution:"):
            result = {"selected_subject_refs": ["S1"], "ambiguous": False}
        elif cache.startswith("world-setting-comparison-batch:"):
            payload = json.loads(kwargs["user_prompt"])
            assert len(payload["candidates"]) == (2 if self.episode_no == 1 else 1)
            assert len(payload["targets"]) == 1
            target = payload["targets"][0]
            assert len(target["properties"]) == (0 if self.episode_no == 1 else 1)
            result = {
                "decisions": [
                    {
                        "source_candidate_refs": [x["ref"] for x in payload["candidates"]],
                        "consolidation_status": "MERGED"
                        if len(payload["candidates"]) > 1
                        else "SINGLE",
                        "operation": "ADD" if self.episode_no == 1 else "UPDATE",
                        "review_reason": None,
                        "target_ref": target["ref"],
                        "matched_property_ref": None if self.episode_no == 1 else target["properties"][0]["ref"],
                        "proposed_scope_name": None,
                        "proposed_setting_name": "색상" if self.episode_no == 1 else None,
                        "proposed_value": f"색{self.episode_no}",
                        "existing_root_property_names_to_move": [],
                        "comparison_reason": "이번 원문에서 백탑의 색상이 바뀌었다.",
                    }
                ]
            }
        else:
            raise AssertionError(f"Unexpected deterministic provider operation: {cache}")
        return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))


class GeneralUncertaintyProvider(DeterministicProvider):
    """A questionable identity stays an original reference beside valid additions."""

    def __init__(self):
        self.calls = []
        self.confirmed_world_input_seen = False

    async def create_text_response(self, **kwargs):
        cache = kwargs["prompt_cache_key"]
        if cache.startswith("world-setting-extraction:") and self.episode_no == 1:
            response = await super().create_text_response(**kwargs)
            result = json.loads(response.text)
            result["candidates"].append({
                "category": "LOCATION", "subject_name": "미궁", "scope_name": None,
                "setting_name": "구조", "extracted_value": "복잡하다.", "confidence": 0.9,
                "evidence_spans": [{"quote": "미궁의 구조는 복잡하다."}],
            })
            return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))
        if cache.startswith("ordered-world-subject-resolution:"):
            payload = json.loads(kwargs["user_prompt"])
            self.calls.append((self.episode_no, cache))
            matches = [item["ref"] for item in payload["subjects"] if item["name"] == "백탑"]
            assert len(matches) == 1
            # Deliberate valid-reference identity mistake; the later explicit
            # review must not rewrite 미궁 as a confirmed 백탑 assertion.
            return LlmTextResponse(text=json.dumps({"selected_subject_refs": matches, "ambiguous": False}))
        if cache.startswith("world-setting-comparison-batch:"):
            payload = json.loads(kwargs["user_prompt"])
            if self.episode_no == 1:
                held = [item for item in payload["candidates"] if item["subject_name"] == "미궁"]
                normal = [item for item in payload["candidates"] if item["subject_name"] == "백탑"]
                assert len(held) == 1 and len(normal) == 2
                response = await super().create_text_response(**{
                    **kwargs, "user_prompt": json.dumps({**payload, "candidates": normal}),
                })
                result = json.loads(response.text)
                result["decisions"].append({
                    "source_candidate_refs": [held[0]["ref"]], "consolidation_status": "SINGLE",
                    "operation": "REVIEW_REQUIRED", "review_reason": "GENERAL_UNCERTAINTY",
                    "target_ref": payload["targets"][0]["ref"], "matched_property_ref": None,
                    "proposed_scope_name": None, "proposed_setting_name": "구조", "proposed_value": "복잡하다.",
                    "existing_root_property_names_to_move": [],
                    "comparison_reason": "미궁과 백탑이 같은 장소인지 확실하지 않아 확인이 필요합니다.",
                })
                return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))
            target = payload["targets"][0]
            assert target["confirmation_status"] == "CONFIRMED"
            assert target["properties"][0]["value"] == "색1"
            assert target["properties"][0]["review_source"] == "AUTOMATIC"
            refs = payload["unresolved_references"]
            assert len(refs) == 1
            assert refs[0]["subject_name"] == "미궁" and refs[0]["setting_name"] == "구조"
            assert refs[0]["value"] == "복잡하다." and refs[0]["applied_to_current_state"] is False
            assert refs[0]["reason"] == "앞 회차에서 대상이나 내용을 확정하지 못한 참고 정보입니다. 원문을 확인해 주세요."
            self.confirmed_world_input_seen = True
        return await super().create_text_response(**kwargs)


class PreparationFailureProvider(DeterministicProvider):
    """Exhaust subject validation retries while preserving unrelated confirmed facts."""

    def __init__(self):
        self.calls = []
        self.failed_character_attempts = 0
        self.failed_world_attempts = 0
        self.confirmed_world_input_seen = False

    async def create_text_response(self, **kwargs):
        cache = kwargs["prompt_cache_key"]
        if cache.startswith(("setting-extraction:", "world-setting-extraction:")):
            current = kwargs["user_prompt"].split("chunk_text:\n", 1)[-1]
            if current.startswith("고요"):
                self.calls.append((self.episode_no, cache))
                return LlmTextResponse(text='{"candidates": []}')
            response = await super().create_text_response(**kwargs)
            result = json.loads(response.text)
            if self.episode_no == 1:
                if cache.startswith("setting-extraction:"):
                    result["candidates"].append({
                        "candidate_kind": "SETTING", "entity_type": "CHARACTER",
                        "entity_name": "미상", "raw_entity_mention": "그 사람",
                        "attribute_name": "status.왼팔_부상", "attribute_value": PREPARATION_CHARACTER_QUOTE,
                        "value_type": "JSON", "value_json": {
                            "extra_json": json.dumps({"name": "왼팔_부상", "active": True})},
                        "evidence_spans": [{"quote": PREPARATION_CHARACTER_QUOTE,
                                            "start_offset": None, "end_offset": None}],
                        "confidence": 0.95,
                    })
                else:
                    result["candidates"].append({
                        "category": "LOCATION", "subject_name": "미궁", "scope_name": None,
                        "setting_name": "구조", "extracted_value": "여러 갈래다.", "confidence": 0.9,
                        "evidence_spans": [{"quote": PREPARATION_WORLD_QUOTE,
                                            "start_offset": None, "end_offset": None}],
                    })
            return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))
        if cache.startswith("ordered-character-subject-resolution:"):
            payload = json.JSONDecoder().raw_decode(kwargs["user_prompt"])[0]
            assert len(payload["candidates"]) == 1
            assert payload["candidates"][0]["entity_name"] == "미상"
            self.calls.append((self.episode_no, cache))
            self.failed_character_attempts += 1
            return LlmTextResponse(text=json.dumps({"resolutions": [{
                "candidate_ref": payload["candidates"][0]["ref"], "target_ref": "D999"}]}))
        if cache.startswith("ordered-world-subject-resolution:"):
            payload = json.JSONDecoder().raw_decode(kwargs["user_prompt"])[0]
            self.calls.append((self.episode_no, cache))
            if payload["candidate"]["subject_name"] == "미궁":
                self.failed_world_attempts += 1
                return LlmTextResponse(text='{"selected_subject_refs": ["S999"], "ambiguous": false}')
            matches = [item["ref"] for item in payload["subjects"]
                       if item["name"] == payload["candidate"]["subject_name"]]
            return LlmTextResponse(text=json.dumps({"selected_subject_refs": matches, "ambiguous": False}))
        if cache.startswith("world-setting-comparison-batch:") and self.episode_no == 2:
            payload = json.loads(kwargs["user_prompt"])
            target = payload["targets"][0]
            assert target["confirmation_status"] == "CONFIRMED"
            assert target["properties"][0]["value"] == "색1"
            assert target["properties"][0]["review_source"] == "AUTOMATIC"
            refs = payload["unresolved_references"]
            assert len(refs) == 2 and {item["setting_name"] for item in refs} == {"구조", "status.왼팔_부상"}
            self.confirmed_world_input_seen = True
        return await super().create_text_response(**kwargs)


class AutomaticIsolationProvider(DeterministicProvider):
    """Exhaust the initial response retries, then exercise guarded subgroup recovery."""

    recover_independent = False

    def __init__(self):
        self.calls = []
        self.failed_world_attempts = 0
        self.confirmed_world_input_seen = False

    async def create_text_response(self, **kwargs):
        cache = kwargs["prompt_cache_key"]
        if cache.startswith("setting-extraction:"):
            response = await super().create_text_response(**kwargs)
            result = json.loads(response.text)
            if self.episode_no == 1:
                result["candidates"].append({
                    "candidate_kind": "CHARACTER_DISCOVERY", "entity_type": "CHARACTER",
                    "entity_name": "세룸 로안", "raw_entity_mention": "세룸",
                    "attribute_name": None, "attribute_value": None,
                    "value_type": None, "value_json": None,
                    "evidence_spans": [{"quote": "세룸의 본명은 세룸 로안이다.",
                                        "start_offset": None, "end_offset": None}],
                    "confidence": 0.95,
                })
        elif cache.startswith("world-setting-extraction:"):
            response = await super().create_text_response(**kwargs)
            result = json.loads(response.text)
            if self.episode_no == 1:
                result["candidates"] = [{
                    "category": "LOCATION", "subject_name": "미궁", "scope_name": None,
                    "setting_name": key, "extracted_value": value,
                    "evidence_spans": [{"quote": f"미궁의 {key}는 {value}."}], "confidence": 0.95,
                } for key, value in ISOLATED_WORLD_VALUES.items()] + result["candidates"]
        elif cache.startswith("ordered-world-subject-resolution:"):
            self.calls.append((self.episode_no, cache))
            payload = json.loads(kwargs["user_prompt"])
            result = {"selected_subject_refs": [subject["ref"] for subject in payload["subjects"]
                      if subject["name"] == payload["candidate"]["subject_name"]], "ambiguous": False}
        elif cache.startswith("world-setting-comparison-batch:"):
            self.calls.append((self.episode_no, cache))
            payload = json.loads(kwargs["user_prompt"])
            assert len(payload["targets"]) == 1
            target = payload["targets"][0]
            failed = payload["candidates"][0]["subject_name"] == "미궁"
            if failed:
                assert self.episode_no == 1 and len(payload["candidates"]) in (1, 4)
                assert target["confirmation_status"] == "PROVISIONAL"
                assert target["properties"] == []
                self.failed_world_attempts += 1
                if "validation_feedback" in payload:
                    assert payload["validation_feedback"]["reason_code"] == "CANONICAL_TARGET_REQUIRED"
            elif self.episode_no > 1:
                assert target["confirmation_status"] == "CONFIRMED"
                assert target["properties"][0]["value"] == f"색{self.episode_no - 1}"
                assert target["properties"][0]["review_source"] == "AUTOMATIC"
                assert len([ref for ref in payload["unresolved_references"]
                            if ref["subject_name"] == "미궁"]) == (1 if self.recover_independent else 4)
                self.confirmed_world_input_seen = True
            if failed and self.recover_independent and len(payload["candidates"]) == 1:
                failed = payload["candidates"][0]["setting_name"] == "괴물"
            result = {"decisions": [{
                "source_candidate_refs": [candidate["ref"]], "consolidation_status": "SINGLE",
                "operation": "UPDATE" if self.episode_no > 1 else "ADD", "review_reason": None,
                "target_ref": None if failed else target["ref"],
                "matched_property_ref": target["properties"][0]["ref"] if self.episode_no > 1 else None,
                "proposed_scope_name": None,
                "proposed_setting_name": candidate["setting_name"] if self.episode_no == 1 else None,
                "proposed_value": candidate["extracted_value"], "existing_root_property_names_to_move": [],
                "comparison_reason": "이번 원문에서 확인한 장소의 특징이다.",
            } for candidate in payload["candidates"]]}
        else:
            return await super().create_text_response(**kwargs)
        return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))


class PartialRecoveryProvider(AutomaticIsolationProvider):
    recover_independent = True


class CharacterPartialRecoveryProvider(DeterministicProvider):
    """Keep the earlier valid injury and fail only a later invalid comparison."""

    def __init__(self):
        self.calls = []
        self.failed_character_attempts = 0
        self.character_attempt_metadata = []
        self.confirmed_character_input_seen = False

    async def create_text_response(self, **kwargs):
        cache = kwargs["prompt_cache_key"]
        if cache.startswith("setting-extraction:") and self.episode_no == 1:
            response = await super().create_text_response(**kwargs)
            result = json.loads(response.text)
            source = next(row for row in result["candidates"] if row["candidate_kind"] == "SETTING")
            original = source_text(1, automatic_isolation=True)
            quote = source["evidence_spans"][0]["quote"]
            start = original.index(quote)
            source["evidence_spans"] = [{"quote": quote, "start_offset": start,
                                         "end_offset": start + len(quote)}]
            later_quote = "오른발을 다쳤다."
            later_start = original.index(later_quote)
            result["candidates"].append({
                **source, "attribute_name": "status.오른발_부상",
                "attribute_value": "오른발에 부상을 입은 상태",
                "value_json": {"extra_json": json.dumps({"name": "오른발 부상", "active": True})},
                "evidence_spans": [{"quote": later_quote,
                                    "start_offset": later_start,
                                    "end_offset": later_start + len(later_quote)}],
            })
            return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))
        if cache.startswith("character-fact-comparison-batch:") and self.episode_no == 1:
            self.calls.append((self.episode_no, cache))
            payload = json.loads(kwargs["user_prompt"])
            assert len(payload["candidates"]) in (1, 2)
            self.failed_character_attempts += 1
            decisions = []
            for source in payload["candidates"]:
                invalid = source["initial_canonical_fact_key"] == "status.오른발_부상"
                decisions.append({
                    "candidate_ref": source["candidate_ref"],
                    "resolved_canonical_fact_key": source["initial_canonical_fact_key"],
                    "operation": "UPDATE" if invalid else "ADD", "target_ref": "P999" if invalid else None,
                    "removed_snapshot_refs": [], "proposed_fact_value": source["attribute_value"],
                    "proposed_value_json": source["value_json"], "temporal_scope": "PRESENT",
                    "comparison_reason": "원문에서 오른발을 다친 사실을 확인했다.",
                })
            self.character_attempt_metadata.append({
                "attempt": self.failed_character_attempts,
                "candidate_refs": [source["candidate_ref"] for source in payload["candidates"]],
                "snapshot_refs": [entry.get("ref", entry.get("snapshot_ref"))
                                  for entry in payload["snapshot_entries"]],
                "response_target_refs": [decision["target_ref"] for decision in decisions],
            })
            return LlmTextResponse(text=json.dumps({"decisions": decisions}, ensure_ascii=False))
        if cache.startswith("character-fact-comparison-batch:") and self.episode_no == 2:
            payload = json.loads(kwargs["user_prompt"])
            assert [row["fact_key"] for row in payload["snapshot_entries"]] == ["status.부상"]
            assert len(payload["unresolved_references"]) == 1
            assert payload["unresolved_references"][0]["setting_name"] == "status.오른발_부상"
            assert payload["unresolved_references"][0]["applied_to_current_state"] is False
            self.confirmed_character_input_seen = True
        return await super().create_text_response(**kwargs)


class ScopeReviewProvider(DeterministicProvider):
    """A real retry preserves a cross-scope comparison for review and later input."""

    def __init__(self):
        self.calls = []
        self.scope_attempts = 0
        self.confirmed_world_input_seen = False

    async def create_text_response(self, **kwargs):
        cache = kwargs["prompt_cache_key"]
        if cache.startswith("world-setting-extraction:"):
            response = await super().create_text_response(**kwargs)
            result = json.loads(response.text)
            if self.episode_no in SCOPE_REVIEW_QUOTES:
                result["candidates"].insert(0, {
                    "category": "LOCATION", "subject_name": "미궁",
                    "scope_name": "1층" if self.episode_no == 1 else "외곽 지역",
                    "setting_name": "광원" if self.episode_no == 1 else "조명 환경",
                    "extracted_value": SCOPE_REVIEW_QUOTES[self.episode_no],
                    "evidence_spans": [{"quote": SCOPE_REVIEW_QUOTES[self.episode_no]}],
                    "confidence": 0.95,
                })
        elif cache.startswith("ordered-world-subject-resolution:"):
            self.calls.append((self.episode_no, cache))
            payload = json.loads(kwargs["user_prompt"])
            result = {"selected_subject_refs": [item["ref"] for item in payload["subjects"]
                      if item["name"] == payload["candidate"]["subject_name"]], "ambiguous": False}
        elif cache.startswith("world-setting-comparison-batch:"):
            payload = json.loads(kwargs["user_prompt"])
            if payload["candidates"][0]["subject_name"] != "미궁":
                if self.episode_no == 3:
                    reviews = [item for item in payload["unresolved_references"]
                               if item["subject_name"] == "미궁"]
                    assert len(reviews) == 1
                    assert reviews[0]["scope_name"] == "외곽 지역"
                    self.confirmed_world_input_seen = True
                return await super().create_text_response(**kwargs)
            self.calls.append((self.episode_no, cache))
            assert len(payload["targets"]) == len(payload["candidates"]) == 1
            target, candidate = payload["targets"][0], payload["candidates"][0]
            retry = False
            if self.episode_no == 1:
                assert target["properties"] == []
            else:
                assert self.episode_no == 2
                assert target["confirmation_status"] == "CONFIRMED"
                assert len(target["properties"]) == 1
                assert target["properties"][0]["value"] == SCOPE_REVIEW_QUOTES[1]
                assert target["properties"][0]["review_source"] == "AUTOMATIC"
                self.scope_attempts += 1
                retry = self.scope_attempts > 1
                if retry:
                    assert payload["validation_feedback"]["reason_code"] != "COMPARISON_VALIDATION_FAILED"
            result = {"decisions": [{
                "source_candidate_refs": [candidate["ref"]], "consolidation_status": "SINGLE",
                "operation": "ADD" if self.episode_no == 1 else "REVIEW_REQUIRED" if retry else "UPDATE",
                "review_reason": "SCOPE_MISMATCH" if retry else None,
                "target_ref": target["ref"],
                "matched_property_ref": None if self.episode_no == 1 else target["properties"][0]["ref"],
                "proposed_scope_name": candidate["scope_name"] if self.episode_no == 1 or retry else None,
                "proposed_setting_name": candidate["setting_name"] if self.episode_no == 1 or retry else None,
                "proposed_value": candidate["extracted_value"], "existing_root_property_names_to_move": [],
                "comparison_reason": ("외곽 지역의 조명은 기존 1층 광원과 관련되지만 적용 범위 확인이 필요합니다."
                                      if retry else "원문에서 장소의 조명을 확인했습니다."),
            }]}
        else:
            return await super().create_text_response(**kwargs)
        return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))


class RecordingSpringWorkerClient(SpringWorkerClient):
    """Record completed real HTTP calls, without substituting API results."""

    def __init__(self, *args):
        super().__init__(*args)
        self.world_http_events = []
        self.claimed_context = None

    async def claim_next_character_fact_comparison_batch(self, analysis_job_id, lease_token):
        result = await super().claim_next_character_fact_comparison_batch(analysis_job_id, lease_token)
        if result is not None and result.analysis_context != self.claimed_context:
            actual, expected = result.analysis_context, self.claimed_context
            if actual is not None and expected is not None:
                differing = [name for name in type(actual).model_fields
                             if getattr(actual, name) != getattr(expected, name)]
                def normalized(context):
                    return sorted(item.model_dump_json() for item in context.unresolved_references)
                print(json.dumps({"characterBatchContextMismatchFields": differing,
                                  "inputHashesEqual": actual.input_state_hash == expected.input_state_hash,
                                  "referenceCounts": [len(actual.unresolved_references), len(expected.unresolved_references)],
                                  "referenceContentsEqualIgnoringOrder": normalized(actual) == normalized(expected)}), flush=True)
        return result

    async def claim_next_world_setting_comparison_batch(self, analysis_job_id, lease_token):
        result = await super().claim_next_world_setting_comparison_batch(analysis_job_id, lease_token)
        self.world_http_events.append((analysis_job_id, "claim", result is not None))
        return result

    async def fail_world_setting_comparison_batch(self, analysis_job_id, comparison_batch_id,
                                                lease_token, *args, **kwargs):
        result = await super().fail_world_setting_comparison_batch(
            analysis_job_id, comparison_batch_id, lease_token, *args, **kwargs)
        self.world_http_events.append((analysis_job_id, "fail", True))
        return result

    async def complete_world_setting_comparison_batch(self, analysis_job_id, comparison_batch_id,
                                                    lease_token, request):
        result = await super().complete_world_setting_comparison_batch(
            analysis_job_id, comparison_batch_id, lease_token, request)
        self.world_http_events.append((analysis_job_id, "complete", True))
        return result


async def run(base_url, database_url, count, mode="normal"):
    # All dependencies are constructed below, without consulting application .env.
    Settings.model_config = {**Settings.model_config, "env_file": None}
    assert urlparse(base_url).hostname == "127.0.0.1"
    database = make_url(database_url)
    assert (database.host == "127.0.0.1" and database.database == "gh180_e2e_test"
            and database.port != 35432)
    engine = create_engine(database_url, connect_args={"options": "-c timezone=Asia/Seoul"})
    factory = sessionmaker(engine, expire_on_commit=False)
    partial_recovery = mode == "automatic-world-partial-recovery"
    character_partial = mode == "automatic-character-partial-recovery"
    automatic_isolation = mode == "automatic-world-failure-isolation" or partial_recovery
    scope_review = mode == "automatic-scope-mismatch"
    preparation_failure = mode == "automatic-preparation-failure"
    general_uncertainty = mode == "automatic-general-uncertainty"
    automatic = automatic_isolation or scope_review or character_partial or preparation_failure or general_uncertainty
    spring = RecordingSpringWorkerClient(base_url, "gh180-e2e-test-only")
    provider = (GeneralUncertaintyProvider() if general_uncertainty else
                PreparationFailureProvider() if preparation_failure else
                CharacterPartialRecoveryProvider() if character_partial else
                PartialRecoveryProvider() if partial_recovery else
                ScopeReviewProvider() if scope_review else AutomaticIsolationProvider()
                if automatic_isolation else DeterministicProvider())
    provider.calls = []
    provider.invalid_attribute = mode == "prevalidation-failure"
    provider.oversized_world = mode == "world-input-limit"
    provider.dual_world_sources = mode == "normal" or scope_review or character_partial or preparation_failure or general_uncertainty
    chunks = EpisodeChunkService(factory)
    worker = AnalysisJobWorker(
        spring_client=spring,
        chunking_service=EpisodeS3ChunkingService(
            Storage(automatic_isolation or character_partial or preparation_failure or general_uncertainty,
                    scope_review, preparation_failure), chunks),
        episode_chunk_service=chunks,
        setting_candidate_service=SettingCandidateService(factory),
        setting_extractor=CharacterSettingExtractor(
            llm_client=provider, model="test-only", max_attempts=1
        ),
        subject_resolver=CharacterSubjectResolver(llm_client=provider, model="test-only"),
        character_fact_comparison_pipeline=CharacterFactComparisonPipeline(
            spring, CharacterFactComparator(llm_client=provider, model="test-only",
                                            max_attempts=3 if character_partial else 1)
        ),
        world_setting_extractor=WorldSettingExtractor(
            llm_client=provider, model="test-only", max_attempts=1
        ),
        world_setting_comparison_pipeline=WorldSettingComparisonPipeline(
            spring,
            WorldSettingSubjectResolver(llm_client=provider, model="test-only",
                                        max_attempts=3 if preparation_failure else 1),
            WorldSettingComparator(llm_client=provider, model="test-only",
                                   max_attempts=3 if automatic else 1),
        ),
        embedding_generation_enabled=False,
    )
    try:
        hashes = []
        for expected in range(1, count + 1):
            payload = await worker.claim_next()
            assert payload is not None and payload.episode.episode_no == expected
            assert payload.analysis_mode == "ORDERED_PROVISIONAL"
            if general_uncertainty:
                assert payload.review_mode == "AUTOMATIC" and payload.provisional_characters == []
                if expected == 2:
                    assert len(payload.known_characters) == 1
                    assert [row.fact_key for row in payload.known_characters[0].active_statuses] == ["status.부상"]
                    refs = payload.analysis_context.unresolved_references
                    assert len(refs) == 1 and refs[0].subject_name == "미궁"
                    assert refs[0].setting_name == "구조" and refs[0].value == "복잡하다."
                    assert refs[0].reason == "앞 회차에서 대상이나 내용을 확정하지 못한 참고 정보입니다. 원문을 확인해 주세요."
            elif preparation_failure:
                assert payload.review_mode == "AUTOMATIC" and payload.provisional_characters == []
                if expected == 2:
                    assert len(payload.known_characters) == 1
                    character = payload.known_characters[0]
                    assert character.name == "세룸"
                    assert [row.fact_key for row in character.active_statuses] == ["status.부상"]
                    assert character.active_statuses[0].provenance.review_source == "AUTOMATIC"
                    refs = payload.analysis_context.unresolved_references
                    assert len(refs) == 2
                    assert {row.setting_name for row in refs} == {"구조", "status.왼팔_부상"}
            elif character_partial:
                assert payload.review_mode == "AUTOMATIC" and payload.provisional_characters == []
                if expected == 2:
                    assert len(payload.known_characters) == 1
                    assert [row.fact_key for row in payload.known_characters[0].active_statuses] == ["status.부상"]
                    refs = payload.analysis_context.unresolved_references
                    assert len(refs) == 1 and refs[0].setting_name == "status.오른발_부상"
            elif scope_review:
                assert payload.review_mode == "AUTOMATIC" and payload.provisional_characters == []
                if expected == 3:
                    unresolved = payload.analysis_context.unresolved_references
                    assert len(unresolved) == 1 and unresolved[0].subject_name == "미궁"
            elif automatic_isolation:
                assert payload.review_mode == "AUTOMATIC"
                assert payload.provisional_characters == []
                if expected == 2:
                    assert len(payload.known_characters) == 1
                    character = payload.known_characters[0]
                    assert character.name == "세룸" and len(character.active_statuses) == 1
                    assert character.aliases == ["세룸 로안"]
                    assert character.active_statuses[0].provenance.confirmation_status == "CONFIRMED"
                    assert character.active_statuses[0].provenance.review_source == "AUTOMATIC"
                    unresolved = payload.analysis_context.unresolved_references
                    assert len(unresolved) == (1 if partial_recovery else 4)
                    assert {item.subject_name for item in unresolved} == {"미궁"}
            else:
                assert len(payload.provisional_characters) == (0 if expected == 1 else 1)
            if expected > 1 and not automatic:
                character = payload.provisional_characters[0]
                assert len(character.active_statuses) == (1 if expected % 2 == 0 else 0)
                assert character.identity_evidence
            provider.episode_no = expected
            spring.claimed_context = payload.analysis_context
            hashes.append(payload.analysis_context.input_state_hash)
            try:
                await worker.process_claimed(payload)
            except SpringWorkerHttpError as failure:
                if mode == "world-input-limit":
                    assert failure.spring_error_code == "WORLD_SETTING_ORDERED_COMPARISON_FAILED"
                    assert not any(
                        "world-setting-comparison" in cache for _, cache in provider.calls
                    )
                    assert await worker.claim_next() is None
                    print(
                        json.dumps(
                            {
                                "expectedWorldInputLimitFailure": True,
                                "worldComparisonProviderCalls": 0,
                            }
                        ),
                        flush=True,
                    )
                    return
                assert mode == "prevalidation-failure"
                assert failure.spring_error_code == "SETTING_CANDIDATE_ORDERED_COMPARISON_FAILED"
                assert not any(
                    "world" in cache or "character-fact" in cache for _, cache in provider.calls
                )
                assert await worker.claim_next() is None
                print(
                    json.dumps({"expectedPrevalidationFailure": True, "worldProviderCalls": 0}),
                    flush=True,
                )
                return
            assert mode == "normal" or automatic, "The invalid candidate unexpectedly passed prevalidation."
            if automatic_isolation and expected == 1:
                events = [(action, present) for job, action, present in spring.world_http_events
                          if job == payload.analysis_job_id]
                failed_at = events.index(("complete", True))
                assert events[failed_at + 1] == ("claim", True), events
                assert ("complete", True) in events[failed_at + 2:], events
                assert provider.failed_world_attempts == 7
            print(json.dumps({"episode": expected, "complete": True}), flush=True)
        assert await worker.claim_next() is None
        assert len(set(hashes)) == count
        if general_uncertainty:
            assert provider.confirmed_world_input_seen
            assert not any(action == "fail" for _, action, _ in spring.world_http_events)
            print(json.dumps({"automaticGeneralUncertaintyPreserved": True,
                              "originalSubjectPreservedInNextReference": True,
                              "nextEpisodeReadConfirmedState": True}), flush=True)
        elif preparation_failure:
            assert provider.failed_character_attempts == provider.failed_world_attempts == 3
            assert provider.confirmed_world_input_seen
            print(json.dumps({"automaticPreparationFailureIsolated": True,
                              "characterAndWorldFailuresPreserved": True,
                              "nextEpisodeReadConfirmedState": True}), flush=True)
        elif character_partial:
            print(json.dumps({"characterRecoveryAttempts": provider.failed_character_attempts,
                              "attempts": provider.character_attempt_metadata}), flush=True)
            assert provider.failed_character_attempts == 4, {
                "actual_count": provider.failed_character_attempts,
                "attempts": provider.character_attempt_metadata,
            }
            assert provider.confirmed_character_input_seen
            print(json.dumps({"automaticCharacterPartialRecovery": True,
                              "independentDecisionPreserved": True,
                              "failedSourcePreservedAsReference": True,
                              "nextEpisodeReadConfirmedState": True}), flush=True)
        elif scope_review:
            assert provider.scope_attempts == 2 and provider.confirmed_world_input_seen
            assert not any(action == "fail" for _, action, _ in spring.world_http_events)
            print(json.dumps({"scopeMismatchReviewPreserved": True,
                              "specificRetryThenReview": True,
                              "nextEpisodeReadReviewReference": True}), flush=True)
        elif automatic_isolation:
            assert provider.confirmed_world_input_seen
            print(json.dumps({"automaticWorldFailureIsolated": True,
                              "automaticWorldPartialRecovery": partial_recovery,
                              "failedBatchPreservedSourceCount": 1 if partial_recovery else 4,
                              "realHttpCompleteThenNextClaimAndComplete": True,
                              "nextEpisodeReadConfirmedState": True}), flush=True)
        else:
            verify_java_journal_replay(engine, payload.analysis_context.run_id, hashes)
        print(
            json.dumps({"completed": count, "fakeProviderCalls": len(provider.calls)}), flush=True
        )
    finally:
        await worker.aclose()
        await spring.aclose()
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]))
