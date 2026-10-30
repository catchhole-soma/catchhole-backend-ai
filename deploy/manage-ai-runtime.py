#!/usr/bin/env python3
"""Host-side deployment/one-shot scheduler. Never prints rendered Compose secrets."""

import argparse
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


class DeploymentError(Exception):
    pass


def command(args, *, env=None, timeout=180, allowed_codes=(0,)):
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise DeploymentError("COMMAND_UNAVAILABLE_OR_TIMEOUT") from None
    if result.returncode not in allowed_codes:
        # Docker/Compose errors may contain interpolated credentials. Do not relay stderr.
        raise DeploymentError("COMMAND_FAILED")
    return result.stdout


def enabled(value):
    return str(value).lower() == "true"


def required(env, key):
    value = str(env.get(key) or "")
    if not value or "REPLACE" in value.upper() or "replace-with" in value:
        raise DeploymentError("MISSING_" + key)
    return value


def validate_worker_tracing(env):
    """Validate explicit production export without connecting or printing its value."""
    flag = str(env.get("AI_WORKER_TRACING_ENABLED", "false")).lower()
    if flag not in {"true", "false"}:
        raise DeploymentError("WORKER_TRACING_BOOLEAN_REQUIRED")
    if flag == "false":
        return False
    endpoint = str(env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "")
    try:
        url = urlsplit(endpoint)
        address = ipaddress.IPv4Address(url.hostname or "")
        private_networks = (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
        if (
            endpoint != endpoint.strip()
            or url.scheme != "http"
            or not any(address in network for network in private_networks)
            or url.port != 4318
            or url.path != "/v1/traces"
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError()
    except ValueError:
        raise DeploymentError("WORKER_TRACING_PRIVATE_OTLP_ENDPOINT_REQUIRED") from None
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", str(env.get("OTEL_SERVICE_NAME", ""))):
        raise DeploymentError("WORKER_TRACING_SERVICE_NAME_REQUIRED")
    return True


class Runtime:
    def __init__(self, directory, release, image=None):
        self.directory = Path(directory).resolve()
        self.release = Path(release).resolve()
        if image is None:
            if not re.fullmatch(r"[a-f0-9]{40}", self.release.name):
                raise DeploymentError("IMMUTABLE_RELEASE_REQUIRED")
            image = "ghcr.io/catchhole-soma/catchhole-backend-ai:sha-" + self.release.name[:7]
        if not re.fullmatch(
            r"ghcr.io/catchhole-soma/catchhole-backend-ai:sha-[a-f0-9]{7,40}", image
        ):
            raise DeploymentError("IMMUTABLE_IMAGE_REQUIRED")
        # Do not let an interactive shell silently override protected env-file settings.
        self.environment = {
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR", "LANG")
        }
        self.environment["AI_IMAGE"] = image

    def compose(self, kind, *args, timeout=180, allowed_codes=(0,)):
        files = [self.directory / "worker.env"]
        if kind != "worker":
            files.append(self.directory / (kind + ".env"))
        for file in files:
            if not file.is_file():
                raise DeploymentError("MISSING_" + file.name.upper().replace(".", "_"))
        command_args = ["docker", "compose"]
        for file in files:
            command_args += ["--env-file", str(file)]
        command_args += ["-f", str(self.release / ("compose." + kind + ".prod.yml")), *args]
        return command(
            command_args, env=self.environment, timeout=timeout, allowed_codes=allowed_codes
        )

    def config(self, kind):
        try:
            return json.loads(self.compose(kind, "config", "--format", "json"))["services"]
        except (ValueError, KeyError):
            raise DeploymentError("INVALID_COMPOSE_CONFIGURATION") from None

    def validate(self):
        worker = self.config("worker")
        tracing_services = [
            name for name in (
                "ai-worker", "ai-character-comparison-worker", "ai-world-comparison-worker",
                "ai-relationship-worker",
            )
            if validate_worker_tracing(worker[name]["environment"])
        ]
        chat = self.config("chatbot")["chatbot"]
        index = self.config("retrieval")["retrieval-index"]
        w = worker["ai-worker"]["environment"]
        relation = worker["ai-relationship-worker"]
        relation_env = relation["environment"]
        if (relation.get("command") != ["python", "scripts/run_analysis_worker.py", "--worker-kind", "relationship"]
                or str(relation.get("deploy", {}).get("replicas")) != "1"
                or str(relation_env.get("AI_WORKER_CONCURRENCY")) != "1"
                or str(relation_env.get("LLM_MAX_CONCURRENT_REQUESTS")) != "1"
                or enabled(relation_env.get("EMBEDDING_GENERATION_ENABLED"))
                or enabled(relation_env.get("RETRIEVAL_PREPARATION_ENABLED"))):
            raise DeploymentError("RELATIONSHIP_WORKER_ISOLATION_REQUIRED")
        c, r = chat["environment"], index["environment"]
        if not enabled(c.get("CHAT_QA_ENABLED")):
            raise DeploymentError("CHAT_QA_ENABLED_REQUIRED")
        for env in (w, c, r):
            for key in ("SPRING_INTERNAL_API_KEY", "SPRING_INTERNAL_API_BASE_URL", "LLM_API_KEY"):
                required(env, key)
        common = ("SPRING_INTERNAL_API_KEY", "SPRING_INTERNAL_API_BASE_URL", "LLM_API_KEY")
        for env, keys in (
            (c, common),
            (r, (*common, "DATABASE_URL", "AWS_REGION", "AWS_S3_BUCKET")),
            (relation_env, (*common, "AWS_REGION", "AWS_S3_BUCKET")),
        ):
            for key in keys:
                if not w.get(key):
                    raise DeploymentError("MISSING_" + key)
                if env.get(key) != w[key]:
                    raise DeploymentError("INCONSISTENT_" + key)
        try:
            bind = ipaddress.ip_address(chat["ports"][0]["host_ip"])
            if bind.is_unspecified or bind.is_loopback or not bind.is_private:
                raise ValueError()
        except (KeyError, IndexError, ValueError):
            raise DeploymentError("CHAT_PRIVATE_BIND_ADDRESS_REQUIRED") from None
        indexing = enabled(r.get("RETRIEVAL_BACKFILL_ENABLED"))
        if indexing and not enabled(r.get("EMBEDDING_GENERATION_ENABLED")):
            raise DeploymentError("RETRIEVAL_EMBEDDING_GENERATION_ENABLED_REQUIRED")
        if enabled(c.get("CHAT_RETRIEVAL_ENABLED")):
            required(c, "CHAT_RETRIEVAL_DATABASE_URL")
            if c.get("CHAT_RETRIEVAL_EMBEDDING_VERSION") != r.get("EMBEDDING_VERSION"):
                raise DeploymentError("EMBEDDING_VERSION_MISMATCH")
        try:
            replicas = int(worker["ai-worker"]["deploy"]["replicas"])
            pool_size = int(w["DATABASE_POOL_SIZE"])
            overflow = int(w["DATABASE_POOL_MAX_OVERFLOW"])
            if pool_size < 1 or overflow < 0:
                raise ValueError()
            pool = pool_size + overflow
            comparisons = sum(
                int(worker[k]["environment"]["DATABASE_POOL_SIZE"])
                + int(worker[k]["environment"]["DATABASE_POOL_MAX_OVERFLOW"])
                for k in ("ai-character-comparison-worker", "ai-world-comparison-worker")
            )
            api_pool = int(required(r, "AI_RUNTIME_API_DB_POOL_SIZE"))
            budget = int(required(r, "AI_RUNTIME_DB_CONNECTION_BUDGET"))
            connections = api_pool + replicas * pool + comparisons
            # The relationship-only worker uses S3 + Spring APIs, never a DB session.
            connections += 2 * enabled(c.get("CHAT_RETRIEVAL_ENABLED")) + 2 * indexing
            # Read-only preflight probes need one temporary connection alongside old runtime.
            if min(replicas, pool, api_pool, budget) < 1 or connections + 1 > budget:
                raise ValueError()
        except (KeyError, ValueError):
            raise DeploymentError("DATABASE_CONNECTION_BUDGET_EXCEEDED") from None
        return {
            "indexing": indexing,
            "analysisReplicas": replicas,
            "databaseConnections": connections,
            "databaseConnectionsDuringPreflight": connections + 1,
            "tracingWorkerServices": tracing_services,
        }

    def preflight(self):
        configuration = self.validate()
        # All three Compose files use the same immutable image; pull it once.
        self.compose("worker", "pull", timeout=600)
        self.verify_ready(
            self.compose(
                "chatbot",
                "run",
                "--rm",
                "--no-deps",
                "chatbot",
                "python",
                "-m",
                "app.chatbot.readiness",
                timeout=60,
                allowed_codes=(0, 2),
            ),
            "READY",
        )
        if configuration["indexing"]:
            self.verify_ready(
                self.compose(
                    "retrieval",
                    "run",
                    "--rm",
                    "--no-deps",
                    "retrieval-index",
                    "python",
                    "-m",
                    "app.retrieval.backfill",
                    "preflight",
                    timeout=60,
                    allowed_codes=(0, 2),
                ),
                "PREFLIGHT_READY",
            )
        return configuration

    @staticmethod
    def verify_ready(output, expected):
        try:
            result = json.loads(output)
            if result.get("state") != expected:
                code = result.get("code", result.get("state"))
                if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                    raise DeploymentError("RUNTIME_PREFLIGHT_NOT_READY_" + code)
                raise ValueError()
        except (ValueError, AttributeError):
            raise DeploymentError("RUNTIME_PREFLIGHT_NOT_READY") from None

    def deploy(self):
        configuration = self.preflight()
        self.compose("worker", "up", "-d", timeout=600)
        self.compose("chatbot", "up", "-d", "--wait", "--wait-timeout", "90", timeout=180)
        for _ in range(30):
            expected = {
                "ai-worker": configuration["analysisReplicas"],
                "ai-character-comparison-worker": 1,
                "ai-world-comparison-worker": 1,
                "ai-relationship-worker": 1,
            }
            if all(
                len(self.compose("worker", "ps", "--status", "running", "-q", name).split())
                == count
                for name, count in expected.items()
            ):
                break
            time.sleep(5)
        else:
            raise DeploymentError("WORKER_PROCESS_CHECK_FAILED")
        # Verify the recreated process and actual internal key, not healthz alone.
        self.verify_ready(
            self.compose(
                "chatbot",
                "exec",
                "-T",
                "chatbot",
                "python",
                "-m",
                "app.chatbot.readiness",
                timeout=60,
                allowed_codes=(0, 2),
            ),
            "READY",
        )
        return {"state": "DEPLOYED", **configuration, "backfillComplete": False}

    def backfill(self, mode):
        # The container owns durable budget, cursor, outbox, per-work and process locks.
        output = self.compose(
            "retrieval",
            "run",
            "--rm",
            "--no-deps",
            "retrieval-index",
            "python",
            "-m",
            "app.retrieval.backfill",
            mode,
            timeout=780,
            allowed_codes=(0, 2),
        )
        # Only the runner's machine-readable aggregate result can enter journald.
        try:
            result = json.loads(output.strip())
            if not isinstance(result, dict):
                raise ValueError()
            safe = {}
            for key in ("state", "reason"):
                value = result.get(key)
                if value is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", str(value)):
                    raise ValueError()
                safe[key] = value
            if safe["state"] is None:
                raise ValueError()
            for key in (
                "schemaVersion",
                "admittedInputTokenUpperBound",
                "maxInputTokens",
                "remainingInputTokenAllowance",
                "uncertainAttempt",
                "sweep",
                "sweepInProgress",
                "checkedSources",
                "readySources",
                "excludedOrUnavailableSources",
                "userTokenDebit",
                "embeddedChunks",
                "providerAttempts",
                "pendingReports",
            ):
                if key in result:
                    if not isinstance(result[key], (int, bool)) or result[key] < 0:
                        raise ValueError()
                    safe[key] = result[key]
            if "snapshotAt" in result:
                if result["snapshotAt"] is not None:
                    datetime.fromisoformat(result["snapshotAt"])
                safe["snapshotAt"] = result["snapshotAt"]
            for key in ("supportedSourceTypes", "unsupportedSourceTypes"):
                if key in result:
                    if not isinstance(result[key], list) or any(
                        item not in {"CURRENT_EPISODE", "SETTING_BOOK", "PAST_EPISODE_VERSION"}
                        for item in result[key]
                    ):
                        raise ValueError()
                    safe[key] = result[key]
            return safe
        except ValueError:
            raise DeploymentError("INVALID_BACKFILL_STATUS") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "preflight", "deploy", "run", "status", "resume"))
    parser.add_argument("--directory", default="/opt/catchhole")
    parser.add_argument("--release", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--image")
    args = parser.parse_args()
    try:
        runtime = Runtime(args.directory, args.release, args.image)
        # Same-host concurrent manual deployments cannot interleave compose updates.
        if args.mode == "validate":
            result = runtime.validate()
        elif args.mode in {"deploy", "preflight"}:
            with open(runtime.directory / ".chat-deploy.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = runtime.deploy() if args.mode == "deploy" else runtime.preflight()
        else:
            result = runtime.backfill(args.mode)
        print(json.dumps(result, sort_keys=True))
        if args.mode in {"run", "resume"} and result.get("state") not in {
            "DISABLED",
            "READY",
            "EMPTY",
            "PROGRESS",
            "TIME_LIMIT",
            "CHUNK_LIMIT",
            "BUDGET_EXHAUSTED",
            "INDEX_BUSY",
            "RESUMED",
            "NOT_STARTED",
        }:
            return 2
        return 0
    except DeploymentError as exc:
        print(json.dumps({"state": "FAILED", "code": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"state": "FAILED", "code": "RUNTIME_CONFIGURATION_FAILURE"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
