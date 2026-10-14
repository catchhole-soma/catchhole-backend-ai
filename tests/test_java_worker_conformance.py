"""Parse DTOs exported by Java's actual HTTP JSON converter, not a Python fixture.

Run with GH221_JAVA_WORKER_CONFORMANCE=/path/to/java-worker-conformance.json.
An absent export is an explicit pending cross-runtime verification gate.
"""

import json
import os
from pathlib import Path

import pytest

from app.analysis.ordered_context import ordered_provenance_data
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import (
    WorkerAnalysisJobPayload,
    WorkerCharacterFactComparisonBatchContextResponse,
    WorkerCharacterFactComparisonBatchPayload,
    WorkerCharacterFactComparisonContextResponse,
    WorkerWorldSettingCandidatePayload,
    WorkerWorldSettingComparisonBatchContextResponse,
    WorkerWorldSettingComparisonBatchPayload,
    WorkerWorldSettingSubjectResolutionPendingResponse,
)

EXPORT_PATH = os.environ.get("GH221_JAVA_WORKER_CONFORMANCE")


def parse_java_worker_conformance(export: dict) -> dict:
    parsed = {
        "bookClaim": WorkerAnalysisJobPayload.model_validate(export["bookClaim"]),
        "episodeClaim": WorkerAnalysisJobPayload.model_validate(export["episodeClaim"]),
        "hiddenBookComparisonClaim": WorkerAnalysisJobPayload.model_validate(export["hiddenBookComparisonClaim"]),
        "worldCandidates": [WorkerWorldSettingCandidatePayload.model_validate(row) for row in export["worldCandidates"]],
        "worldSubjectPending": WorkerWorldSettingSubjectResolutionPendingResponse.model_validate(export["worldSubjectPending"]),
        "worldBatch": WorkerWorldSettingComparisonBatchPayload.model_validate(export["worldBatch"]),
        "worldBatchContext": WorkerWorldSettingComparisonBatchContextResponse.model_validate(export["worldBatchContext"]),
        "characterBatch": WorkerCharacterFactComparisonBatchPayload.model_validate(export["characterBatch"]),
        "characterBatchContext": WorkerCharacterFactComparisonBatchContextResponse.model_validate(export["characterBatchContext"]),
        "bookSource": WorkerAnalysisSource.model_validate(export["bookSource"]),
    }
    if "characterSingletonContext" in export:
        parsed["characterSingletonContext"] = WorkerCharacterFactComparisonContextResponse.model_validate(
            export["characterSingletonContext"]
        )
    return parsed


@pytest.mark.skipif(not EXPORT_PATH, reason="Real Java HTTP JSON export is not supplied.")
def test_actual_java_worker_payloads_match_python_contract():
    parsed = parse_java_worker_conformance(json.loads(Path(EXPORT_PATH).read_text(encoding="utf-8")))
    book = parsed["bookClaim"]
    episode = parsed["episodeClaim"]
    hidden = parsed["hiddenBookComparisonClaim"]
    assert book.source_kind == "SETTING_BOOK" and book.episode is None and book.setting_book is not None
    assert book.analysis_mode == "ORDERED_PROVISIONAL" and book.review_mode == "AUTOMATIC"
    assert episode.source_kind == "EPISODE" and episode.episode is not None and episode.setting_book is None
    assert hidden.source_kind == "SETTING_BOOK" and hidden.episode is None
    assert hidden.analysis_mode == "CONFIRMED_ONLY" and hidden.analysis_context is None
    assert parsed["bookSource"].source_kind == "SETTING_BOOK"
    assert parsed["worldCandidates"] and parsed["worldSubjectPending"].candidates
    world_batch = parsed["worldBatch"]
    character_batch = parsed["characterBatch"]
    assert world_batch.source.source_kind == character_batch.source.source_kind == "SETTING_BOOK"
    assert world_batch.source_episode_id is None and character_batch.source_episode_id is None
    assert parsed["worldBatchContext"].analysis_context == world_batch.analysis_context
    assert parsed["characterBatchContext"].analysis_context == character_batch.analysis_context
    assert parsed["characterBatchContext"].candidates == character_batch.candidates
    for domain, context in (("character", parsed["characterBatchContext"]), ("world", parsed["worldBatchContext"])):
        provenances = ([entry.provenance for entry in context.snapshot_entries]
            if hasattr(context, "snapshot_entries") else
            [prop.provenance for target in context.targets for prop in target.properties])
        known_books = [provenance for provenance in provenances
            if provenance is not None and provenance.source_kind == "SETTING_BOOK"]
        assert known_books, f"Actual Java {domain} CURRENT context must contain applied setting-book provenance."
        for provenance in known_books:
            projected = ordered_provenance_data(provenance)
            assert projected["source_kind"] == "SETTING_BOOK" and projected["source_episode_no"] is None
            assert str(provenance.source_setting_book_id) not in json.dumps(projected)


@pytest.mark.skipif(not EXPORT_PATH, reason="Real Java HTTP JSON export is not supplied.")
def test_actual_java_singleton_and_contexts_preserve_nonempty_current_source_and_protection_metadata():
    parsed = parse_java_worker_conformance(json.loads(Path(EXPORT_PATH).read_text(encoding="utf-8")))
    assert "characterSingletonContext" in parsed
    singleton = parsed["characterSingletonContext"]
    assert singleton.candidate.source.source_kind == "SETTING_BOOK"
    assert singleton.protected_slots and parsed["characterBatchContext"].protected_slots
    assert any(target.protected_paths for target in parsed["worldBatchContext"].targets)
    for domain, entries in (
        ("character singleton", singleton.snapshot_entries),
        ("character batch", parsed["characterBatchContext"].snapshot_entries),
        ("world", [prop for target in parsed["worldBatchContext"].targets for prop in target.properties]),
    ):
        books = [entry.provenance for entry in entries
            if entry.provenance is not None and entry.provenance.source_kind == "SETTING_BOOK"]
        assert books, f"Actual Java {domain} context must include applied book provenance."
        for provenance in books:
            assert provenance.current_source_times, f"Actual Java {domain} must retain CURRENT source times."
            projected = ordered_provenance_data(provenance)
            assert projected["current_source_times"]
            assert str(provenance.source_setting_book_id) not in json.dumps(projected)
