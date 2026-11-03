"""Per-role runtimes from claim configuration v2 reach the provider request; no paid calls (MockTransport)."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.llm.openai_client import OpenAIResponsesClient
from app.validation.contracts import BudgetState, IncompleteInspection, QuestionBatch
from app.validation.execution import ValidationQueryEmbedding, ValidationRoleClients
from app.validation.models import RoleRuntime, call_role
from tests.support.validation_synthetic import budget


class Ledger:
    def __init__(self):
        self.reservations = []
        self.query_input = self.query_request_id = None

    async def reserve_ai_tokens(self, **kwargs):
        self.reservations.append(kwargs)
        if kwargs["purpose"] == "VALIDATION_QUERY_EMBEDDING":
            self.query_request_id = str(kwargs["request_id"])

    async def settle_ai_tokens(self, **kwargs):
        pass

    async def release_ai_tokens(self, *args, **kwargs):
        pass


def test_each_generation_role_uses_its_own_model_effort_and_output_cap():
    bodies = []

    def provider(request):
        body = json.loads(request.content)
        bodies.append((request.url.path, body))
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 40})
        result = {"questions": [], "segments": [{"segment_ref": "TSEG1", "status": "NO_CHECK", "reason": "없음"}]}
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(result),
                                         "usage": {"input_tokens": 40, "output_tokens": 5}})

    runtimes = {"PLANNER": RoleRuntime.of("gpt-6.1-terra", "medium", 50000),
                "JUDGE": RoleRuntime.of("gpt-6-luna", "high", 20000),
                "TYPO": RoleRuntime.of("gpt-6-luna", "low", 12000)}
    ledger = Ledger()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            raw = OpenAIResponsesClient("synthetic", "default-model", "https://provider.invalid/responses",
                                        http_client=http)
            claim = SimpleNamespace(job_id=uuid4(), lease_token=uuid4())
            roles = ValidationRoleClients(raw, ledger, claim, runtimes)
            assert {role: client.default_model for role, client in roles.clients.items()} == {
                "PLANNER": "gpt-6.1-terra", "JUDGE": "gpt-6-luna", "TYPO": "gpt-6-luna"}
            state = BudgetState(budget())
            for role in ("PLANNER", "JUDGE"):
                await call_role(roles, role, {"segment": {"ref": "TSEG1"}}, QuestionBatch, state,
                                runtime=runtimes[role], schema_name=f"validation_{role.lower()}_test")

    asyncio.run(run())
    generated = [body for path, body in bodies if not path.endswith("input_tokens")]
    assert [(body["model"], body["reasoning"], body["max_output_tokens"]) for body in generated] == [
        ("gpt-6.1-terra", {"effort": "medium"}, 50000), ("gpt-6-luna", {"effort": "high"}, 20000)]
    assert [body["text"]["format"]["name"] for body in generated] == ["validation_planner_test",
                                                                      "validation_judge_test"]
    # Exact reservation per call: counted input plus that role's own output cap.
    assert [row["reserved_tokens"] for row in ledger.reservations] == [50040, 20040]
    assert [row["purpose"] for row in ledger.reservations] == ["VALIDATION_PLANNER", "VALIDATION_JUDGE"]


@pytest.mark.parametrize("change", [
    {"model": "bad model name"}, {"reasoning_effort": "max"}, {"max_output_tokens": 0}])
def test_invalid_role_runtime_is_rejected(change):
    values = {"model": "gpt-6-luna", "reasoning_effort": "low", "max_output_tokens": 10} | change
    with pytest.raises(ValueError):
        RoleRuntime.of(values["model"], values["reasoning_effort"], values["max_output_tokens"])


def test_parallel_query_embeddings_never_mix_their_reservation_identity():
    """Judges run concurrently; the query proof set on the ledger must belong to the query being reserved."""
    from app.retrieval.contracts import IndexSpec

    seen = []

    class Raw:
        model, version, dimensions = "text-embedding-3-small", "v1", 1536

        async def create_embeddings(self, inputs):
            await asyncio.sleep(0.01)
            return SimpleNamespace(model=self.model, embeddings=[[0.5] * 1536], input_token_count=3)

    class Proofs(Ledger):
        async def reserve_ai_tokens(self, **kwargs):
            seen.append(self.query_input["queryHash"])
            await asyncio.sleep(0.005)
            await super().reserve_ai_tokens(**kwargs)

    ledger = Proofs()
    claim = SimpleNamespace(job_id=uuid4(), lease_token=uuid4(), wire={"manifestHash": "c" * 64},
                            scope=SimpleNamespace(inputDigest="d" * 64))
    embedding = ValidationQueryEmbedding(Raw(), ledger, claim, IndexSpec())

    async def run():
        return await asyncio.gather(embedding.create_embedding("가"), embedding.create_embedding("나"))

    asyncio.run(run())
    from hashlib import sha256
    assert sorted(seen) == sorted(sha256(query.encode()).hexdigest() for query in ("가", "나"))
    assert embedding.last_receipt["queryHash"] in seen


def test_budget_exhaustion_from_a_role_call_is_not_a_model_failure():
    class Client:
        async def count_input_tokens(self, **kwargs):
            return 10

        async def create_text_response(self, **kwargs):
            raise AssertionError("not reached")

    state = BudgetState(budget(model_calls=1))
    state.consume("model")
    with pytest.raises(IncompleteInspection, match="BUDGET_EXHAUSTED"):
        asyncio.run(call_role(Client(), "JUDGE", {}, QuestionBatch, state,
                              runtime=RoleRuntime.of("gpt-6-luna", "medium", 10)))
