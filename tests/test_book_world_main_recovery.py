"""Main's atomic salvage must keep BOOK chronology and current protection."""

import asyncio
import json

import pytest

from app.analysis.world_setting_batch_recovery import compare_world_batch_with_recovery
from tests.test_setting_book_reference_time import book_source, current_entry
from tests.test_world_comparison_semantic_recovery import Client, candidate, comparator, decision, target


@pytest.mark.parametrize("ordered", [False, True])
@pytest.mark.parametrize("guard", ["chronology", "protected_current", "protected_absent"])
def test_shared_recovery_checks_book_guard_initially_and_on_retry_without_losing_sibling(ordered, guard):
    source = book_source("EPISODE", 8)
    sources = [candidate("C1", "조건", "바뀐 값"), candidate("C2", "교육", "독립 교육")]
    sources = [type(item).model_validate({**item.model_dump(), "temporal_scope": "PRESENT",
        "raw_extraction_json": {"epistemic_status": "ASSERTED"}}) for item in sources]
    existing = target([{"setting_name": "조건", "value": "기존 값"}])
    provenance = current_entry(protected=guard == "protected_current").provenance
    if guard == "chronology":
        provenance = type(provenance).model_validate({**provenance.model_dump(), "source_episode_no": 2,
            "current_source_times": [
                {"sourceKind": "EPISODE", "sourceEpisodeNo": 2, "temporalScope": "PRESENT"},
                {"sourceKind": "EPISODE", "sourceEpisodeNo": 9, "temporalScope": "PRESENT"},
            ]})
    existing.properties[0].provenance = provenance
    if guard == "protected_absent":
        existing = type(existing).model_validate({**existing.model_dump(), "properties": [], "protected_paths": [{
            "scope_name": None, "setting_name": "조건"}]})
    blocked = decision(["C1"], "조건", "바뀐 값", operation="ADD" if guard == "protected_absent" else "UPDATE",
        matched=None if guard == "protected_absent" else ("T1.P1" if ordered else "조건"), ordered=ordered)
    independent = decision(["C2"], "교육", "독립 교육", ordered=ordered)
    client = Client([{"decisions": [blocked, independent]}, {"decisions": [blocked]}])
    before = ([item.model_dump() for item in sources], existing.model_dump())

    result, _ = asyncio.run(compare_world_batch_with_recovery(comparator(client), "WORLD_RULE_HISTORY",
        sources, [existing], ordered_context=ordered, analysis_source=source))

    assert [row.source_candidate_refs for row in result.decisions] == [["C2"]]
    assert result.decisions[0].operation == "ADD"
    assert [row.source_candidate_refs for row in result.failures] == [["C1"]]
    assert result.failures[0].failure_code == "COMPARISON_VALIDATION_FAILED"
    assert len(client.requests) == 2
    prompts = [json.loads(request["user_prompt"]) for request in client.requests]
    assert [row["ref"] for row in prompts[1]["candidates"]] == ["C1"]
    assert all(prompt["source"] == source.prompt_data() for prompt in prompts)
    assert ([item.model_dump() for item in sources], existing.model_dump()) == before
