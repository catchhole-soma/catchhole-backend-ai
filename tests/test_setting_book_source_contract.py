"""Frozen source transport and model context; fixtures contain no manuscript data."""

import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.analysis.ordered_context import ordered_provenance_data
from app.analysis.setting_extractor import CharacterSettingExtractor
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.schemas.analysis_context import AnalysisStateProvenance
from app.schemas.worker import WorkerAnalysisJobPayload
from tests.test_analysis_job_worker import _payload
from tests.test_ordered_analysis_runtime import RecordingClient, _input_context
from tests.test_setting_extractor import CHUNK_ID, DEFAULT_SCHEMA_HINTS

BOOK_ID = uuid4()
REVISION_ID = uuid4()
SOURCE = {
    "sourceKind": "SETTING_BOOK", "episodeId": None, "episodeNo": None,
    "settingBookId": str(BOOK_ID), "settingBookName": "인물 설정.docx",
    "revisionId": str(REVISION_ID), "revisionNo": 2, "contentHash": "b" * 64,
}


def book_payload_data():
    from tests.semantic_current_fixtures import semantic_current_fixture
    data = _payload().model_dump(by_alias=True, mode="json")
    data.update(
        sourceKind="SETTING_BOOK", episode=None, analysisMode="ORDERED_PROVISIONAL",
        reviewMode="AUTOMATIC", comparisonEvidenceContract={"formatVersion": 1}, analysisContext=_input_context().model_dump(by_alias=True, mode="json"),
        settingBook={
            "settingBookId": str(BOOK_ID), "originalFilename": "인물 설정.docx",
            "revisionId": str(REVISION_ID), "revisionNo": 2,
            "contentS3Key": "works/test/setting-books/test/revision.txt",
            "contentS3Version": "revision-version", "contentHash": "b" * 64, "charCount": 13,
        },
    )
    data["semanticCurrentContext"] = semantic_current_fixture(data["analysisContext"], data["knownCharacters"])
    return data


def test_book_claim_has_frozen_source_without_fabricated_episode():
    payload = WorkerAnalysisJobPayload.model_validate(book_payload_data())
    assert payload.source_kind == "SETTING_BOOK"
    assert payload.episode is None
    assert payload.setting_book.revision_id == REVISION_ID
    assert payload.setting_book.revision_no == 2


@pytest.mark.parametrize("mutation", ["missing_book", "wrong_hash", "mixed", "confirmed_only", "manual"])
def test_book_extraction_rejects_invalid_manifest_before_pipeline(mutation):
    data = book_payload_data()
    if mutation == "missing_book":
        data["settingBook"] = None
    elif mutation == "wrong_hash":
        data["settingBook"]["contentHash"] = "c" * 64
    elif mutation == "mixed":
        data["episode"] = _payload().episode.model_dump(by_alias=True, mode="json")
    elif mutation == "confirmed_only":
        data.update(analysisMode="CONFIRMED_ONLY", analysisContext=None)
    else:
        data["reviewMode"] = "MANUAL"
    with pytest.raises(ValidationError):
        WorkerAnalysisJobPayload.model_validate(data)


def test_legacy_episode_claim_defaults_to_episode_source():
    payload = _payload()
    assert payload.source_kind == "EPISODE"
    assert payload.setting_book is None


@pytest.mark.parametrize("mutation", ["different_revision", "missing_candidate_source", "episode_candidate"])
def test_book_character_batch_rejects_mixed_candidate_sources(mutation):
    from app.schemas.worker import WorkerCharacterFactComparisonBatchPayload
    from tests.test_character_fact_batch import _candidate
    data = {
        "comparisonBatchId": str(uuid4()), "workId": str(uuid4()), "sourceEpisodeId": None,
        "source": SOURCE, "characterRef": "K1", "matchedCharacterName": "아린",
        "canonicalFactType": "STATUS", "analysisContext": _input_context().model_dump(by_alias=True, mode="json"),
        "candidates": [_candidate("C1", "Q1", "다리가 아프다").model_dump(by_alias=True, mode="json")],
    }
    data["candidates"][0].update(sourceEpisodeNo=None, source=dict(SOURCE))
    if mutation == "different_revision":
        data["candidates"][0]["source"]["revisionId"] = str(uuid4())
    elif mutation == "missing_candidate_source":
        data["candidates"][0]["source"] = None
    else:
        data["candidates"][0]["source"] = None
        data["candidates"][0]["sourceEpisodeNo"] = 1
    with pytest.raises(ValidationError):
        WorkerCharacterFactComparisonBatchPayload.model_validate(data)


def test_book_provenance_is_known_without_episode_or_internal_ids_in_prompt():
    provenance = AnalysisStateProvenance.model_validate({
        "confirmationStatus": "PROVISIONAL", "sourceEpisodeNo": None,
        "sourceKind": "SETTING_BOOK", "sourceSettingBookId": str(BOOK_ID),
        "sourceSettingBookName": "인물 설정.docx", "sourceSettingBookRevisionId": str(REVISION_ID),
        "sourceSettingBookRevisionNo": 2, "sourceCandidateIds": [str(uuid4())],
    })
    result = ordered_provenance_data(provenance)
    assert result["source_kind"] == "SETTING_BOOK"
    assert result["source_setting_book_name"] == "인물 설정.docx"
    assert result["source_setting_book_revision_no"] == 2
    assert result["source_episode_no"] is None
    assert str(BOOK_ID) not in json.dumps(result)
    assert str(REVISION_ID) not in json.dumps(result)


@pytest.mark.parametrize("domain", ["characters", "world"])
def test_book_extraction_prompts_read_document_structure_without_episode_time(domain):
    client = RecordingClient({"candidates": []})
    book = WorkerAnalysisJobPayload.model_validate(book_payload_data()).setting_book
    kwargs = {"chunk_text": "| 인물 | 근력 |\n| 아린 | 10 |", "setting_book": book}
    if domain == "characters":
        extractor = CharacterSettingExtractor(llm_client=client, model="test-model")
        kwargs.update(source_chunk_id=CHUNK_ID, schema_hints=DEFAULT_SCHEMA_HINTS)
    else:
        extractor = WorldSettingExtractor(llm_client=client, model="test-model")
    asyncio.run(extractor.extract_from_chunk(**kwargs))
    request = client.requests[0]
    assert '"source_kind": "SETTING_BOOK"' in request["user_prompt"]
    assert '"revision_no": 2' in request["user_prompt"]
    assert "인물 설정.docx" in request["user_prompt"]
    assert '"episode_no"' not in request["user_prompt"]
    assert '"reference_time_kind": "UNSPECIFIED"' in request["user_prompt"]
    assert "표" in request["system_prompt"] and "목록" in request["system_prompt"]
    assert "미래" in request["system_prompt"]
    assert str(BOOK_ID) not in request["user_prompt"]
    assert str(REVISION_ID) not in request["user_prompt"]


def test_worker_claim_advertises_book_capability_without_changing_client_default():
    import httpx

    from app.clients.spring_worker_client import SpringWorkerClient
    from app.worker.analysis_job_worker import AnalysisJobWorker
    requests = []
    async def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(204)
    async def scenario():
        client = SpringWorkerClient("http://test.invalid", "test-only", httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        worker = AnalysisJobWorker(spring_client=client)
        try:
            await client.claim(allowed_job_types=["SETTING_EXTRACTION"])
            await worker.claim_next()
        finally:
            await worker.aclose()
            await client.aclose()
    asyncio.run(scenario())
    assert "supportedSourceKinds" not in requests[0]
    assert requests[1]["supportedSourceKinds"] == ["EPISODE", "SETTING_BOOK"]


def test_book_provisional_character_and_reference_can_feed_episode_one():
    from app.analysis.ordered_context import OrderedExtractionContext
    data = _payload().model_dump(by_alias=True, mode="json")
    data.update(analysisMode="ORDERED_PROVISIONAL", analysisContext=_input_context().model_dump(by_alias=True, mode="json"))
    data["episode"]["contentHash"] = "b" * 64
    data["provisionalCharacters"] = [{"provisionalSubjectKey": f"provisional-character:{uuid4()}",
        "name": "아린", "sourceEpisodeNo": None, "source": SOURCE}]
    data["analysisContext"]["unresolvedReferences"] = [{"domain": "characters", "subjectName": "아린",
        "sourceEpisodeNo": None, "source": SOURCE, "reason": "시점 확인이 필요합니다."}]
    payload = WorkerAnalysisJobPayload.model_validate(data)
    prompt = OrderedExtractionContext(payload.analysis_context, (), tuple(payload.provisional_characters)).prompt_data()
    assert prompt["characters"][0]["source"]["source_kind"] == "SETTING_BOOK"
    assert prompt["unresolved_references"][0]["source"]["setting_book_name"] == "인물 설정.docx"
    assert str(BOOK_ID) not in json.dumps(prompt)
    assert str(REVISION_ID) not in json.dumps(prompt)
    data["provisionalCharacters"][0]["source"] = None
    with pytest.raises(ValidationError):
        WorkerAnalysisJobPayload.model_validate(data)


def test_world_book_response_keeps_explicit_source_and_rejects_unknown_null_episode():
    from app.schemas.worker import WorkerWorldSettingSubjectResolutionCandidate
    data = {"candidateId": str(uuid4()), "sourceEpisodeId": None, "sourceEpisodeNo": None,
            "source": SOURCE, "category": "RACE", "subjectName": "엘프", "evidenceSpans": []}
    candidate = WorkerWorldSettingSubjectResolutionCandidate.model_validate(data)
    assert candidate.source.source_kind == "SETTING_BOOK"
    assert candidate.source_episode_id is None
    data["source"] = None
    with pytest.raises(ValidationError):
        WorkerWorldSettingSubjectResolutionCandidate.model_validate(data)
