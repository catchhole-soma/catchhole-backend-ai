"""Compare default-mode HTTP requests with an actual pre-change git archive capture."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests/legacy_request_probe.py"
GOLDEN = ROOT / "tests/fixtures/legacy_requests_80d5184.json"


@pytest.fixture(scope="module")
def captured_requests():
    golden = json.loads(GOLDEN.read_text())
    assert golden["baselineCommit"] == "80d5184c006a42248ab214b36d0898c071887900"
    assert sha256(PROBE.read_bytes()).hexdigest() == golden["probeSha256"], (
        "Changed probe inputs require a new capture from the recorded baseline commit."
    )
    result = subprocess.run(
        [sys.executable, str(PROBE), str(ROOT)],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout), golden["requests"]


@pytest.mark.parametrize(
    "purpose", ["extraction", "character_batch", "world_subject", "world_batch"]
)
def test_confirmed_only_http_request_preserves_upstream_transport_and_authorized_prompts(purpose, captured_requests):
    actual, baseline = captured_requests
    # Whole HTTP JSON plus separate system/user/schema digests and explicit model,
    # reasoning, store flag, cache key, output limit and key presence diagnostics.
    overrides = json.loads((ROOT / "tests/fixtures/legacy_reason_language_overrides.json").read_text())["overrides"]
    assert set(overrides) == {"character_batch", "world_batch"}
    identity_overrides = json.loads((ROOT / "tests/fixtures/legacy_subject_identity_overrides.json").read_text())["overrides"]
    assert set(identity_overrides) == {"world_subject"}
    overrides = {**overrides, **identity_overrides}
    assert all(set(row) == {"sha256", "system_sha256"} for row in overrides.values())
    state_scope_overrides = json.loads(
        (ROOT / "tests/fixtures/character_state_scope_request_overrides.json").read_text()
    )["overrides"]
    assert set(state_scope_overrides) == {"extraction"}
    assert set(state_scope_overrides["extraction"]) == {
        "sha256", "system_sha256", "prompt_cache_key"
    }
    # Historical captures stay intact; authorized prompt changes are separate.
    upstream = json.loads((ROOT / "tests/fixtures/upstream_requests_main_4ed7e55.json").read_text())
    assert upstream["baselineCommit"] == "4ed7e5556a91641c320c9e55dc646d4f17b0648d"
    assert upstream["probeHashes"][PROBE.name] == sha256(PROBE.read_bytes()).hexdigest()
    expected = upstream["requests"][purpose]
    # A new upstream prompt/cache is authorized, but user data, schema, model,
    # reasoning, store flag, request keys and output limits remain unchanged.
    changing = {"sha256", "system_sha256", "prompt_cache_key"}
    assert {k:v for k,v in expected.items() if k not in changing} == {
        k:v for k,v in baseline[purpose].items() if k not in changing
    }
    authorized = {
        **upstream["gh180Overrides"].get(purpose, {}),
        **state_scope_overrides.get(purpose, {}),
        **json.loads((ROOT / "tests/fixtures/world_semantic_scope_request_overrides.json").read_text())["overrides"].get(purpose, {}),
    }
    assert set(authorized) <= changing
    policy_overrides = json.loads((ROOT / "tests/fixtures/pr1_conflict_policy_request_overrides.json").read_text())["overrides"]
    assert set(policy_overrides) == {"character_batch", "world_batch", "world_singleton"}
    assert all(set(row) == changing for row in policy_overrides.values())
    authorized = {**authorized, **policy_overrides.get(purpose, {})}
    task2_overrides = json.loads((ROOT / "tests/fixtures/pr1_task2_contract_request_overrides.json").read_text())["overrides"]
    assert set(task2_overrides) == set(policy_overrides)
    assert all(set(row) == changing for row in task2_overrides.values())
    authorized = {**authorized, **task2_overrides.get(purpose, {})}
    qualifier_overrides = json.loads((ROOT / "tests/fixtures/pr1_h_role_start_request_overrides.json").read_text())["overrides"]
    assert set(qualifier_overrides) == {"extraction", "character_batch"}
    assert all(set(row) == changing for row in qualifier_overrides.values())
    authorized = {**authorized, **qualifier_overrides.get(purpose, {})}
    undecided_overrides = json.loads((ROOT / "tests/fixtures/pr1_undecided_state_request_overrides.json").read_text())["overrides"]
    assert set(undecided_overrides) == {"character_batch"}
    assert all(set(row) == changing for row in undecided_overrides.values())
    authorized = {**authorized, **undecided_overrides.get(purpose, {})}
    general_rule_overrides = json.loads((ROOT / "tests/fixtures/pr1_explicit_general_rule_request_overrides.json").read_text())["overrides"]
    assert set(general_rule_overrides) == {"world_batch", "world_singleton"}
    assert all(set(row) == changing for row in general_rule_overrides.values())
    authorized = {**authorized, **general_rule_overrides.get(purpose, {})}
    negative_fact_overrides = json.loads((ROOT / "tests/fixtures/pr1_explicit_negative_fact_request_overrides.json").read_text())["overrides"]
    assert set(negative_fact_overrides) == {"character_batch"}
    assert all(set(row) == changing for row in negative_fact_overrides.values())
    authorized = {**authorized, **negative_fact_overrides.get(purpose, {})}
    narrative_overrides = json.loads((ROOT / "tests/fixtures/narrative_time_request_overrides_20261013.json").read_text())["overrides"]
    assert set(narrative_overrides) == {"extraction", "character_batch"}
    assert set(narrative_overrides["extraction"]) == changing | {"schema_sha256"}
    assert set(narrative_overrides["character_batch"]) == changing
    authorized = {**authorized, **narrative_overrides.get(purpose, {})}
    # Preserve historical captures; this task authorizes only the batch output budget.
    budget_overrides = json.loads((ROOT / "tests/fixtures/character_token_budget_request_overrides_20261014.json").read_text())["overrides"]
    assert set(budget_overrides) == {"character_batch"}
    assert set(budget_overrides["character_batch"]) == {"sha256", "max_output_tokens"}
    authorized = {**authorized, **budget_overrides.get(purpose, {})}
    integration_overrides = json.loads((ROOT / "tests/fixtures/pr221_main_integration_request_overrides_20261015.json").read_text())["overrides"]
    assert set(integration_overrides) == {"extraction", "world_batch", "world_singleton"}
    assert set(integration_overrides["extraction"]) == {"sha256", "system_sha256"}
    assert all(set(integration_overrides[key]) == changing for key in ("world_batch", "world_singleton"))
    authorized = {**authorized, **integration_overrides.get(purpose, {})}
    assert actual[purpose] == {**expected, **authorized}
