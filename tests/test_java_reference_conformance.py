"""Frozen reference/hash conformance against actual Java HTTP + state exports."""

import os
from pathlib import Path

import pytest

from app.schemas.worker import WorkerAnalysisJobPayload
from evals.multi_stage_setting.ordered_journal import journal_state_hash, load_journal_json

EXPORT_PATH = os.environ.get("GH221_JAVA_REFERENCE_WIRE")


@pytest.mark.skipif(not EXPORT_PATH, reason="Actual Java reference-time wire export is not supplied.")
def test_actual_java_reference_revision_and_canonical_input_hash_match_python():
    export = load_journal_json(Path(EXPORT_PATH).read_text(encoding="utf-8"))
    before = WorkerAnalysisJobPayload.model_validate(export["originalBookClaim"])
    after = WorkerAnalysisJobPayload.model_validate(export["referenceChangedBookClaim"])
    assert before.frozen_source().source_kind == after.frozen_source().source_kind == "SETTING_BOOK"
    assert before.episode is after.episode is None
    assert before.setting_book.reference_time_kind == "EPISODE"
    assert before.setting_book.reference_episode_no == 30
    assert after.setting_book.reference_time_kind == "WORK_START"
    assert after.setting_book.reference_episode_no is None
    assert before.setting_book.content_hash == after.setting_book.content_hash
    assert before.setting_book.revision_id != after.setting_book.revision_id
    assert before.frozen_source().identity() != after.frozen_source().identity()
    assert before.analysis_context.input_state_hash != after.analysis_context.input_state_hash
    for key, claim in (("originalInputState", before), ("referenceChangedInputState", after)):
        assert journal_state_hash(export[key]) == claim.analysis_context.input_state_hash
        assert claim.analysis_context.source_hash == claim.setting_book.content_hash
        assert claim.analysis_context.reference_time_kind == claim.setting_book.reference_time_kind
        assert claim.analysis_context.reference_episode_no == claim.setting_book.reference_episode_no
    assert export["inputManifest"]["sourceReferenceTimeKind"] == "WORK_START"
    assert export["journal"]["sourceReferenceTimeKind"] == "WORK_START"
    assert export["journal"]["episodeNo"] is None
    assert export["journal"]["inputStateHash"] == after.analysis_context.input_state_hash
