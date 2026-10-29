"""Java-owned golden wire and exact query identity; no provider or database access."""
import asyncio
import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.validation.checkpoint import SemanticState
from app.validation.client import ValidationClaim
from app.validation.contracts import IncompleteInspection
from app.validation.execution import claim_budgets
from app.worker.episode_validation_worker import EpisodeValidationWorker


def fixture():
    return json.loads((Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json").read_text())["execution"]


def test_exact_common_claim_budget_and_typed_checkpoint():
    wire = fixture()
    claim = ValidationClaim.from_wire(wire["responses"]["claim"])
    assert claim_budgets(claim)["SETTING"].model_calls == 20
    state = SemanticState.model_validate(wire["requests"]["checkpoint"]["payload"])
    assert state.inputDigest == claim.scope.inputDigest
    for sample in wire["queryEmbeddingIdentity"]["vectors"]:
        assert sha256(sample["query"].encode()).hexdigest() == sample["sha256"]
    claim.wire["budgets"]["roles"].pop("COVERAGE")
    with pytest.raises(IncompleteInspection, match="EXECUTION_BUDGET_CONFIGURATION_INVALID"):
        claim_budgets(claim)


def test_runtime_is_disabled_and_cli_refuses_missing_explicit_opt_in():
    with pytest.raises(ValueError, match="disabled"):
        EpisodeValidationWorker(None, None)
    from scripts.run_validation_worker import parser, run
    args = parser().parse_args(["--backend-url", "https://never-called.invalid", "--worker-id", "fixture"])
    with pytest.raises(ValueError, match="OFF"):
        asyncio.run(run(args))
