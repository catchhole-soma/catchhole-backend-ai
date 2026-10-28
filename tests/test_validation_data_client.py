import asyncio
from hashlib import sha256
import json
from uuid import uuid4

import httpx
import pytest


def test_frozen_http_reader_preserves_unicode_source_identity_and_safe_failure():
    from app.validation.data import ValidationContext, ValidationDataClient
    from app.validation.contracts import IncompleteInspection

    target = "😀민수는 쉬었다."
    digest = sha256(target.encode()).hexdigest()
    context = ValidationContext(7, uuid4(), uuid4(), "b" * 64, digest)
    captured = []
    changed = False

    def handler(request):
        captured.append((request, json.loads(request.content)))
        body = captured[-1][1]
        result = dict(formatVersion=1, reportId=str(context.report_id), baselineRevision=1,
                      baselineHash=context.baseline_hash, targetHash=digest,
                      manifestHash="c" * 64, status="READY")
        if body["operation"] == "INPUT":
            result.update(target=dict(sourceRef="T1", sourceKind="EPISODE", episodeId=str(uuid4()),
                                      episodeNo=1, contentHash=digest, contentS3Key="private/key",
                                      contentS3Version="v1"), sources=[], entryCount=0)
        if body["operation"] == "READ_SOURCE":
            result.update(sourceRef="T1", contentHash=digest, start=0, end=len(target),
                          totalCodePoints=len(target), text=target)
        if changed:
            result["targetHash"] = "a" * 64
        return httpx.Response(200, json={"data": result})

    async def run():
        nonlocal changed
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = ValidationDataClient("http://spring.invalid", "synthetic-internal", context, http)
            data = await client.read_input()
            source = await client.read_source(data.target)
            assert source.text == target
            assert source.end == 9
            assert data.sources == ()  # first episode is an actual empty prior set
            changed = True
            with pytest.raises(IncompleteInspection, match="INPUT_CHANGED"):
                await client.validate()

    asyncio.run(run())
    assert all(request.headers["X-Validation-Member-Id"] == "7" for request, body in captured)
    assert all(body["expectedBaselineHash"] == "b" * 64 for request, body in captured)
    assert captured[1][1]["expectedSourceHash"] == digest
    assert captured[1][1]["expectedSourceVersion"] == "v1"


def test_typo_result_keeps_zero_suggestions_separate_from_unprocessed_span():
    from app.validation.contracts import TypoCheckResult, SegmentCoverage
    completed = TypoCheckResult(target_hash="a" * 64, status="COMPLETED", suggestions=[],
                               segments=[SegmentCoverage(segment_ref="TSEG1", status="NO_CHECK", reason="오타 없음")], failure_code=None)
    assert completed.suggestions == []
    with pytest.raises(ValueError):
        completed.model_copy(update={"segments": []}).require_coverage(["TSEG1"])
    incomplete = completed.model_copy(update={"status": "INCOMPLETE", "failure_code": "SOURCE_UNREADABLE"})
    assert incomplete.status != completed.status


@pytest.mark.parametrize("override,explicit_partial", [
    ({"start": 1, "end": 3, "text": "민수"}, False),
    ({"start": 1, "end": 3, "text": ["민", "수"]}, True),
    ({"start": True}, False),
    ({"end": 8, "text": "😀민수는 쉬었다"}, False),
    ({"text": "😀민수는 걸었다."}, False),
])
def test_source_contract_rejects_partial_full_read_and_malformed_text(override, explicit_partial):
    from app.validation.data import ValidationContext, ValidationDataClient, SourceDocument
    from app.validation.contracts import IncompleteInspection
    text = "😀민수는 쉬었다."
    digest = sha256(text.encode()).hexdigest()
    context = ValidationContext(1, uuid4(), uuid4(), "b"*64, digest)
    source = SourceDocument("T1", "EPISODE", digest, "v1", {})
    result = dict(formatVersion=1, reportId=str(context.report_id), baselineRevision=1,
                  baselineHash=context.baseline_hash, targetHash=digest, status="READY",
                  sourceRef="T1", contentHash=digest, start=0, end=9, totalCodePoints=9, text=text)
    result.update(override)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": result})
        )) as http:
            client = ValidationDataClient("http://spring.invalid", "synthetic", context, http)
            with pytest.raises(IncompleteInspection, match="INVALID_SOURCE_CONTRACT"):
                await client.read_source(source, 1, 3) if explicit_partial else await client.read_source(source)
    asyncio.run(run())


@pytest.mark.parametrize("envelope", [None, [], {"data": None}, {"data": []}, {"data": "raw"}])
def test_malformed_json_envelope_is_a_typed_incomplete_failure(envelope):
    from app.validation.data import ValidationContext, ValidationDataClient
    from app.validation.contracts import IncompleteInspection
    context = ValidationContext(1, uuid4(), uuid4(), "b"*64, "a"*64)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=envelope)
        )) as http:
            client = ValidationDataClient("http://spring.invalid", "synthetic", context, http)
            with pytest.raises(IncompleteInspection):
                await client.read_input()
    asyncio.run(run())


def test_explicit_partial_and_default_full_read_controls():
    from app.validation.data import ValidationContext, ValidationDataClient, SourceDocument
    text = "😀민수는 쉬었다."
    digest = sha256(text.encode()).hexdigest()
    context = ValidationContext(1, uuid4(), uuid4(), "b"*64, digest)
    source = SourceDocument("T1", "EPISODE", digest, "v1", {})
    def handler(request):
        body = json.loads(request.content)
        start = body.get("start") or 0
        end = body.get("end") or 9
        return httpx.Response(200, json={"data": dict(formatVersion=1, reportId=str(context.report_id),
            baselineRevision=1, baselineHash=context.baseline_hash, targetHash=digest, status="READY",
            sourceRef="T1", contentHash=digest, start=start, end=end, totalCodePoints=9, text=text[start:end])})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = ValidationDataClient("http://spring.invalid", "synthetic", context, http)
            assert (await client.read_source(source)).text == text
            assert (await client.read_source(source, 1, 3)).text == "민수"
    asyncio.run(run())
