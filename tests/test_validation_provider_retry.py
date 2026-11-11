import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel

from app.llm.openai_client import OpenAIResponsesClient
from app.validation import provider_retry as retry
from app.validation.client import ExecutionGate
from app.validation.contracts import BudgetState, IncompleteInspection, InvocationBudget
from app.validation.execution import ValidationRoleClients
from app.validation.models import RoleRuntime, call_role


class Answer(BaseModel):
    value: int


class Ledger:
    def __init__(self):
        self.reserved, self.settled, self.unknown = [], [], []
        self.gate = ExecutionGate()

    async def reserve_ai_tokens(self, **kwargs):
        self.gate.require()
        self.reserved.append(kwargs)

    async def settle_ai_tokens(self, **kwargs):
        self.settled.append(kwargs)

    async def release_ai_tokens(self, request_id, outcome):
        self.unknown.append({"request_id": request_id, "outcome": outcome})


def budget(calls=10, seconds=30):
    return BudgetState(InvocationBudget(calls, 10, seconds, 1000, 1000, 0, 10))


@pytest.fixture
def delays(monkeypatch):
    values = []

    async def sleep(delay):
        values.append(delay)

    monkeypatch.setattr(retry.asyncio, 'sleep', sleep)
    monkeypatch.setattr(retry.random, 'uniform', lambda *a: 0.25)
    return values


def run_provider(provider, ledger=None, limits=None, role='JUDGE'):
    ledger = ledger or Ledger()
    limits = limits or budget()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            raw = OpenAIResponsesClient('SECRET_KEY', 'gpt-6-luna',
                                        'https://private.invalid/responses', http_client=http)
            runtime = RoleRuntime.of('gpt-6-luna', 'medium', 100)
            claim = SimpleNamespace(job_id=uuid4(), lease_token=uuid4())
            roles = ValidationRoleClients(raw, ledger, claim, {role: runtime},
                                          request_semaphore=asyncio.Semaphore(1))
            if role == 'TYPO':
                from app.validation.typo import _inspect
                return await _inspect(roles.for_validation_role(role), limits,
                                      'PRIVATE_MANUSCRIPT', runtime)
            result = await call_role(roles, 'JUDGE', {'source': 'PRIVATE_MANUSCRIPT'},
                                     Answer, limits, runtime=runtime)
            return result
    return asyncio.run(run()), ledger, limits


def success(request):
    if request.url.path.endswith('/input_tokens'):
        return httpx.Response(200, json={'input_tokens': 20})
    return httpx.Response(200, json={'status': 'completed', 'output_text': '{"value": 1}',
                                    'usage': {'input_tokens': 20, 'output_tokens': 5}})


def test_generation_retry_preserves_failed_usage_and_new_reservation(delays, caplog):
    calls = []

    def provider(request):
        calls.append(request.url.path)
        if calls.count('/responses') == 1 and request.url.path == '/responses':
            return httpx.Response(503, headers={'x-request-id': 'req_test123', 'retry-after': '3'},
                                  json={'error': {'code': 'server_is_overloaded',
                                                 'message': 'PRIVATE_MANUSCRIPT'}})
        return success(request)

    result, ledger, limits = run_provider(provider)
    assert result.value == 1 and limits.model_calls == 2
    assert len(ledger.reserved) == 2 and len(ledger.unknown) == len(ledger.settled) == 1
    assert ledger.reserved[0]['request_id'] != ledger.reserved[1]['request_id']
    assert delays == [3.25]
    assert 'status=503' in caplog.text and 'request_id=req_test123' in caplog.text
    assert all(x not in caplog.text for x in ['PRIVATE_MANUSCRIPT', 'SECRET_KEY', 'private.invalid'])


@pytest.mark.parametrize('path', ['/responses', '/responses/input_tokens'])
def test_typo_retries_transient_provider_failure_with_metering(path, delays):
    failed = False

    def provider(request):
        nonlocal failed
        if request.url.path == path and not failed:
            failed = True
            return httpx.Response(503, json={})
        return success(request)

    result, ledger, limits = run_provider(provider, role='TYPO')
    assert result.text == '{"value": 1}' and limits.model_calls == 2
    assert len(ledger.settled) == 1 and delays == [2.25]
    assert len(ledger.unknown) == int(path == '/responses')


@pytest.mark.parametrize('status,code', [(400, 'invalid_request_error'), (401, 'invalid_api_key'),
                                      (403, None), (429, 'insufficient_quota')])
@pytest.mark.parametrize('path', ['/responses', '/responses/input_tokens'])
def test_typo_permanent_rejection_preserves_segment_stop_code(status, code, path, delays):
    def provider(request):
        if request.url.path == path:
            return httpx.Response(status, json={'error': {'code': code}})
        return success(request)

    ledger, limits = Ledger(), budget()
    with pytest.raises(IncompleteInspection) as exc:
        run_provider(provider, ledger, limits, role='TYPO')
    assert exc.value.code == 'LLM_PROVIDER_REJECTED'
    assert exc.value.cause == f'MODEL_CALL_HTTP_{status}'
    assert limits.model_calls == 1 and delays == []
    assert len(ledger.reserved) == int(path == '/responses')


def test_exact_count_retry_does_not_generate_before_count_succeeds(delays):
    paths = []

    def provider(request):
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(429, json={'error': {'code': 'rate_limit_exceeded'}})
        return success(request)

    result, ledger, limits = run_provider(provider)
    assert result.value == 1
    assert paths == ['/responses/input_tokens', '/responses/input_tokens', '/responses']
    assert len(ledger.reserved) == 1 and len(ledger.unknown) == 0
    assert limits.model_calls == 2 and delays == [2.25]


@pytest.mark.parametrize('status,code', [(400, 'invalid_request_error'), (401, 'invalid_api_key'),
    (403, None), (422, None), (429, 'insufficient_quota'), (429, 'billing_hard_limit_reached'),
    (429, 'unrecognized_quota'), (429, {'malformed': True})])
def test_nonretryable_errors_do_not_loop(status, code, delays):
    def provider(request):
        if request.url.path.endswith('/input_tokens'):
            return success(request)
        return httpx.Response(status, json={'error': {'code': code}})
    ledger = Ledger()
    with pytest.raises(IncompleteInspection) as e:
        run_provider(provider, ledger)
    assert e.value.cause == f'MODEL_CALL_HTTP_{status}'
    assert len(ledger.reserved) == 1 and delays == []


def test_exhausted_attempts_and_long_retry_after(delays):
    for hint, attempts in [('0', 3), ('3600', 1)]:
        ledger = Ledger()
        def provider(request, hint=hint):
            if request.url.path.endswith('/input_tokens'):
                return success(request)
            return httpx.Response(503, headers={'retry-after': hint}, json={})
        with pytest.raises(IncompleteInspection):
            run_provider(provider, ledger)
        assert len(ledger.reserved) == attempts == len(ledger.unknown)


def test_budget_and_gate_prevent_new_generation(delays, monkeypatch):
    def provider(request):
        if request.url.path.endswith('/input_tokens'):
            return success(request)
        return httpx.Response(503, json={})
    ledger = Ledger()
    with pytest.raises(IncompleteInspection) as e:
        run_provider(provider, ledger, budget(calls=1))
    assert e.value.code == 'BUDGET_EXHAUSTED' and len(ledger.reserved) == 1
    ledger = Ledger()
    async def invalidate(delay):
        ledger.gate.stop()
    monkeypatch.setattr(retry.asyncio, 'sleep', invalidate)
    with pytest.raises(IncompleteInspection) as e:
        run_provider(provider, ledger)
    assert e.value.code == 'EXECUTION_STOPPED' and len(ledger.reserved) == 1


def test_untagged_http_from_ledger_is_never_retried(delays):
    class BrokenLedger(Ledger):
        async def reserve_ai_tokens(self, **kwargs):
            self.reserved.append(kwargs)
            response = httpx.Response(503, request=httpx.Request('POST', 'http://spring.invalid'))
            response.raise_for_status()
    ledger = BrokenLedger()
    with pytest.raises(IncompleteInspection):
        run_provider(success, ledger)
    assert len(ledger.reserved) == 1 and delays == []


def test_network_retry_and_cancellation(delays, monkeypatch):
    n = 0
    def provider(request):
        nonlocal n
        n += 1
        if n == 1:
            raise httpx.ReadTimeout('PRIVATE_MANUSCRIPT', request=request)
        return success(request)
    assert run_provider(provider)[0].value == 1
    async def canceled(delay):
        raise asyncio.CancelledError()
    monkeypatch.setattr(retry.asyncio, 'sleep', canceled)
    n = 0
    with pytest.raises(asyncio.CancelledError):
        run_provider(provider)


def test_diagnostics_reject_untrusted_header_and_code(delays, caplog):
    def provider(request):
        return httpx.Response(400, headers={'x-request-id': 'PRIVATE_MANUSCRIPT'},
                              json={'error': {'code': 'PRIVATE_MANUSCRIPT'}})
    with pytest.raises(IncompleteInspection):
        run_provider(provider)
    assert 'PRIVATE_MANUSCRIPT' not in caplog.text


def test_retry_after_formats():
    assert retry._server_delay('NaN') is None
    assert retry._server_delay('-3') is None
    assert retry._server_delay('Wed, 01 Jan 2020 00:00:00 GMT') == 0
    assert retry._server_delay('invalid') is None


def test_recovery_deadline_declines_delay_without_retry(delays, monkeypatch):
    monkeypatch.setattr(retry, 'MAX_RECOVERY_SECONDS', 0.1)
    def provider(request):
        if request.url.path.endswith('/input_tokens'):
            return success(request)
        return httpx.Response(503, json={})
    ledger = Ledger()
    with pytest.raises(IncompleteInspection):
        run_provider(provider, ledger)
    assert len(ledger.reserved) == 1 and delays == []
