"""The two PR65 world calls absent from the immutable historical probe."""
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
    expected = json.loads((ROOT / "tests/fixtures/upstream_requests_pr65.json").read_text())
    assert expected["baselineCommit"] == "275bada00fcd4d420277d378ddf7f6d417dea899"
    assert expected["probeHashes"][PROBE.name] == sha256(PROBE.read_bytes()).hexdigest()
    actual = subprocess.run([sys.executable, str(PROBE), str(ROOT)], cwd=ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, check=True, timeout=15)
    return json.loads(actual.stdout), expected


@pytest.mark.parametrize("purpose", ["world_extraction", "world_singleton"])
def test_world_request_keeps_upstream_input_and_transport(purpose, captured):
    actual, expected = captured
    overrides = expected["gh180Overrides"].get(purpose, {})
    assert set(overrides) <= {"sha256", "system_sha256", "prompt_cache_key"}
    assert actual[purpose] == {**expected["requests"][purpose], **overrides}
