"""The two main-baseline world calls absent from the immutable historical probe."""
import json
import os
from hashlib import sha256
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests/upstream_world_request_probe.py"


@pytest.fixture(scope="module")
def captured():
    expected = json.loads((ROOT / "tests/fixtures/upstream_requests_main_4ed7e55.json").read_text())
    assert expected["baselineCommit"] == "4ed7e5556a91641c320c9e55dc646d4f17b0648d"
    assert expected["probeHashes"][PROBE.name] == sha256(PROBE.read_bytes()).hexdigest()
    actual = subprocess.run([sys.executable, str(PROBE), str(ROOT)], cwd=ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, check=True, timeout=15)
    return json.loads(actual.stdout), expected


@pytest.mark.parametrize("purpose", ["world_extraction", "world_singleton"])
def test_world_request_keeps_upstream_input_and_transport(purpose, captured):
    actual, expected = captured
    overrides = {**expected["gh180Overrides"].get(purpose, {}),
                 **json.loads((ROOT / "tests/fixtures/world_semantic_scope_request_overrides.json").read_text())["overrides"].get(purpose, {})}
    assert set(overrides) <= {"sha256", "system_sha256", "prompt_cache_key"}
    if purpose == "world_extraction":
        # Keep the latest main extraction request exactly, including PR65.
        assert overrides == {}
    policy_overrides = json.loads((ROOT / "tests/fixtures/pr1_conflict_policy_request_overrides.json").read_text())["overrides"]
    assert all(set(row) == {"sha256", "system_sha256", "prompt_cache_key"} for row in policy_overrides.values())
    overrides = {**overrides, **policy_overrides.get(purpose, {})}
    task2_overrides = json.loads((ROOT / "tests/fixtures/pr1_task2_contract_request_overrides.json").read_text())["overrides"]
    assert all(set(row) == {"sha256", "system_sha256", "prompt_cache_key"} for row in task2_overrides.values())
    overrides = {**overrides, **task2_overrides.get(purpose, {})}
    general_rule_overrides = json.loads((ROOT / "tests/fixtures/pr1_explicit_general_rule_request_overrides.json").read_text())["overrides"]
    assert set(general_rule_overrides) == {"world_batch", "world_singleton"}
    assert all(set(row) == {"sha256", "system_sha256", "prompt_cache_key"} for row in general_rule_overrides.values())
    overrides = {**overrides, **general_rule_overrides.get(purpose, {})}
    transition_overrides = json.loads((ROOT / "tests/fixtures/pr221_world_property_transition_request_overrides.json").read_text())["overrides"]
    assert set(transition_overrides) == {"world_extraction", "world_singleton", "world_batch"}
    assert all(set(row) == {"sha256", "system_sha256", "prompt_cache_key"} for row in transition_overrides.values())
    overrides = {**overrides, **transition_overrides.get(purpose, {})}
    integration_overrides = json.loads((ROOT / "tests/fixtures/pr221_main_integration_request_overrides_20261015.json").read_text())["overrides"]
    assert set(integration_overrides) == {"extraction", "world_batch", "world_singleton"}
    assert all(set(integration_overrides[key]) == {"sha256", "system_sha256", "prompt_cache_key"}
               for key in ("world_batch", "world_singleton"))
    overrides = {**overrides, **integration_overrides.get(purpose, {})}
    final_integration = json.loads((ROOT / "tests/fixtures/pr221_final_main_integration_request_overrides_20261015.json").read_text())["overrides"]
    assert set(final_integration) == {"world_extraction", "world_singleton", "world_batch"}
    assert all(set(row) == {"sha256", "system_sha256", "prompt_cache_key"} for row in final_integration.values())
    overrides = {**overrides, **final_integration.get(purpose, {})}
    extraction_budget_overrides = json.loads((ROOT / "tests/fixtures/extraction_output_cap_request_overrides_20261016.json").read_text())["overrides"]
    assert set(extraction_budget_overrides) == {"extraction", "world_extraction"}
    assert all(set(row) == {"sha256", "max_output_tokens"} for row in extraction_budget_overrides.values())
    overrides = {**overrides, **extraction_budget_overrides.get(purpose, {})}
    world_prompt_overrides = json.loads((ROOT / "tests/fixtures/world_extraction_scope_request_overrides_20261017.json").read_text())["overrides"]
    assert set(world_prompt_overrides) == {"world_extraction"}
    assert set(world_prompt_overrides["world_extraction"]) == {"sha256", "system_sha256", "prompt_cache_key"}
    overrides = {**overrides, **world_prompt_overrides.get(purpose, {})}
    assert actual[purpose] == {**expected["requests"][purpose], **overrides}
