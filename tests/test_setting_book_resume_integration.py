"""Real worker, extractors, SQL saves and comparisons against a persisted fake Backend.

Only the remote storage/LLM/Spring boundary is replaced. Spring-owned world writes
and review decisions are represented by local test tables, never production SQL.
"""

import asyncio
import json
from datetime import datetime
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, insert, select, update

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
from app.analysis.character_subject_resolver import CharacterSubjectResolver
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.setting_extractor import CharacterSettingExtractor
from app.analysis.world_setting_comparator import (
    WorldSettingComparator,
    WorldSettingSubjectResolver,
)
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.clients.exceptions import AiTokenQuotaExhaustedError, SpringWorkerTransportError
from app.llm.responses import LlmTextResponse
from app.models.analysis_job import AnalysisJob
from app.models.setting_book_chunk import SettingBookChunk
from app.models.setting_candidate import SettingCandidate
from app.schemas.worker import (
    WorkerAnalysisJobPayload,
    WorkerCharacterFactComparisonBatchCandidate,
    WorkerCharacterFactComparisonBatchContextResponse,
    WorkerCharacterFactComparisonBatchPayload,
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonBatchContextResponse,
    WorkerWorldSettingComparisonBatchPayload,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingSubjectPageResponse,
    WorkerWorldSettingSubjectResolutionCandidate,
    WorkerWorldSettingSubjectResolutionPendingResponse,
    WorkerWorldSettingSubjectResolutionResponse,
    WorkerWorldSettingSubjectResolutionResult,
)
from app.services.setting_book_s3_chunking_service import SettingBookS3ChunkingService
from app.services.setting_candidate_service import SettingCandidateService
from app.worker.analysis_job_worker import AnalysisJobWorker
from tests.test_setting_book_source_contract import book_payload_data
from tests.semantic_current_fixtures import semantic_current_fixture

pytest_plugins = ["tests.test_setting_book_storage"]
from tests.test_setting_extractor import _valid_discovery_payload, _valid_setting_payload


class SyntheticBookProvider:
    def __init__(self, quota_at=None):
        self.requests = []
        self.quota_at = quota_at

    async def create_text_response(self, **request):
        self.requests.append(request)
        key = request["prompt_cache_key"]
        if self.quota_at and key.startswith(self.quota_at):
            self.quota_at = None
            raise AiTokenQuotaExhaustedError()
        if key.startswith("setting-extraction:"):
            discovery = _valid_discovery_payload(provider_payload=True)
            discovery.update(entity_name="아린", raw_entity_mention="아린",
                             evidence_spans=[{"quote": "아린", "start_offset": None, "end_offset": None}])
            setting = _valid_setting_payload(provider_payload=True)
            setting.update(entity_name="아린", raw_entity_mention="아린", attribute_name="stats.strength",
                attribute_value="10", value_json={"value": 10, "extra_json": None},
                evidence_spans=[{"quote": "| 아린 | 10 |", "start_offset": None, "end_offset": None}])
            result = {"candidates": [discovery, setting] if "| 아린 | 10 |" in request["user_prompt"] else []}
        elif key.startswith("character-fact-comparison-batch:"):
            result = {"decisions": [{"candidate_ref": "C1", "resolved_canonical_fact_key": "stats.strength",
                "operation": "ADD", "target_ref": None, "removed_snapshot_refs": [],
                "proposed_fact_value": "10", "proposed_value_json": {"value": 10},
                "temporal_scope": "PRESENT", "comparison_reason": "표에서 아린의 근력은 10으로 명시되어 있습니다."}]}
        elif key.startswith("world-setting-extraction:"):
            result = {"candidates": [{"category": "WORLD_RULE_HISTORY", "subject_name": "세계",
                "scope_name": None, "setting_name": "낮의 길이", "extracted_value": "길다",
                "temporal_scope": "PRESENT", "effective_from_episode_no": None,
                "epistemic_status": "ASSERTED",
                "evidence_spans": [{"quote": "낮은 길다."}], "confidence": 0.95}]
                if "낮은 길다." in request["user_prompt"] else []}
        elif key.startswith("world-manuscript-subject-resolution:"):
            result = {"decisions": [{"candidate_ref": "C1", "selected_subject_refs": [],
                                      "ambiguous": False, "identity_proofs": []}]}
        elif key.startswith("world-setting-comparison-batch:"):
            result = {"decisions": [{"source_candidate_refs": ["C1"], "consolidation_status": "SINGLE",
                "operation": "ADD", "review_reason": None, "target_ref": "T1", "matched_property_ref": None,
                "proposed_scope_name": None, "proposed_setting_name": "낮의 길이", "proposed_value": "길다",
                "comparison_reason": "설정집에서 낮이 길다는 세계 규칙을 확인했습니다.",
                "existing_root_property_names_to_move": []}]}
        else:
            raise AssertionError(f"Unexpected paid-operation boundary: {key}")
        return LlmTextResponse(text=json.dumps(result, ensure_ascii=False))


class PersistedBookBackend:
    def __init__(self, factory, payload, *, disconnect_at=None, reject_world=False):
        self.factory = factory
        self.payload = payload
        self.source = payload.frozen_source()
        self.disconnect_at = disconnect_at
        self.reject_world = reject_world
        self.progress = []
        self.completions = []
        self.failures = []
        self.publish_count = 0
        self.character_complete_count = 0
        self.world_complete_count = 0
        metadata = MetaData()
        self.world = Table("test_backend_world_candidates", metadata,
            Column("id", String, primary_key=True), Column("analysis_job_id", String),
            Column("item", JSON), Column("source", JSON), Column("state", String), Column("target_key", String))
        self.applied = Table("test_backend_applied_decisions", metadata,
            Column("source_candidate_id", String, primary_key=True), Column("domain", String),
            Column("payload", JSON), Column("apply_count", Integer))
        with factory() as session:
            metadata.create_all(session.get_bind())

    def _disconnect(self, point):
        if self.disconnect_at is not None and self.disconnect_at == point:
            self.disconnect_at = None
            raise SpringWorkerTransportError("Synthetic connection lost after Backend commit.")

    async def report_progress(self, analysis_job_id, lease_token, current_step,
                              episode_status=None, checkpoint_stage=None):
        assert analysis_job_id == self.payload.analysis_job_id
        self.progress.append((current_step, episode_status, checkpoint_stage))
        with self.factory() as session:
            job = session.get(AnalysisJob, analysis_job_id)
            assert job.lease_token == lease_token
            if checkpoint_stage is not None:
                job.checkpoint_stage = checkpoint_stage
            session.commit()
        self._disconnect(checkpoint_stage)

    async def fail(self, analysis_job_id, lease_token, **kwargs):
        self.failures.append(kwargs["failure_code"])
        with self.factory() as session:
            session.get(AnalysisJob, analysis_job_id).status = "FAILED"
            session.commit()

    async def complete(self, analysis_job_id, lease_token, summary_json):
        with self.factory() as session:
            job = session.get(AnalysisJob, analysis_job_id)
            assert job.lease_token == lease_token
            job.status = "SUCCEEDED"
            job.journal_status = "SEALED"
            job.automatic_applied_at = datetime(2026, 10, 6, 12)
            session.commit()
        self.completions.append(json.loads(summary_json))

    async def publish_world_setting_candidates(self, analysis_job_id, lease_token, candidates):
        self.publish_count += 1
        with self.factory() as session:
            for item in candidates:
                session.execute(insert(self.world).values(id=str(uuid4()), analysis_job_id=str(analysis_job_id),
                    item=item.model_dump(by_alias=True, mode="json"), source=self.source.model_dump(by_alias=True, mode="json"),
                    state="SUBJECT_PENDING", target_key=None))
            session.get(AnalysisJob, analysis_job_id).checkpoint_stage = "WORLD_CANDIDATES_PUBLISHED"
            session.commit()
        self._disconnect("WORLD_CANDIDATES_PUBLISHED")
        return candidates

    def _character_candidate(self):
        with self.factory() as session:
            return session.scalars(select(SettingCandidate).where(
                SettingCandidate.analysis_job_id == self.payload.analysis_job_id,
                SettingCandidate.candidate_kind == "SETTING",
                SettingCandidate.comparison_status == "PENDING")).first()

    def _character_batch(self, candidate):
        return WorkerCharacterFactComparisonBatchPayload(comparison_batch_id=uuid5(self.payload.analysis_job_id, str(candidate.id)),
            work_id=self.payload.work_id, source_episode_id=None, source=self.source,
            character_ref="K1", matched_character_name="아린", canonical_fact_type="STATS",
            analysis_context=self.payload.analysis_context, provisional_subject_key=candidate.provisional_subject_key,
            candidates=[WorkerCharacterFactComparisonBatchCandidate(candidate_ref="C1", projected_snapshot_ref="Q1",
                source_episode_no=None, source=self.source, attribute_value=candidate.attribute_value,
                value_json=candidate.value_json, value_type=candidate.value_type, evidence_spans=candidate.evidence_spans,
                raw_fact_key="stats.strength", initial_canonical_fact_key="stats.strength", canonical_key_resolution="EXACT")])

    async def claim_next_character_fact_comparison_batch(self, *_args):
        candidate = self._character_candidate()
        return self._character_batch(candidate) if candidate else None

    async def get_character_fact_comparison_batch_context(self, *_args):
        batch = self._character_batch(self._character_candidate())
        return WorkerCharacterFactComparisonBatchContextResponse(**batch.model_dump(exclude={"source", "source_episode_id", "work_id"}),
            base_snapshot_version=0, snapshot_entries=[], context_token="c" * 64)

    async def complete_character_fact_comparison_batch(self, analysis_job_id, batch_id, lease_token, request):
        candidate = self._character_candidate()
        assert request.failures == [] and request.decisions[0].operation == "ADD"
        with self.factory() as session:
            row = session.get(SettingCandidate, candidate.id)
            row.comparison_status = "COMPLETED"
            row.raw_comparison_json = request.model_dump(by_alias=True, mode="json")
            session.execute(insert(self.applied).values(source_candidate_id=str(row.id), domain="characters",
                payload=request.model_dump(by_alias=True, mode="json"), apply_count=1))
            session.commit()
        self.character_complete_count += 1

    async def fail_character_fact_comparison_batch(self, *_args, **_kwargs):
        # Spring makes quota-failed comparison batches claimable again on quota resume.
        pass

    def _world_rows(self, state=None):
        with self.factory() as session:
            statement = select(self.world).where(self.world.c.analysis_job_id == str(self.payload.analysis_job_id))
            if state is not None:
                statement = statement.where(self.world.c.state == state)
            return list(session.execute(statement).mappings())

    async def get_pending_world_setting_subject_resolutions(self, *_args):
        return WorkerWorldSettingSubjectResolutionPendingResponse(analysis_context=self.payload.analysis_context,
            candidates=[WorkerWorldSettingSubjectResolutionCandidate(candidate_id=row["id"], source_episode_id=None,
                source=row["source"], source_episode_no=None, category=row["item"]["category"],
                subject_name=row["item"]["subjectName"], evidence_spans=row["item"]["evidenceSpans"],
                rawExtractionJson=row["item"]["rawExtractionJson"])
                for row in self._world_rows("SUBJECT_PENDING")])

    async def get_world_setting_subjects(self, *_args):
        return WorkerWorldSettingSubjectPageResponse(subjects=[], page=0, has_next=False)

    async def reset_stale_world_setting_subject_resolution(self, *_args):
        raise AssertionError("A current book lease cannot require episode subject reset.")

    async def complete_world_setting_subject_resolutions(self, analysis_job_id, lease_token, request):
        results = []
        with self.factory() as session:
            for resolution in request.resolutions:
                assert resolution.target_world_setting_ids == []
                key = resolution.provisional_subject_keys[0]
                session.execute(update(self.world).where(self.world.c.id == str(resolution.candidate_id))
                    .values(state="PENDING", target_key=key))
                results.append(WorkerWorldSettingSubjectResolutionResult(candidate_id=resolution.candidate_id,
                    resolution_type="NEW", canonical_subject_key=key, canonical_subject_name="세계",
                    target_world_setting_ids=[], provisional_subject_keys=[key]))
            session.commit()
        return WorkerWorldSettingSubjectResolutionResponse(resolutions=results)

    def _world_batch(self, row):
        item = row["item"]
        return WorkerWorldSettingComparisonBatchPayload(comparison_batch_id=uuid5(self.payload.analysis_job_id, row["id"]),
            work_id=self.payload.work_id, source_episode_id=None, source=self.source, category=item["category"],
            resolution_type="NEW", canonical_subject_key=row["target_key"], canonical_subject_name=item["subjectName"],
            resolved_target_world_setting_ids=[], resolved_provisional_subject_keys=[row["target_key"]],
            analysis_context=self.payload.analysis_context, candidates=[WorkerWorldSettingComparisonBatchCandidate(
                candidate_ref="C1", candidate_id=row["id"], subject_name=item["subjectName"],
                scope_name=item["scopeName"], setting_name=item["settingName"], extracted_value=item["extractedValue"],
                temporal_scope=item["temporalScope"], effective_from_episode_no=item.get("effectiveFromEpisodeNo"),
                evidence_spans=item["evidenceSpans"], rawExtractionJson=item["rawExtractionJson"])])

    async def claim_next_world_setting_comparison_batch(self, *_args):
        rows = self._world_rows("PENDING")
        return self._world_batch(rows[0]) if rows else None

    async def get_world_setting_comparison_batch_context(self, *_args, **_kwargs):
        row = self._world_rows("PENDING")[0]
        batch = self._world_batch(row)
        return WorkerWorldSettingComparisonBatchContextResponse(comparison_batch_id=batch.comparison_batch_id,
            candidates=batch.candidates, exact_targets=[{"candidateRef": "C1", "worldSettingId": None}],
            targets=[WorkerWorldSettingComparisonTarget(provisional_subject_key=row["target_key"],
                subject_name="세계", properties=[], version=0)], analysis_context=self.payload.analysis_context,
            context_token="d" * 64)

    async def complete_world_setting_comparison_batch(self, analysis_job_id, batch_id, lease_token, request):
        row = self._world_rows("PENDING")[0]
        assert not request.failures and request.decisions[0].suggested_operation == "ADD"
        with self.factory() as session:
            session.execute(update(self.world).where(self.world.c.id == row["id"])
                .values(state="REJECTED" if self.reject_world else "COMPLETED"))
            if not self.reject_world:
                session.execute(insert(self.applied).values(source_candidate_id=row["id"], domain="worldSettings",
                    payload=request.model_dump(by_alias=True, mode="json"), apply_count=1))
            session.commit()
        self.world_complete_count += 1

    async def fail_world_setting_comparison_batch(self, *_args, **_kwargs):
        # Unexpected fixture contract drift must fail promptly rather than reclaim
        # the same synthetic row forever. Quota failures intentionally stay retryable.
        assert (_kwargs.get("failure_code") or _args[-1]) == "AI_TOKEN_QUOTA_EXHAUSTED"


def _payload_for_database(write, work_id, job_id):
    data = book_payload_data()
    data.update(analysisJobId=str(job_id), workId=str(work_id), leaseToken=str(write.lease_token), knownCharacters=[],
        analysisContext=write.context.model_dump(by_alias=True, mode="json"))
    data["settingBook"].update(settingBookId=str(write.setting_book_id), revisionId=str(write.setting_book_revision_id),
        revisionNo=1, originalFilename="설정.txt", contentS3Key=write.content_s3_key,
        contentS3Version=write.content_s3_version, contentHash=write.context.source_hash, charCount=write.char_count)
    data["semanticCurrentContext"] = semantic_current_fixture(data["analysisContext"], data["knownCharacters"])
    return WorkerAnalysisJobPayload.model_validate(data)


def _worker(backend, provider, factory, chunks, storage):
    worker = AnalysisJobWorker(spring_client=backend,
        setting_book_chunking_service=SettingBookS3ChunkingService(storage, chunks), setting_book_chunk_service=chunks,
        setting_extractor=CharacterSettingExtractor(llm_client=provider, model="test-model", max_attempts=1),
        world_setting_extractor=WorldSettingExtractor(llm_client=provider, model="test-model", max_attempts=1),
        subject_resolver=CharacterSubjectResolver(llm_client=provider, model="test-model"),
        setting_candidate_service=SettingCandidateService(factory),
        character_fact_comparison_pipeline=CharacterFactComparisonPipeline(backend, CharacterFactComparator(llm_client=provider, model="test-model", max_attempts=1)),
        world_setting_comparison_pipeline=WorldSettingComparisonPipeline(backend,
            WorldSettingSubjectResolver(llm_client=provider, model="test-model", max_attempts=1),
            WorldSettingComparator(llm_client=provider, model="test-model", max_attempts=1)),
        embedding_generation_enabled=True)

    class LocalIo:
        async def run(self, operation, *args, **kwargs):
            return operation(*args, **kwargs)

    worker._blocking_io_executor = LocalIo()
    worker._owns_blocking_io_executor = False
    return worker


@pytest.mark.parametrize("disconnect_at", ["CHARACTER_CANDIDATES_SAVED", "WORLD_CANDIDATES_PUBLISHED", "WORLD_COMPARISONS_FINISHED"])
def test_nonempty_book_run_survives_checkpoint_commit_before_ack_with_fresh_worker(book_database, disconnect_at):
    _run_persisted_scenario(book_database, disconnect_at=disconnect_at)


_BOOK_TEXT = "\ufeff| 인물 | 근력 |\r\n| 아린 | 10 |\r\n\r\n- 세계 규칙: 낮은 길다."


@pytest.mark.parametrize("book_database", [
    _BOOK_TEXT + "\n", "\n \n" + _BOOK_TEXT,
    "가" * 6000 + "\n\n" + _BOOK_TEXT,
    "\r\n" + _BOOK_TEXT + "\r\n\r\n",
    "\ufeff😀\u00a0설정\r\n" + _BOOK_TEXT + "\r\n",
], indirect=True, ids=["trailing-newline", "leading-blank", "inter-chunk-gap", "crlf", "bom-unicode"])
def test_book_whitespace_gaps_resume_from_chunks_ready_with_fresh_worker(book_database):
    _run_persisted_scenario(book_database, disconnect_at="CHUNKS_READY")


@pytest.mark.parametrize("mutation", ["text", "paragraph", "revision", "job", "raw-source"])
def test_fresh_book_worker_rejects_tampered_or_foreign_checkpoint_before_llm(book_database, mutation):
    factory, chunks, write, work_id, job_id, source_text = book_database
    payload = _payload_for_database(write, work_id, job_id)
    backend = PersistedBookBackend(factory, payload, disconnect_at="CHUNKS_READY")
    provider = SyntheticBookProvider()

    class Storage:
        def get_text(self, key, version_id=None):
            assert (key, version_id) == (write.content_s3_key, "v1")
            return source_text

    async def scenario():
        first = _worker(backend, provider, factory, chunks, Storage())
        try:
            with pytest.raises(SpringWorkerTransportError):
                await first.process_claimed(payload)
        finally:
            await first.aclose()
        with factory() as session:
            chunk = session.scalars(select(SettingBookChunk)).one()
            chunk_id = chunk.id
            if mutation == "text":
                chunk.chunk_text += "변조"
            elif mutation == "paragraph":
                chunk.paragraph_end_index += 1
            elif mutation == "revision":
                chunk.setting_book_revision_id = uuid4()
            elif mutation == "job":
                chunk.analysis_job_id = uuid4()
            job = session.get(AnalysisJob, job_id)
            assert job.checkpoint_stage == "CHUNKS_READY"
            new_token = uuid4()
            job.lease_token, job.status = new_token, "RUNNING"
            session.commit()
        if mutation == "raw-source":
            nonlocal source_text
            source_text += "\n"  # Non-whitespace count is unchanged; exact hash must still fail.
        resumed = payload.model_copy(update={"lease_token": new_token, "checkpoint_stage": "CHUNKS_READY"})
        backend.payload = resumed
        second = _worker(backend, provider, factory, chunks, Storage())
        try:
            with pytest.raises(ComparisonValidationError):
                await second.process_claimed(resumed)
        finally:
            await second.aclose()
        with factory() as session:
            assert list(session.scalars(select(SettingBookChunk.id))) == [chunk_id]
            assert list(session.scalars(select(SettingCandidate))) == []
        assert not provider.requests and not backend.completions and not backend.publish_count

    asyncio.run(scenario())


def test_book_worker_resumes_quota_failure_and_consumes_backend_rejection(book_database):
    _run_persisted_scenario(book_database, quota_at="world-setting-comparison-batch:", reject_world=True)


def _run_persisted_scenario(database, *, disconnect_at=None, quota_at=None, reject_world=False):
    factory, chunks, write, work_id, job_id, source_text = database
    payload = _payload_for_database(write, work_id, job_id)
    backend = PersistedBookBackend(factory, payload, disconnect_at=disconnect_at, reject_world=reject_world)
    provider = SyntheticBookProvider(quota_at=quota_at)
    reads = []

    class Storage:
        def get_text(self, key, version_id=None):
            reads.append((key, version_id))
            return source_text

    async def scenario():
        first = _worker(backend, provider, factory, chunks, Storage())
        try:
            with pytest.raises((SpringWorkerTransportError, AiTokenQuotaExhaustedError)):
                await first.process_claimed(payload)
        finally:
            await first.aclose()
        chunk_ids = [row.id for row in chunks.get_setting_book_chunks(job_id, write.setting_book_revision_id)]
        with factory() as session:
            candidate_ids = list(session.scalars(select(SettingCandidate.id)))
            job = session.get(AnalysisJob, job_id)
            checkpoint = job.checkpoint_stage
            assert checkpoint == (disconnect_at or "WORLD_CANDIDATES_PUBLISHED")
            assert job.status == "FAILED"
            new_token = uuid4()
            job.lease_token = new_token
            job.status = "RUNNING"
            session.commit()
        world_ids = [row["id"] for row in backend._world_rows()]
        assert len(candidate_ids) == (0 if checkpoint == "CHUNKS_READY" else 2)
        resumed = payload.model_copy(update={"lease_token": new_token, "checkpoint_stage": checkpoint})
        backend.payload = resumed
        second = _worker(backend, provider, factory, chunks, Storage())
        try:
            result = await second.process_claimed(resumed)
        finally:
            await second.aclose()
        assert result.episode_count == 0
        assert [row.id for row in chunks.get_setting_book_chunks(job_id, write.setting_book_revision_id)] == chunk_ids
        if world_ids:
            assert [row["id"] for row in backend._world_rows()] == world_ids
        with factory() as session:
            saved = list(session.scalars(select(SettingCandidate)))
            assert len(saved) == 2
            if candidate_ids:
                assert {row.id for row in saved} == set(candidate_ids)
            assert all(row.episode_id is None and row.source_kind == "SETTING_BOOK" for row in saved)
            assert all(row.source_chunk_id in chunk_ids for row in saved)
            assert all(row.source_setting_book_revision_id == write.setting_book_revision_id for row in saved)
            for row in saved:
                for span in row.evidence_spans:
                    assert source_text[span["start_offset"]:span["end_offset"]] == span["quote"]
            applied = list(session.execute(select(backend.applied)).mappings())
            assert len(applied) == (1 if reject_world else 2)
            assert all(row["apply_count"] == 1 for row in applied)
            job = session.get(AnalysisJob, job_id)
            assert job.checkpoint_stage == "WORLD_COMPARISONS_FINISHED"
            assert job.status == "SUCCEEDED" and job.journal_status == "SEALED"
            assert job.automatic_applied_at is not None
        assert backend.character_complete_count == backend.world_complete_count == backend.publish_count == 1
        assert reads == [(write.content_s3_key, "v1"), (write.content_s3_key, "v1")]
        assert all(episode_status is None for _step, episode_status, _checkpoint in backend.progress)
        assert backend.completions[0]["settingBookCount"] == 1 and backend.completions[0]["episodeCount"] == 0
        assert len(provider.requests) == len(chunk_ids) * 2 + 3 + (1 if quota_at else 0)
        assert all("SETTING_BOOK" in request["user_prompt"] for request in provider.requests)
        assert all(str(write.setting_book_id) not in request["user_prompt"] for request in provider.requests)
        for span in backend._world_rows()[0]["item"]["evidenceSpans"]:
            assert source_text[span["startOffset"]:span["endOffset"]] == span["quote"]
        assert backend._world_rows()[0]["source"]["revisionId"] == str(write.setting_book_revision_id)
        if reject_world:
            assert backend._world_rows()[0]["state"] == "REJECTED"

    asyncio.run(scenario())
