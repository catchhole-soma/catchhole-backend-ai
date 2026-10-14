import asyncio
import json

from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonRunResult
from app.analysis.character_subject_resolver import CharacterSubjectResolver
from app.analysis.setting_extractor import CharacterSettingExtractor
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.analysis.world_setting_pipeline import WorldSettingComparisonRunResult
from app.schemas.worker import WorkerAnalysisJobPayload
from app.services.setting_candidate_service import SettingCandidateService
from app.worker.analysis_job_worker import AnalysisJobWorker
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_setting_book_source_contract import book_payload_data
from tests.semantic_current_fixtures import semantic_current_fixture

pytest_plugins = ["tests.test_setting_book_storage"]


class BookSpring:
    def __init__(self):
        self.progress = []
        self.completed = []
        self.failures = []
        self.published = []

    async def report_progress(self, analysis_job_id, lease_token, current_step, episode_status=None, checkpoint_stage=None):
        self.progress.append((current_step, episode_status, checkpoint_stage))

    async def complete(self, **kwargs):
        self.completed.append(json.loads(kwargs["summary_json"]))

    async def fail(self, **kwargs):
        self.failures.append(kwargs["failure_code"])

    async def publish_world_setting_candidates(self, analysis_job_id, lease_token, candidates):
        self.published.extend(candidates)
        return candidates


class ComparisonStage:
    def __init__(self, world=False, error=None):
        self.world = world
        self.error = error
        self.contexts = []

    async def process_all(self, analysis_job_id, lease_token, **kwargs):
        self.contexts.append(kwargs["analysis_context"])
        if self.error:
            raise self.error
        return WorldSettingComparisonRunResult(0, 0) if self.world else CharacterFactComparisonRunResult(0, 0)


def test_book_run_shares_stages_and_restarts_with_stable_chunks(book_database):
    from app.services.setting_book_s3_chunking_service import SettingBookS3ChunkingService
    factory, chunks, write, work_id, job_id, text = book_database
    data = book_payload_data()
    data.update(analysisJobId=str(job_id), workId=str(work_id), leaseToken=str(write.lease_token),
                analysisContext=write.context.model_dump(by_alias=True, mode="json"))
    data["settingBook"].update(settingBookId=str(write.setting_book_id), revisionId=str(write.setting_book_revision_id),
        contentS3Key=write.content_s3_key, contentS3Version=write.content_s3_version,
        contentHash=write.context.source_hash, charCount=write.char_count)
    data["semanticCurrentContext"] = semantic_current_fixture(data["analysisContext"], data["knownCharacters"])
    payload = WorkerAnalysisJobPayload.model_validate(data)
    storage_requests = []

    class Storage:
        def get_text(self, key, version_id=None):
            storage_requests.append((key, version_id))
            return text

    spring = BookSpring()
    provider = RecordingClient({"candidates": []}, {"candidates": []})
    character = ComparisonStage()
    world = ComparisonStage(world=True)

    async def scenario():
        worker = AnalysisJobWorker(
            spring_client=spring,
            setting_book_chunking_service=SettingBookS3ChunkingService(Storage(), chunks),
            setting_book_chunk_service=chunks,
            setting_extractor=CharacterSettingExtractor(llm_client=provider, model="test-model"),
            world_setting_extractor=WorldSettingExtractor(llm_client=provider, model="test-model"),
            subject_resolver=CharacterSubjectResolver(llm_client=provider, model="test-model"),
            setting_candidate_service=SettingCandidateService(factory),
            character_fact_comparison_pipeline=character, world_setting_comparison_pipeline=world,
            embedding_generation_enabled=True,
        )
        # Use a synchronous runner for the one-thread local SQLite fixture.
        class LocalIo:
            async def run(self, operation, *args, **kwargs):
                return operation(*args, **kwargs)
        worker._blocking_io_executor = LocalIo()
        worker._owns_blocking_io_executor = False
        try:
            result = await worker.process_claimed(payload)
            ids = [chunk.id for chunk in chunks.get_setting_book_chunks(job_id, write.setting_book_revision_id)]
            from app.models.analysis_job import AnalysisJob
            with factory() as session:
                session.get(AnalysisJob, job_id).checkpoint_stage = "WORLD_COMPARISONS_FINISHED"
                session.commit()
            resumed = payload.model_copy(update={"checkpoint_stage": "WORLD_COMPARISONS_FINISHED"})
            await worker.process_claimed(resumed)
            assert [chunk.id for chunk in chunks.get_setting_book_chunks(job_id, write.setting_book_revision_id)] == ids
            return result
        finally:
            await worker.aclose()

    result = asyncio.run(scenario())
    assert result.episode_count == 0
    assert len(provider.requests) == 2
    assert storage_requests == [(write.content_s3_key, "v1"), (write.content_s3_key, "v1")]
    assert spring.completed[0]["episodeCount"] == 0
    assert spring.completed[0]["settingBookCount"] == 1
    assert all(status is None for _step, status, _checkpoint in spring.progress)
    assert character.contexts == [write.context, write.context]
    assert world.contexts == [write.context, write.context]
    for call in provider.requests:
        prompt = call["user_prompt"]
        metadata = (json.loads(prompt)["metadata"] if prompt.startswith("{") else
                    json.JSONDecoder().raw_decode(prompt.split("metadata:\n", 1)[1])[0])
        assert metadata["source_kind"] == "SETTING_BOOK"
