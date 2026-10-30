"""Run a private, bounded relationship-only experiment (no Gold or Worker writes).

Prepare creates a new output directory without credentials or provider calls. An
execution must use another new directory and explicitly set --max-requests.
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from dotenv import dotenv_values

# Keep the script executable directly as well as importable in offline tests.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.analysis.relationship_extractor import (
    DEFAULT_PROMPT_PATH,
    DEFAULT_RELATIONSHIP_MODEL,
    RELATIONSHIP_RESPONSE_SCHEMA,
    RelationshipExtractor,
)
from app.analysis.relationship_observations import RELATIONSHIP_OBSERVATION_VERSION
from app.llm.openai_client import OpenAIResponsesClient

DEFAULT_RESPONSES_URL = "https://api.openai.com/v1/responses"
CODE_FILES = (
    "scripts/evaluate_relationship_extraction.py",
    "app/analysis/relationship_extractor.py",
    "app/analysis/relationship_observations.py",
    "app/analysis/exceptions.py",
    "app/llm/openai_client.py",
    "app/llm/protocols.py",
    "app/llm/responses.py",
    "app/llm/exceptions.py",
    "app/llm/prompts/relationship_extraction.md",
    "app/usage/metering.py",
)


class ExperimentError(Exception):
    """A fixed diagnostic code, never provider or manuscript text."""


@dataclass(frozen=True)
class RunConfig:
    source_dir: Path
    output_dir: Path
    episodes: tuple[int, ...] = (1, 2, 3, 4)
    model: str = DEFAULT_RELATIONSHIP_MODEL
    reasoning_effort: str = "medium"
    max_output_tokens: int = 16000
    max_requests: int | None = None
    execute: bool = False
    env_file: Path | None = None


@dataclass(frozen=True)
class EpisodeSource:
    episode_no: int
    path: Path
    text: str
    source_sha256: str
    chunk_id: UUID


def _hash(data: bytes) -> str:
    return sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _json_hash(value: object) -> str:
    return _hash(_json_bytes(value))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_private(path: Path, data: bytes) -> None:
    """Persist before continuing; private temporary files remain private on a crash."""
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: object) -> None:
    _write_private(path, _json_bytes(value))


def _code_hashes() -> dict[str, str]:
    return {name: _hash((REPOSITORY_ROOT / name).read_bytes()) for name in CODE_FILES}


def _git_head() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT,
        capture_output=True, text=True, check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else None


def _sources(config: RunConfig) -> list[EpisodeSource]:
    if not config.episodes or any(type(number) is not int or number <= 0
                                  for number in config.episodes):
        raise ExperimentError("INVALID_EPISODES")
    if len(set(config.episodes)) != len(config.episodes):
        raise ExperimentError("DUPLICATE_EPISODE")
    sources = []
    for number in config.episodes:
        candidates = {config.source_dir / f"{number:02d}화.txt",
                      config.source_dir / f"{number}화.txt"}
        files = [path for path in candidates if path.is_file()]
        if len(files) != 1:
            raise ExperimentError("SOURCE_MISSING_OR_AMBIGUOUS")
        raw = files[0].read_bytes()
        text = raw.decode("utf-8")
        if not text.strip():
            raise ExperimentError("EMPTY_SOURCE")
        digest = _hash(raw)
        sources.append(EpisodeSource(
            number, files[0], text, digest,
            uuid5(NAMESPACE_URL, f"catchhole:relationship-evaluation:{number}:{digest}"),
        ))
    return sources


def _api_key(config: RunConfig, environ: dict[str, str]) -> str:
    values = environ
    if config.env_file is not None:
        if not config.env_file.is_file():
            raise ExperimentError("ENV_FILE_MISSING")
        # No ambient .env search, interpolation, or assignment to process settings.
        values = dotenv_values(config.env_file, interpolate=False)
    key = values.get("LLM_API_KEY") or values.get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise ExperimentError("API_KEY_MISSING")
    return key


def _usage(payload: object) -> dict:
    raw = payload.get("usage") if isinstance(payload, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    input_details = raw.get("input_tokens_details")
    output_details = raw.get("output_tokens_details")

    def number(value):
        return value if type(value) is int and value >= 0 else None

    result = {
        "input_tokens": number(raw.get("input_tokens")),
        "cached_input_tokens": number(input_details.get("cached_tokens"))
        if isinstance(input_details, dict) else None,
        "output_tokens": number(raw.get("output_tokens")),
        "reasoning_output_tokens": number(output_details.get("reasoning_tokens"))
        if isinstance(output_details, dict) else None,
    }
    result["known"] = result["input_tokens"] is not None and result["output_tokens"] is not None
    return result


class RequestRecorder:
    """One sequential run: mkdir is the exclusive claim; no resume or retries."""

    def __init__(self, config: RunConfig, responses_url: str):
        self.config = config
        self.responses_url = httpx.URL(responses_url)
        self.source: EpisodeSource | None = None
        self.records: list[dict] = []
        self.started = 0.0
        self._save()

    def _save(self):
        _write_json(self.config.output_dir / "requests.private.json", {
            "max_requests": self.config.max_requests,
            "requests_consumed": len(self.records),
            "requests": self.records,
        })

    async def before_request(self, request: httpx.Request):
        if request.method != "POST" or request.url != self.responses_url or self.source is None:
            raise ExperimentError("UNEXPECTED_PROVIDER_REQUEST")
        if len(self.records) >= self.config.max_requests:
            raise ExperimentError("REQUEST_BUDGET_EXHAUSTED")
        if any(row["episode_no"] == self.source.episode_no for row in self.records):
            raise ExperimentError("RETRY_FORBIDDEN")
        body = json.loads(request.content)
        schema = body.get("text", {}).get("format", {})
        if (body.get("model") != self.config.model
                or body.get("reasoning", {}).get("effort") != self.config.reasoning_effort
                or body.get("max_output_tokens") != self.config.max_output_tokens
                or body.get("store") is not False
                or schema.get("schema") != RELATIONSHIP_RESPONSE_SCHEMA.schema
                or schema.get("strict") is not True):
            raise ExperimentError("REQUEST_CONDITION_MISMATCH")
        self.started = time.perf_counter()
        self.records.append({
            "sequence": len(self.records) + 1,
            "episode_no": self.source.episode_no,
            "phase": "RELATIONSHIP_EXTRACTION",
            "state": "OUTBOUND_UNKNOWN",
            "started_at": _now(),
            "request_sha256": _hash(request.content),
            "endpoint_sha256": _hash(str(request.url).encode()),
            "requested_model": body["model"],
            "reasoning_effort": body["reasoning"]["effort"],
            "max_output_tokens": body["max_output_tokens"],
            "store": body["store"],
            "schema_sha256": _json_hash(schema["schema"]),
            "prompt_cache_key": body.get("prompt_cache_key"),
            "source_sha256": self.source.source_sha256,
            "source_chunk_id": str(self.source.chunk_id),
        })
        # Persist the debit BEFORE outbound transport; failed requests consume budget too.
        self._save()

    async def after_response(self, response: httpx.Response):
        record = self.records[-1]
        record["http_status"] = response.status_code
        if 200 <= response.status_code < 300:
            raw = await response.aread()
            filename = f"episode-{self.source.episode_no:02d}.provider-response.private.json"
            _write_private(self.config.output_dir / filename, raw)
            record.update(raw_response_file=filename, raw_response_sha256=_hash(raw))
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                payload = None
            record["usage"] = _usage(payload)
            record["actual_model"] = payload.get("model") if isinstance(payload, dict) else None
            record["provider_status"] = payload.get("status") if isinstance(payload, dict) else None
        # Never retain error bodies, headers, exception messages, or credentials.
        record.update(state="HTTP_RECEIVED", elapsed_seconds=time.perf_counter() - self.started)
        self._save()

    def finish(self, success: bool, error_type: str | None = None):
        if (self.source is None or not self.records
                or self.records[-1]["episode_no"] != self.source.episode_no):
            return
        record = self.records[-1]
        record.update(state="COMPLETED" if success else "FAILED",
                      elapsed_seconds=time.perf_counter() - self.started)
        if error_type:
            record["failure_type"] = error_type
        self._save()


def _failure_type(error: Exception) -> str:
    if isinstance(error, ExperimentError):
        return str(error)
    # Class names only; str/repr/error.response may contain secrets or manuscript.
    return type(error).__name__


async def run_experiment(
    config: RunConfig, *, transport: httpx.AsyncBaseTransport | None = None,
    environ: dict[str, str] | None = None,
) -> dict:
    """Tests inject MockTransport; normal CLI has no transport override."""
    if (not config.model.strip() or config.reasoning_effort not in
            {"low", "medium", "high", "xhigh"}
            or type(config.max_output_tokens) is not int
            or not 1 <= config.max_output_tokens <= 128000):
        raise ExperimentError("INVALID_MODEL_CONFIGURATION")
    if config.execute and (type(config.max_requests) is not int or config.max_requests <= 0):
        raise ExperimentError("EXECUTE_REQUIRES_POSITIVE_MAX_REQUESTS")
    if config.max_requests is not None and (
            type(config.max_requests) is not int or config.max_requests <= 0):
        raise ExperimentError("INVALID_REQUEST_BUDGET")
    sources = _sources(config)
    # Parent must already exist; exclusive mkdir also rejects symlinks and prior prepare runs.
    try:
        config.output_dir.mkdir(mode=0o700)
    except FileExistsError:
        raise ExperimentError("OUTPUT_DIRECTORY_ALREADY_EXISTS") from None
    code_hashes = _code_hashes()
    environment = dict(os.environ) if environ is None else environ
    responses_url = environment.get("OPENAI_RESPONSES_API_URL", DEFAULT_RESPONSES_URL)
    manifest = {
        "format_version": 1,
        "created_at": _now(),
        "status": "PREPARED",
        "scope": "RELATIONSHIP_ONLY_NO_GOLD_NO_CHARACTER_WORLD_OR_DATABASE_WRITES",
        "model": config.model, "reasoning_effort": config.reasoning_effort,
        "max_output_tokens": config.max_output_tokens, "max_requests": config.max_requests,
        "store": False, "read_timeout_seconds": 300, "implicit_retries": 0,
        "relationship_version": RELATIONSHIP_OBSERVATION_VERSION,
        "schema_sha256": _json_hash(RELATIONSHIP_RESPONSE_SCHEMA.schema),
        "prompt_sha256": _hash(DEFAULT_PROMPT_PATH.read_bytes()),
        "code_sha256": code_hashes, "git_head": _git_head(),
        "endpoint_sha256": _hash(responses_url.encode()),
        "sources": [{"episode_no": source.episode_no, "filename": source.path.name,
                     "source_sha256": source.source_sha256,
                     "source_chunk_id": str(source.chunk_id)} for source in sources],
        "usage_note": "Cached tokens are part of input; reasoning tokens are part of output.",
        "quality_note": "Accepted observations passed lexical grounding, not semantic assessment.",
        "execution_note": "No resume: each prepare or execute requires a new output directory.",
    }
    _write_json(config.output_dir / "manifest.private.json", manifest)
    result = {"status": "PREPARED", "requests_consumed": 0, "episodes": [],
              "failure": None, "started_at": _now()}
    _write_json(config.output_dir / "result.private.json", result)
    if not config.execute:
        return result
    recorder = None
    active_episode = None
    try:
        key = _api_key(config, environment)
        recorder = RequestRecorder(config, responses_url)
        manifest["status"] = result["status"] = "RUNNING"
        _write_json(config.output_dir / "manifest.private.json", manifest)
        _write_json(config.output_dir / "result.private.json", result)
        async with httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(120, read=300),
            follow_redirects=False, trust_env=False,
            event_hooks={"request": [recorder.before_request], "response": [recorder.after_response]},
        ) as http_client:
            client = OpenAIResponsesClient(
                api_key=key, model=config.model, responses_api_url=responses_url,
                reasoning_effort=config.reasoning_effort, http_client=http_client,
                store_responses=False,
            )
            extractor = RelationshipExtractor(
                client, model=config.model, max_output_tokens=config.max_output_tokens,
                reasoning_effort=config.reasoning_effort,
            )
            for source in sources:
                active_episode = source.episode_no
                recorder.source = source
                if _code_hashes() != code_hashes:
                    raise ExperimentError("CODE_CHANGED_DURING_RUN")
                if _hash(source.path.read_bytes()) != source.source_sha256:
                    raise ExperimentError("SOURCE_CHANGED_DURING_RUN")
                extraction = await extractor.extract_from_chunk(
                    source_chunk_id=source.chunk_id, chunk_text=source.text,
                    episode_no=source.episode_no,
                )
                filename = f"episode-{source.episode_no:02d}.observations.private.json"
                _write_json(config.output_dir / filename, {
                    "episode_no": source.episode_no, "source_chunk_id": str(source.chunk_id),
                    "raw_observations": extraction.raw_observations,
                    "observations": [item.model_dump(mode="json") for item in extraction.observations],
                    "rejections": [item.model_dump(mode="json") for item in extraction.rejections],
                })
                recorder.finish(True)
                result["episodes"].append({
                    "episode_no": source.episode_no, "status": "COMPLETED",
                    "raw_count": len(extraction.raw_observations),
                    "accepted_count": len(extraction.observations),
                    "rejected_count": len(extraction.rejections), "result_file": filename,
                })
                result["requests_consumed"] = len(recorder.records)
                _write_json(config.output_dir / "result.private.json", result)
        # Check drift during the final request too. Successful HTTP/parse records
        # remain successful even if the run's final reproducibility check fails.
        active_episode = None
        recorder.source = None
        if _code_hashes() != code_hashes:
            raise ExperimentError("CODE_CHANGED_DURING_RUN")
        if any(_hash(source.path.read_bytes()) != source.source_sha256 for source in sources):
            raise ExperimentError("SOURCE_CHANGED_DURING_RUN")
        result["status"] = "COMPLETED"
    except Exception as error:  # noqa: BLE001 - stop safely without logging sensitive exceptions
        error_type = _failure_type(error)
        if recorder is not None:
            recorder.finish(False, error_type)
        result["status"] = "PARTIAL_FAILED" if result["episodes"] else "FAILED"
        result["failure"] = {"episode_no": active_episode, "type": error_type}
    result["requests_consumed"] = len(recorder.records) if recorder else 0
    result["finished_at"] = _now()
    manifest["status"] = result["status"]
    _write_json(config.output_dir / "result.private.json", result)
    _write_json(config.output_dir / "manifest.private.json", manifest)
    return result


def _episodes(value: str) -> tuple[int, ...]:
    try:
        numbers = tuple(int(part) for part in value.split(","))
        if not numbers or min(numbers) <= 0 or len(set(numbers)) != len(numbers):
            raise ValueError
        return numbers
    except ValueError:
        raise argparse.ArgumentTypeError("Use unique positive episode numbers, such as 1,2,3,4.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--episodes", default=(1, 2, 3, 4), type=_episodes)
    parser.add_argument("--model", default=DEFAULT_RELATIONSHIP_MODEL)
    parser.add_argument("--reasoning-effort", default="medium", choices=["low", "medium", "high", "xhigh"])
    parser.add_argument("--max-output-tokens", default=16000, type=int)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    # Provider debug output is not part of this private experiment's accounting.
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(run_experiment(RunConfig(**vars(args))))
    except Exception as error:  # noqa: BLE001 - never print provider/manuscript exception text
        print(json.dumps({"status": "FAILED", "type": _failure_type(error)}))
        return 1
    finally:
        logging.disable(previous_logging_disable)
    print(json.dumps({"status": result["status"], "requests_consumed": result["requests_consumed"],
                      "episodes_completed": len(result["episodes"])}))
    return 0 if result["status"] in {"COMPLETED", "PREPARED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
