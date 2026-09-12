"""Deterministic request probe shared by the baseline git archive and current app."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import UUID

import httpx

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_name_resolver import ActiveCharacterStatus, KnownCharacter
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.analysis.world_setting_comparator import (
    WorldSettingComparator,
    WorldSettingSubjectResolver,
)
from app.core.config import Settings, get_settings
from app.llm.openai_client import OpenAIResponsesClient
from app.schemas.worker import (
    WorkerCharacterFactComparisonBatchCandidate,
    WorkerCharacterFactComparisonBatchSnapshotEntry,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonTarget,
    WorkerWorldSettingSubject,
)


def digest(value):
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def collect():
    Settings.model_config = {**Settings.model_config, "env_file": None}
    get_settings.cache_clear()
    requests = {}
    operation = "extraction"
    responses = [
        {"candidates": []},
        {
            "decisions": [
                {
                    "candidate_ref": "C1",
                    "resolved_canonical_fact_key": "status.부상",
                    "operation": "REMOVE",
                    "target_ref": None,
                    "removed_snapshot_refs": ["P1"],
                    "proposed_fact_value": None,
                    "proposed_value_json": None,
                    "temporal_scope": "PRESENT",
                    "comparison_reason": "원문에서 부상이 회복되었다.",
                }
            ]
        },
        {"selected_subject_refs": ["S1"]},
        {
            "decisions": [
                {
                    "source_candidate_refs": ["C1"],
                    "consolidation_status": "SINGLE",
                    "operation": "UPDATE",
                    "target_ref": "T1",
                    "matched_scope_name": None,
                    "matched_property_name": "색상",
                    "proposed_scope_name": None,
                    "proposed_setting_name": "색상",
                    "proposed_value": "푸른색",
                    "comparison_reason": "원문에서 백탑의 색상이 바뀌었다.",
                }
            ]
        },
    ]

    def transport(request):
        body = json.loads(request.content)
        assert operation not in requests, "A supposedly successful request retried."
        requests[operation] = {
            "sha256": digest(body),
            "keys": sorted(body),
            "model": body["model"],
            "reasoning": body.get("reasoning"),
            "store": body["store"],
            "max_output_tokens": body["max_output_tokens"],
            "prompt_cache_key": body.get("prompt_cache_key"),
            "system_sha256": digest(body["input"][0]),
            "user_sha256": digest(body["input"][1]),
            "schema_sha256": digest(body.get("text")),
        }
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(responses.pop(0), ensure_ascii=False),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = OpenAIResponsesClient(
            api_key="synthetic-test-only",
            model="gpt-5.6-terra",
            responses_api_url="https://provider.test.invalid/responses",
            reasoning_effort="none",
            http_client=http,
        )
        await CharacterSettingExtractor(
            llm_client=client, model="gpt-5.6-terra", max_attempts=1
        ).extract_from_chunk(
            UUID(int=1),
            "세룸은 부상이 회복되어 일어섰다.",
            episode_no=5,
            episode_title="회복",
            schema_hints=(
                CharacterSettingSchemaHint("statuses.condition", "상태", "status.*", (), "JSON"),
            ),
            known_characters=(
                KnownCharacter(
                    UUID(int=2), "세룸", (ActiveCharacterStatus("status.부상", "다리를 다침"),)
                ),
            ),
        )
        operation = "character_batch"
        await CharacterFactComparator(
            llm_client=client, model="gpt-5.6-luna", max_attempts=1
        ).compare_batch(
            matched_character_name="세룸",
            canonical_fact_type="STATUS",
            candidates=[
                WorkerCharacterFactComparisonBatchCandidate.model_validate(
                    {
                        "candidateRef": "C1",
                        "projectedSnapshotRef": "Q1",
                        "sourceEpisodeNo": 5,
                        "rawFactKey": "status.부상",
                        "initialCanonicalFactKey": "status.부상",
                        "canonicalKeyResolution": "PATTERN",
                        "attributeValue": "부상이 회복되었다.",
                        "valueType": "JSON",
                        "valueJson": {"name": "부상", "active": False},
                        "evidenceSpans": [{"quote": "세룸은 부상이 회복되어 일어섰다."}],
                        "confidence": 0.95,
                    }
                )
            ],
            snapshot_entries=[
                WorkerCharacterFactComparisonBatchSnapshotEntry.model_validate(
                    {
                        "snapshotRef": "P1",
                        "origin": "PERSISTED",
                        "sourceCandidateRef": None,
                        "dependencyCandidateRefs": [],
                        "factType": "STATUS",
                        "factKey": "status.부상",
                        "factValue": "다리를 다침",
                        "valueJson": {"active": True},
                    }
                )
            ],
        )
        world = WorkerWorldSettingCandidatePayload.model_validate(
            {
                "candidateId": str(UUID(int=3)),
                "workId": str(UUID(int=4)),
                "sourceEpisodeId": str(UUID(int=5)),
                "category": "LOCATION",
                "subjectName": "백탑",
                "scopeName": None,
                "settingName": "색상",
                "extractedValue": "푸른색",
                "evidenceSpans": [{"quote": "백탑은 푸른색으로 바뀌었다."}],
                "extractionConfidence": 0.95,
            }
        )
        operation = "world_subject"
        await WorldSettingSubjectResolver(
            llm_client=client, model="gpt-5.6-luna", max_attempts=1
        ).select_subjects(
            world, [WorkerWorldSettingSubject(world_setting_id=UUID(int=6), subject_name="백탑")]
        )
        operation = "world_batch"
        await WorldSettingComparator(
            llm_client=client, model="gpt-5.6-luna", max_attempts=1
        ).compare_batch(
            category="LOCATION",
            candidates=[
                WorkerWorldSettingComparisonBatchCandidate.model_validate(
                    {
                        "candidateRef": "C1",
                        "candidateId": str(world.candidate_id),
                        "subjectName": world.subject_name,
                        "scopeName": None,
                        "settingName": world.setting_name,
                        "extractedValue": world.extracted_value,
                        "evidenceSpans": [
                            span.model_dump(by_alias=True) for span in world.evidence_spans
                        ],
                        "extractionConfidence": 0.95,
                    }
                )
            ],
            targets=[
                WorkerWorldSettingComparisonTarget.model_validate(
                    {
                        "worldSettingId": str(UUID(int=6)),
                        "subjectName": "백탑",
                        "version": 3,
                        "properties": [{"scopeName": None, "settingName": "색상", "value": "흰색"}],
                    }
                )
            ],
        )
    assert not responses
    return requests


if __name__ == "__main__":
    import app

    assert Path(app.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()), (
        "Wrong app checkout imported."
    )
    print(json.dumps(asyncio.run(collect()), ensure_ascii=False, sort_keys=True))
