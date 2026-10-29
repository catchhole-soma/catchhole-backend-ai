"""Offline safety/recording tests; MockTransport never reaches a provider."""

import asyncio
import json
import logging
import os
from dataclasses import replace
from hashlib import sha256

import httpx
import pytest

from scripts.evaluate_relationship_extraction import (
    ExperimentError,
    RunConfig,
    main,
    run_experiment,
)


@pytest.fixture
def config(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    for number in (1, 2):
        (source_dir / f"{number:02d}화.txt").write_text(
            "가람은 나래를 도왔다. 둘은 아직 서로의 이름을 모른다.", encoding="utf-8",
        )
    return RunConfig(source_dir, tmp_path / "run", episodes=(1, 2),
                     execute=True, max_requests=2)


def _payload(*, text=None, status="completed", usage=True):
    result = {
        "id": "test-response", "model": "gpt-6.1-terra-test-snapshot", "status": status,
        "output_text": text if text is not None else json.dumps({"observations": [{
            "source": {"name": "가람", "mention": "가람"},
            "target": {"name": "나래", "mention": "나래"},
            "summary": "가람이 나래를 도왔다. 친구 여부는 알 수 없다.",
            "evidence_span_ids": ["S1"],
        }]}, ensure_ascii=False),
    }
    if usage:
        result["usage"] = {
            "input_tokens": 100, "input_tokens_details": {"cached_tokens": 40},
            "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 20},
        }
    return result


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _run(config, handler, *, environ=None):
    return asyncio.run(run_experiment(
        config, transport=httpx.MockTransport(handler),
        environ=environ if environ is not None else {"LLM_API_KEY": "private-test-key"},
    ))


def test_prepare_needs_no_key_or_request_budget_and_never_calls_provider(config):
    def forbidden(request):
        pytest.fail("Prepare must not call the provider")

    result = _run(replace(config, execute=False, max_requests=None), forbidden, environ={})
    assert result["status"] == "PREPARED"
    assert result["requests_consumed"] == 0
    assert not (config.output_dir / "requests.private.json").exists()
    manifest = _read(config.output_dir / "manifest.private.json")
    assert manifest["model"] == "gpt-6.1-terra"
    assert manifest["reasoning_effort"] == "medium"
    assert manifest["max_output_tokens"] == 16000
    assert manifest["store"] is False
    assert manifest["read_timeout_seconds"] == 300
    assert manifest["implicit_retries"] == 0
    assert len(manifest["sources"]) == 2
    assert manifest["code_sha256"]["scripts/evaluate_relationship_extraction.py"]
    assert os.stat(config.output_dir).st_mode & 0o777 == 0o700
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in config.output_dir.iterdir())


def test_success_records_exact_private_raw_usage_conditions_and_debit_before_wire(config):
    calls = []
    raw = (" \n" + json.dumps(_payload(), ensure_ascii=False) + "\n").encode()

    def handler(request):
        debit = _read(config.output_dir / "requests.private.json")
        assert debit["requests_consumed"] == len(calls) + 1
        assert debit["requests"][-1]["state"] == "OUTBOUND_UNKNOWN"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6.1-terra"
        assert body["reasoning"] == {"effort": "medium"}
        assert body["store"] is False
        assert body["max_output_tokens"] == 16000
        assert request.extensions["timeout"]["read"] == 300
        assert request.headers["Authorization"] == "Bearer private-test-key"
        assert request.url.path == "/v1/responses"
        calls.append(request)
        return httpx.Response(200, content=raw)

    result = _run(config, handler)
    assert result["status"] == "COMPLETED"
    assert result["requests_consumed"] == len(calls) == 2
    assert [row["accepted_count"] for row in result["episodes"]] == [1, 1]
    ledger = _read(config.output_dir / "requests.private.json")
    for number, record in enumerate(ledger["requests"], 1):
        assert record["episode_no"] == number
        assert record["phase"] == "RELATIONSHIP_EXTRACTION"
        assert record["state"] == "COMPLETED"
        assert record["actual_model"] == "gpt-6.1-terra-test-snapshot"
        assert record["requested_model"] == "gpt-6.1-terra"
        assert record["usage"] == {"input_tokens": 100, "cached_input_tokens": 40,
                                   "output_tokens": 50, "reasoning_output_tokens": 20,
                                   "known": True}
        assert record["raw_response_sha256"] == sha256(raw).hexdigest()
        assert record["request_sha256"] == sha256(calls[number - 1].content).hexdigest()
        assert record["elapsed_seconds"] >= 0
        assert (config.output_dir / record["raw_response_file"]).read_bytes() == raw
        observation = _read(config.output_dir / f"episode-{number:02d}.observations.private.json")
        assert len(observation["raw_observations"]) == len(observation["observations"]) == 1
        assert observation["observations"][0]["evidence_spans"][0]["start_offset"] == 0
        assert observation["rejections"] == []
    assert _read(config.output_dir / "manifest.private.json")["status"] == "COMPLETED"
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in config.output_dir.iterdir())
    assert b"private-test-key" not in b"".join(path.read_bytes() for path in config.output_dir.iterdir())


def test_grounding_rejection_does_not_retry_or_fail_the_run(config):
    raw = json.loads(_payload()["output_text"])["observations"]
    raw.append({**raw[0], "evidence_span_ids": ["S999"]})
    result = _run(config, lambda request: httpx.Response(
        200, json=_payload(text=json.dumps({"observations": raw})),
    ))
    assert result["status"] == "COMPLETED"
    assert result["requests_consumed"] == 2
    assert result["episodes"][0]["raw_count"] == 2
    assert result["episodes"][0]["accepted_count"] == 1
    assert result["episodes"][0]["rejected_count"] == 1
    observation = _read(config.output_dir / "episode-01.observations.private.json")
    assert observation["rejections"] == [{"index": 1, "reason": "UNKNOWN_SOURCE_SPAN"}]


@pytest.mark.parametrize("code", [302, 401, 429, 500])
def test_http_failure_is_counted_never_retried_and_does_not_record_error_body(config, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, content=b"private-test-key secret manuscript error",
                              headers={"Location": "https://example.com/secret"})

    result = _run(config, handler)
    assert result["status"] == "FAILED"
    assert result["requests_consumed"] == len(calls) == 1
    assert result["failure"] == {"episode_no": 1, "type": "HTTPStatusError"}
    ledger = _read(config.output_dir / "requests.private.json")
    assert ledger["requests"][0]["http_status"] == code
    assert ledger["requests"][0]["state"] == "FAILED"
    assert not list(config.output_dir.glob("*provider-response*"))
    combined = b"".join(path.read_bytes() for path in config.output_dir.iterdir())
    assert b"private-test-key" not in combined
    assert b"secret manuscript error" not in combined
    assert b"example.com/secret" not in combined


@pytest.mark.parametrize("text", ["not JSON", '{"observations":{}}',
                                  '{"observations":[],"observations":[]}'])
def test_first_parse_failure_stops_following_episodes_but_keeps_2xx_response(config, text):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_payload(text=text))

    result = _run(config, handler)
    assert result["status"] == "FAILED"
    assert result["requests_consumed"] == len(calls) == 1
    assert result["failure"]["type"] == "LlmExtractionError"
    assert (config.output_dir / "episode-01.provider-response.private.json").exists()
    assert not (config.output_dir / "episode-02.provider-response.private.json").exists()
    assert _read(config.output_dir / "requests.private.json")["requests"][0]["usage"]["known"]


@pytest.mark.parametrize("kind", ["incomplete", "invalid_json", "timeout"])
def test_provider_failure_or_truncation_stops_without_retries(config, kind):
    calls = []

    def handler(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("private-test-key manuscript", request=request)
        if kind == "invalid_json":
            return httpx.Response(200, content=b"broken JSON")
        payload = _payload(status="incomplete")
        payload["incomplete_details"] = {"reason": "max_output_tokens"}
        return httpx.Response(200, json=payload)

    result = _run(config, handler)
    assert result["status"] == "FAILED"
    assert result["requests_consumed"] == len(calls) == 1
    ledger = _read(config.output_dir / "requests.private.json")
    assert ledger["requests"][0]["state"] == "FAILED"
    assert b"private-test-key" not in b"".join(path.read_bytes() for path in config.output_dir.iterdir())


def test_request_budget_exhaustion_stops_before_second_wire_call(config):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_payload())

    result = _run(replace(config, max_requests=1), handler)
    assert result["status"] == "PARTIAL_FAILED"
    assert result["requests_consumed"] == len(calls) == 1
    assert result["failure"] == {"episode_no": 2, "type": "REQUEST_BUDGET_EXHAUSTED"}
    ledger = _read(config.output_dir / "requests.private.json")
    assert ledger["requests"][0]["state"] == "COMPLETED"
    assert len(result["episodes"]) == 1


@pytest.mark.parametrize("maximum", [None, 0, -1, True])
def test_execute_requires_explicit_positive_request_budget_before_creating_output(config, maximum):
    with pytest.raises(ExperimentError):
        _run(replace(config, max_requests=maximum), lambda request: pytest.fail("No calls"))
    assert not config.output_dir.exists()


def test_output_directory_cannot_be_reused_after_prepare_or_execution(config):
    _run(replace(config, execute=False), lambda request: pytest.fail("No calls"))
    before = {path.name: path.read_bytes() for path in config.output_dir.iterdir()}
    with pytest.raises(ExperimentError, match="OUTPUT_DIRECTORY_ALREADY_EXISTS"):
        _run(config, lambda request: pytest.fail("No calls"))
    assert before == {path.name: path.read_bytes() for path in config.output_dir.iterdir()}


def test_deterministic_chunk_ids_and_private_missing_key_failure(config):
    prepared = replace(config, execute=False)
    _run(prepared, lambda request: pytest.fail("No calls"), environ={})
    first = _read(config.output_dir / "manifest.private.json")
    other = replace(config, output_dir=config.output_dir.with_name("other"))
    result = _run(other, lambda request: pytest.fail("No calls"), environ={})
    second = _read(other.output_dir / "manifest.private.json")
    assert first["sources"] == second["sources"]
    assert result["status"] == "FAILED"
    assert result["requests_consumed"] == 0
    assert result["failure"]["type"] == "API_KEY_MISSING"


def test_explicit_env_file_loads_only_key_without_interpolation_or_environment_mutation(config):
    env_file = config.source_dir.parent / "explicit.env"
    env_file.write_text("OPENAI_API_KEY='file-key-${NO_EXPANSION}'\nLLM_MODEL=do-not-use\n"
                        "OPENAI_RESPONSES_API_URL=https://do-not-use.invalid\n")
    old_environ = dict(os.environ)

    def handler(request):
        assert request.headers["Authorization"] == "Bearer file-key-${NO_EXPANSION}"
        assert request.url.host == "api.openai.com"
        assert json.loads(request.content)["model"] == "gpt-6.1-terra"
        return httpx.Response(200, json=_payload())

    result = _run(replace(config, env_file=env_file), handler,
                  environ={"LLM_API_KEY": "ambient-not-used"})
    assert result["status"] == "COMPLETED"
    assert dict(os.environ) == old_environ
    combined = b"".join(path.read_bytes() for path in config.output_dir.iterdir())
    assert b"file-key" not in combined
    assert b"ambient-not-used" not in combined


def test_changed_source_aborts_before_consuming_next_request(config):
    calls = []

    def handler(request):
        calls.append(request)
        (config.source_dir / "02화.txt").write_text("changed", encoding="utf-8")
        return httpx.Response(200, json=_payload())

    result = _run(config, handler)
    assert result["status"] == "PARTIAL_FAILED"
    assert len(calls) == result["requests_consumed"] == 1
    assert result["failure"]["type"] == "SOURCE_CHANGED_DURING_RUN"
    ledger = _read(config.output_dir / "requests.private.json")
    assert ledger["requests"][0]["state"] == "COMPLETED"


def test_missing_usage_remains_unknown_not_zero(config):
    result = _run(config, lambda request: httpx.Response(200, json=_payload(usage=False)))
    assert result["status"] == "COMPLETED"
    usage = _read(config.output_dir / "requests.private.json")["requests"][0]["usage"]
    assert usage == {"input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
                     "reasoning_output_tokens": None, "known": False}


def test_source_drift_during_final_response_preserves_successful_request_records(config):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            (config.source_dir / "01화.txt").write_text("changed", encoding="utf-8")
        return httpx.Response(200, json=_payload())

    result = _run(config, handler)
    assert result["status"] == "PARTIAL_FAILED"
    assert len(calls) == result["requests_consumed"] == 2
    assert result["failure"] == {"episode_no": None, "type": "SOURCE_CHANGED_DURING_RUN"}
    ledger = _read(config.output_dir / "requests.private.json")
    assert [record["state"] for record in ledger["requests"]] == ["COMPLETED", "COMPLETED"]


def test_code_drift_during_final_response_is_reported_without_another_call(config, monkeypatch):
    from scripts import evaluate_relationship_extraction as runner

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            monkeypatch.setattr(runner, "_code_hashes", lambda: {"changed": "hash"})
        return httpx.Response(200, json=_payload())

    result = _run(config, handler)
    assert result["status"] == "PARTIAL_FAILED"
    assert len(calls) == result["requests_consumed"] == 2
    assert result["failure"]["type"] == "CODE_CHANGED_DURING_RUN"
    ledger = _read(config.output_dir / "requests.private.json")
    assert [record["state"] for record in ledger["requests"]] == ["COMPLETED", "COMPLETED"]


def test_main_prints_only_fixed_summary_on_missing_key(config, monkeypatch, capsys):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    previous_logging_disable = logging.root.manager.disable
    assert main(["--source-dir", str(config.source_dir), "--output-dir", str(config.output_dir),
                 "--episodes", "1,2", "--execute", "--max-requests", "2"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "FAILED", "requests_consumed": 0, "episodes_completed": 0,
    }
    assert logging.root.manager.disable == previous_logging_disable


@pytest.mark.parametrize("effort", ["low", "high"])
def test_explicit_experiment_effort_matches_manifest_and_actual_request(config, effort):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_payload())
    result = _run(replace(config, reasoning_effort=effort), handler)
    assert result["status"] == "COMPLETED"
    assert all(request["reasoning"] == {"effort": effort} for request in requests)
    assert _read(config.output_dir / "manifest.private.json")["reasoning_effort"] == effort
    assert all(row["reasoning_effort"] == effort
               for row in _read(config.output_dir / "requests.private.json")["requests"])
