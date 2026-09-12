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
    # The immutable historical capture stays intact. Only the explicitly requested
    # plain-language and subject-identity instructions have a separate expectation.
    upstream = json.loads((ROOT / "tests/fixtures/upstream_requests_pr65.json").read_text())
    assert upstream["baselineCommit"] == "275bada00fcd4d420277d378ddf7f6d417dea899"
    assert upstream["probeHashes"][PROBE.name] == sha256(PROBE.read_bytes()).hexdigest()
    expected = upstream["requests"][purpose]
    # A new upstream prompt/cache is authorized, but user data, schema, model,
    # reasoning, store flag, request keys and output limits remain unchanged.
    changing = {"sha256", "system_sha256", "prompt_cache_key"}
    assert {k:v for k,v in expected.items() if k not in changing} == {
        k:v for k,v in baseline[purpose].items() if k not in changing
    }
    authorized = upstream["gh180Overrides"].get(purpose, {})
    assert set(authorized) <= changing
    assert actual[purpose] == {**expected, **authorized}
