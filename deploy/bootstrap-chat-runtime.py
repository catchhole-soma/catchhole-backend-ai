#!/usr/bin/env python3
"""Private, idempotent host bootstrap. No secret or rendered Compose output is printed."""

import argparse
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = 1
MARKER = "CHAT_BOOTSTRAP_PREPARED_VERSION"
ACTIVATION_MARKER = "CHAT_BOOTSTRAP_ACTIVATION_ID"
SAFE_ENVIRONMENT = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR", "LANG")
DATABASE_FAILURE_CODES = frozenset(
    {
        "BOOTSTRAP_BUSY",
        "BOOTSTRAP_CONFIGURATION_INVALID",
        "BOOTSTRAP_DATABASE_OR_STATE_FAILURE",
        "DATABASE_CAPACITY_INSUFFICIENT",
        "DATABASE_CONFIGURATION_INVALID",
        "DATABASE_TARGET_MISMATCH",
        "DB_BOOTSTRAP_AUTHENTICATION_FAILED",
        "DB_BOOTSTRAP_PERMISSION_DENIED",
        "PRIVATE_DIRECTORY_PERMISSIONS_INVALID",
        "PRIVATE_FILE_PERMISSIONS_INVALID",
        "READER_INTENT_INVALID",
        "READER_TARGET_MISMATCH",
        "READ_ROLE_COLLISION",
        "READ_ROLE_ELEVATED",
        "READ_ROLE_EXCESS_READ_PRIVILEGE",
        "READ_ROLE_MEMBERSHIP",
        "READ_ROLE_NOT_AVAILABLE",
        "READ_ROLE_SECURITY_DEFINER_ACCESS",
        "READ_ROLE_WRITE_PRIVILEGE",
        "RETRIEVAL_DATABASE_NOT_READY",
        "RETRIEVAL_DATABASE_URL_INVALID",
        "RETRIEVAL_TRANSACTION_NOT_READ_ONLY",
        "RETRIEVAL_VECTOR_SCHEMA_INVALID",
        "RETRIEVAL_SCHEMA_NOT_READY",
    }
)


class BootstrapError(Exception):
    pass


def command(args, *, environment=None, timeout=180, database_result=False):
    try:
        result = subprocess.run(
            args, env=environment, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BootstrapError("COMMAND_UNAVAILABLE_OR_TIMEOUT") from None
    if database_result:
        try:
            value = json.loads(result.stdout)
            if value.get("schemaVersion") != 1:
                raise ValueError()
            if result.returncode == 2 and value.get("state") == "FAILED":
                code = value.get("code")
                if code in DATABASE_FAILURE_CODES:
                    raise BootstrapError(code)
            if result.returncode == 0 and value.get("state") == "READY":
                return result.stdout
        except (ValueError, TypeError, AttributeError):
            pass
        raise BootstrapError("DATABASE_BOOTSTRAP_RESULT_INVALID")
    if result.returncode:
        raise BootstrapError("COMMAND_FAILED")
    return result.stdout


def private_ip(value):
    try:
        address = ipaddress.ip_address(value)
        if (
            address.version != 4
            or not address.is_private
            or address.is_loopback
            or address.is_unspecified
        ):
            raise ValueError()
        return str(address)
    except (ValueError, TypeError):
        raise BootstrapError("PRIVATE_IPV4_REQUIRED") from None


def is_enabled(value):
    return str(value).lower() == "true"


def atomic_private_write(path, content):
    path = Path(path)
    if path.is_symlink():
        raise BootstrapError("SYMLINK_NOT_ALLOWED")
    previous = path.stat() if path.exists() else None
    if previous is not None and not stat.S_ISREG(previous.st_mode):
        raise BootstrapError("REGULAR_FILE_REQUIRED")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            if previous is not None:
                # Preserve the existing owner; root invocations must not steal env files.
                os.fchown(output.fileno(), previous.st_uid, previous.st_gid)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_env(path):
    path = Path(path)
    if path.is_symlink():
        raise BootstrapError("SYMLINK_NOT_ALLOWED")
    return path.read_text() if path.exists() else ""


def assignments(content):
    """Only identify keys/our safe markers. Compose is the actual env value parser."""
    values = {}
    for line in content.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if match:
            values[match[1]] = match[2].strip().strip("\"'")
    return values


def initial_bind(content, first):
    """Recognize only initial literals; let Compose resolve all actual custom values."""
    values = re.findall(r"^\s*(?:export\s+)?CHAT_BIND_ADDRESS\s*=(.*)$", content, re.MULTILINE)
    if not values:
        return True
    raw = values[-1].strip()
    for value in ("", "REPLACE_WITH_AI_PRIVATE_IP", *(("127.0.0.1",) if first else ())):
        literal = re.escape(value)
        if re.fullmatch(rf"(?:{literal}|'{literal}'|\"{literal}\")(?:\s*#.*)?", raw):
            return True
    return False


def updated_env(content, changes):
    for key, value in changes.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or "\n" in str(value) or "\r" in str(value):
            raise BootstrapError("INVALID_ENV_VALUE")
    lines = []
    for line in content.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if not match or match[1] not in changes:
            lines.append(line)
    for key, value in changes.items():
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
        lines.append(f'{key}="{escaped}"')
    return "\n".join(lines) + "\n"


def available_memory():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    raise BootstrapError("AI_MEMORY_PROBE_UNAVAILABLE")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def healthy(url, status):
    try:
        with build_opener(NoRedirect()).open(Request(url), timeout=3) as response:
            return (
                response.status == 200 and json.loads(response.read(65536)).get("status") == status
            )
    except Exception:
        return False


def api_readiness(environment):
    key = environment.get("INTERNAL_API_KEY") or ""
    if len(key) < 16:
        raise BootstrapError("INTERNAL_API_KEY_INVALID")
    try:
        request = Request(
            "http://127.0.0.1:8080/api/internal/v1/chat-runs/operations/readiness",
            headers={"X-Internal-Api-Key": key},
        )
        with build_opener(NoRedirect()).open(request, timeout=5) as response:
            value = json.loads(response.read(65536))
            flags = value["data"]
            if (
                response.status != 200
                or value.get("success") is not True
                or any(
                    type(flags[name]) is not bool
                    for name in ("operationsEnabled", "publicChatEnabled", "capabilityConfigured")
                )
            ):
                raise ValueError()
    except Exception:
        raise BootstrapError("API_OPERATIONS_READINESS_FAILED") from None
    if not flags["capabilityConfigured"]:
        raise BootstrapError("CAPABILITY_SECRET_INVALID")
    if not flags["operationsEnabled"]:
        raise BootstrapError("API_OPERATIONS_DISABLED")
    return flags


class Bootstrap:
    def __init__(self, directory, *, release=None, image=None):
        self.directory = Path(directory).resolve()
        self.release = Path(release or Path(__file__).resolve().parent).resolve()
        self.image = image
        if image is not None and not re.fullmatch(
            r"ghcr.io/catchhole-soma/catchhole-backend-ai:sha-[a-f0-9]{7,40}", image
        ):
            raise BootstrapError("IMMUTABLE_IMAGE_REQUIRED")
        self.environment = {
            key: value for key, value in os.environ.items() if key in SAFE_ENVIRONMENT
        }
        if image is not None:
            self.environment["AI_IMAGE"] = image
        self.private = self.directory / ".chat-bootstrap"
        self.state = {"schemaVersion": VERSION}

    @contextmanager
    def locked(self, *, api=False):
        if self.private.is_symlink():
            raise BootstrapError("SYMLINK_NOT_ALLOWED")
        self.private.mkdir(mode=0o700, exist_ok=True)
        if self.private.stat().st_uid != os.getuid():
            raise BootstrapError("BOOTSTRAP_DIRECTORY_OWNER_MISMATCH")
        os.chmod(self.private, 0o700)
        lock_path = self.directory / ".api-deploy.lock" if api else self.private / "host.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + 300
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BootstrapError("BOOTSTRAP_BUSY") from None
                    time.sleep(0.5)
            state_path = self.private / "state.json"
            if state_path.is_symlink():
                raise BootstrapError("SYMLINK_NOT_ALLOWED")
            if state_path.exists():
                metadata = state_path.stat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise BootstrapError("BOOTSTRAP_STATE_PERMISSIONS_INVALID")
                self.state = json.loads(state_path.read_text())
                if self.state.get("schemaVersion") != VERSION:
                    raise BootstrapError("BOOTSTRAP_STATE_VERSION_UNSUPPORTED")
            yield
        finally:
            os.close(fd)

    def save_state(self):
        atomic_private_write(self.private / "state.json", json.dumps(self.state, sort_keys=True))

    def compose(self, kind, *args, extra_env=None, timeout=180):
        files = [self.directory / ("api.env" if kind == "api" else "worker.env")]
        if kind not in {"api", "worker"}:
            files.append(Path(extra_env) if extra_env else self.directory / (kind + ".env"))
        if any(not path.is_file() or path.is_symlink() for path in files):
            raise BootstrapError("ENV_FILE_MISSING_OR_UNSAFE")
        compose_file = (self.directory if kind == "api" else self.release) / (
            "compose." + kind + ".prod.yml"
        )
        argv = ["docker", "compose"]
        for path in files:
            argv += ["--env-file", str(path)]
        argv += ["-f", str(compose_file), *args]
        return command(argv, environment=self.environment, timeout=timeout)

    def configuration(self, kind, *, candidate=None):
        temporary = None
        try:
            if candidate is not None:
                fd, temporary = tempfile.mkstemp(prefix=".candidate-", dir=self.private)
                with os.fdopen(fd, "w") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(candidate)
            result = json.loads(
                self.compose(kind, "config", "--format", "json", extra_env=temporary)
            )
            # Compose escapes dollars for replayable config output. Restore the
            # actual values before sending headers or literal docker-run env files.
            for service in result["services"].values():
                service["environment"] = {
                    key: value.replace("$$", "$") if isinstance(value, str) else value
                    for key, value in service.get("environment", {}).items()
                }
            return result["services"]
        except (ValueError, KeyError, TypeError):
            raise BootstrapError("COMPOSE_CONFIGURATION_INVALID") from None
        finally:
            if temporary is not None:
                os.unlink(temporary)

    @staticmethod
    def worker_connections(config):
        try:
            analysis = config["ai-worker"]
            replicas = int(analysis["deploy"]["replicas"])
            total = 0
            for name, count in (
                ("ai-worker", replicas),
                ("ai-character-comparison-worker", 1),
                ("ai-world-comparison-worker", 1),
            ):
                env = config[name]["environment"]
                size, overflow = (
                    int(env["DATABASE_POOL_SIZE"]),
                    int(env["DATABASE_POOL_MAX_OVERFLOW"]),
                )
                if size < 1 or overflow < 0 or count < 1:
                    raise ValueError()
                total += count * (size + overflow)
            return replicas, total
        except (KeyError, TypeError, ValueError):
            raise BootstrapError("WORKER_POOL_CONFIGURATION_INVALID") from None

    def inspect(self):
        config = self.configuration("worker")
        replicas, connections = self.worker_connections(config)
        try:
            url = urlsplit(config["ai-worker"]["environment"]["SPRING_INTERNAL_API_BASE_URL"])
            if (
                url.scheme not in {"http", "https"}
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError()
            address = private_ip(url.hostname)
            port = url.port or (443 if url.scheme == "https" else 80)
        except (KeyError, TypeError, ValueError):
            raise BootstrapError("SPRING_PRIVATE_URL_INVALID") from None
        return {
            "state": "INSPECTED",
            "apiPrivateIp": address,
            "apiPort": port,
            "workerDatabaseConnections": connections,
            "analysisReplicas": replicas,
        }

    def database_prepare(self, environment, required_connections, existing_url):
        if self.image is None:
            raise BootstrapError("IMMUTABLE_IMAGE_REQUIRED")
        command(["docker", "pull", self.image], environment=self.environment, timeout=600)
        values = {key: str(value) for key, value in environment.items() if value is not None}
        values.update(
            CHAT_BOOTSTRAP_OUTPUT_DIR="/bootstrap",
            CHAT_BOOTSTRAP_REQUIRED_CONNECTIONS=str(required_connections),
        )
        if existing_url:
            values["CHAT_RETRIEVAL_DATABASE_URL"] = existing_url
        fd, path = tempfile.mkstemp(prefix=".database-env-", dir=self.private)
        try:
            with os.fdopen(fd, "w") as output:
                os.fchmod(output.fileno(), 0o600)
                for key, value in values.items():
                    if (
                        "\n" in value
                        or "\r" in value
                        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                    ):
                        raise BootstrapError("INVALID_ENV_VALUE")
                    # docker run env-files are literal, unlike Compose interpolation.
                    output.write(f"{key}={value}\n")
            command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--env-file",
                    path,
                    "--mount",
                    f"type=bind,src={self.private},dst=/bootstrap",
                    self.image,
                    "python",
                    "-m",
                    "app.retrieval.bootstrap",
                    "prepare",
                ],
                environment=self.environment,
                timeout=240,
                database_result=True,
            )
        finally:
            os.unlink(path)
        reader = self.private / "reader.json"
        if reader.is_symlink() or not reader.is_file():
            raise BootstrapError("READER_CREDENTIALS_MISSING")
        metadata = reader.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise BootstrapError("READER_CREDENTIALS_PERMISSIONS_INVALID")
        try:
            url = json.loads(reader.read_text())["databaseUrl"]
            if not isinstance(url, str) or not url.startswith("postgresql+psycopg://"):
                raise ValueError()
            # The module authenticates the supplied DSN, then SQLAlchemy may
            # normalize its escaping/query order. Keep the operator's exact value.
            return existing_url or url
        except (ValueError, KeyError, TypeError):
            raise BootstrapError("READER_CREDENTIALS_INVALID") from None

    def prepare_ai(self, address, api_pool_size):
        address = private_ip(address)
        if api_pool_size < 1:
            raise BootstrapError("API_POOL_SIZE_INVALID")
        if available_memory() < 768 * 1024 * 1024:
            raise BootstrapError("AI_MEMORY_BELOW_768_MIB")
        with self.locked():
            _, worker_pool = self.worker_connections(self.configuration("worker"))
            paths = {kind: self.directory / (kind + ".env") for kind in ("chatbot", "retrieval")}
            texts = {kind: read_env(path) for kind, path in paths.items()}
            initialized = self.state.get("ai", {}).get("initialized", False)
            first_chat = not initialized and assignments(texts["chatbot"]).get(MARKER) != str(
                VERSION
            )
            first_retrieval = not initialized and assignments(texts["retrieval"]).get(
                MARKER
            ) != str(VERSION)
            chat_changes, retrieval_changes = {}, {}
            if initial_bind(texts["chatbot"], first_chat):
                chat_changes["CHAT_BIND_ADDRESS"] = address
            # The checked-in template is not valid Compose until its bind placeholder
            # is resolved. Parse a private candidate; publish only after DB validation.
            chat_config = self.configuration(
                "chatbot", candidate=updated_env(texts["chatbot"], chat_changes)
            )["chatbot"]
            try:
                ports = chat_config["ports"]
                if len(ports) != 1 or private_ip(ports[0]["host_ip"]) != address:
                    raise ValueError()
            except (KeyError, TypeError, ValueError, BootstrapError):
                raise BootstrapError("CHAT_BIND_ADDRESS_CONFLICT") from None
            chat = chat_config["environment"]
            retrieval = self.configuration("retrieval", candidate=texts["retrieval"])[
                "retrieval-index"
            ]["environment"]
            if first_chat:
                chat_changes.update(CHAT_QA_ENABLED="true", CHAT_RETRIEVAL_ENABLED="true")
            if first_retrieval:
                retrieval_changes.update(
                    RETRIEVAL_BACKFILL_ENABLED="true", RETRIEVAL_EMBEDDING_GENERATION_ENABLED="true"
                )
            chat_defaults = {
                "CHAT_QA_MODEL": "gpt-6-luna",
                "CHAT_QA_REASONING_EFFORT": "medium",
                "CHAT_RETRIEVAL_EMBEDDING_VERSION": retrieval.get("EMBEDDING_VERSION") or "v1",
            }
            retrieval_defaults = {
                "RETRIEVAL_BACKFILL_ID": "retrieval-v1-rollout",
                "RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS": "50000000",
                "RETRIEVAL_BACKFILL_ACTOR": "scheduled-retrieval",
                "EMBEDDING_MODEL": "text-embedding-3-small",
                "EMBEDDING_DIMENSIONS": "1536",
                "EMBEDDING_VERSION": "v1",
            }
            for key, default in chat_defaults.items():
                if not chat.get(key):
                    chat_changes[key] = default
            for key, default in retrieval_defaults.items():
                if not retrieval.get(key):
                    retrieval_changes[key] = default
            # A blank scope alongside explicit IDs is the existing allowlist contract.
            if not retrieval.get("RETRIEVAL_BACKFILL_SCOPE") and not retrieval.get(
                "RETRIEVAL_BACKFILL_WORK_IDS"
            ):
                retrieval_changes["RETRIEVAL_BACKFILL_SCOPE"] = "active-works"
            prospective_chat = {**chat, **chat_changes}
            prospective_retrieval = {**retrieval, **retrieval_changes}
            required = api_pool_size + worker_pool + 1
            required += 2 * is_enabled(prospective_chat.get("CHAT_RETRIEVAL_ENABLED"))
            required += 2 * is_enabled(prospective_retrieval.get("RETRIEVAL_BACKFILL_ENABLED"))
            if str(retrieval.get("AI_RUNTIME_API_DB_POOL_SIZE")) != str(api_pool_size):
                retrieval_changes["AI_RUNTIME_API_DB_POOL_SIZE"] = str(api_pool_size)
            budget = retrieval.get("AI_RUNTIME_DB_CONNECTION_BUDGET")
            if not budget or str(budget) == "0":
                retrieval_changes["AI_RUNTIME_DB_CONNECTION_BUDGET"] = str(required)
            else:
                try:
                    if int(budget) < required:
                        raise BootstrapError("EXISTING_DATABASE_BUDGET_INSUFFICIENT")
                except (ValueError, TypeError):
                    raise BootstrapError("DATABASE_BUDGET_INVALID") from None
            existing_url = chat.get("CHAT_RETRIEVAL_DATABASE_URL") or ""
            database_url = self.database_prepare(prospective_retrieval, required, existing_url)
            if not existing_url:
                chat_changes["CHAT_RETRIEVAL_DATABASE_URL"] = database_url
            for kind, changes in (("chatbot", chat_changes), ("retrieval", retrieval_changes)):
                if assignments(texts[kind]).get(MARKER) != str(VERSION):
                    changes[MARKER] = str(VERSION)
                if changes:
                    atomic_private_write(paths[kind], updated_env(texts[kind], changes))
            changed = [
                f"{kind}.{key}"
                for kind, changes in (("chatbot", chat_changes), ("retrieval", retrieval_changes))
                for key in changes
                if key != MARKER
            ]
            ai = self.state.setdefault("ai", {})
            ai.setdefault("initialChangedFields", changed)
            ai.update(initialized=True, changedFields=changed)
            self.save_state()
            return {
                "state": "AI_PREPARED",
                "requiredDatabaseConnections": required,
                "changedFields": changed,
            }

    def api_config(self):
        try:
            environment = self.configuration("api")["backend"]["environment"]
            pool = int(environment["DATABASE_POOL_MAXIMUM_SIZE"])
            if pool < 1:
                raise ValueError()
            return environment, pool
        except (KeyError, TypeError, ValueError):
            raise BootstrapError("API_POOL_CONFIGURATION_INVALID") from None

    def wait_api(self):
        for attempt in range(30):
            if healthy("http://127.0.0.1:8080/actuator/health", "UP"):
                return
            if attempt < 29:
                time.sleep(2)
        raise BootstrapError("API_HEALTH_NOT_READY")

    def restart_api(self):
        self.compose(
            "api",
            "up",
            "-d",
            "--no-deps",
            "--pull",
            "never",
            "--force-recreate",
            "backend",
            timeout=180,
        )
        self.wait_api()

    def prepare_api(self, address):
        address = private_ip(address)
        with self.locked(api=True):
            env, pool = self.api_config()
            api = self.state.setdefault("api", {})
            path = self.directory / "api.env"
            content = read_env(path)
            if not api.get("prepared"):
                already_written = assignments(content).get(MARKER) == str(VERSION)
                if "pendingChanges" not in api and not already_written:
                    secret = env.get("CHAT_CAPABILITY_SECRET") or secrets.token_hex(32)
                    if len(secret) < 32:
                        raise BootstrapError("CAPABILITY_SECRET_INVALID")
                    changes = {"CHAT_OPERATIONS_ENABLED": "true", MARKER: str(VERSION)}
                    if not env.get("CHAT_CAPABILITY_SECRET"):
                        changes["CHAT_CAPABILITY_SECRET"] = secret
                    if not env.get("CHAT_PYTHON_URL"):
                        changes["CHAT_PYTHON_URL"] = f"http://{address}:8082"
                    if "CHAT_ENABLED" not in assignments(content):
                        changes["CHAT_ENABLED"] = (
                            "true" if is_enabled(env.get("CHAT_ENABLED")) else "false"
                        )
                    restart = any(
                        str(env.get(key, "")) != value
                        for key, value in changes.items()
                        if key != MARKER
                    )
                    api.update(
                        pendingChanges=changes,
                        restartRequired=restart,
                        activationPending=not is_enabled(env.get("CHAT_ENABLED")),
                    )
                    self.save_state()  # durable generated secret before env mutation
                if not already_written:
                    atomic_private_write(path, updated_env(content, api["pendingChanges"]))
                # A previous crash may have written env but not recreated the process.
                if api.get("restartRequired"):
                    self.restart_api()
                else:
                    self.wait_api()
                api.pop("pendingChanges", None)
                api.pop("restartRequired", None)
                api["prepared"] = True
                self.save_state()
            else:
                self.wait_api()
            return {
                "state": "API_PREPARED",
                "apiPoolSize": pool,
                "activationPending": bool(api.get("activationPending", False)),
            }

    def activate_api(self, address):
        address = private_ip(address)
        with self.locked(api=True):
            env, _ = self.api_config()
            api = self.state.get("api", {})
            enabled = is_enabled(env.get("CHAT_ENABLED"))
            if not api.get("prepared"):
                raise BootstrapError("API_BOOTSTRAP_NOT_PREPARED")
            if not api.get("activationPending"):
                return {
                    "state": "API_ALREADY_ENABLED" if enabled else "API_ACTIVATION_SKIPPED",
                    "publicChatEnabled": enabled,
                }
            if (env.get("CHAT_PYTHON_URL") or "").rstrip("/") != f"http://{address}:8082":
                raise BootstrapError("CHAT_PYTHON_URL_CONFLICT")
            if not healthy(f"http://{address}:8082/healthz", "ready"):
                raise BootstrapError("CHAT_HEALTH_NOT_READY")
            flags = api_readiness(env)
            path = self.directory / "api.env"
            content = read_env(path)
            activation_id = api.get("activationId")
            if activation_id and assignments(content).get(ACTIVATION_MARKER) == activation_id:
                if not enabled:
                    # The atomic activation write already happened; a later OFF is operator intent.
                    api.update(activationPending=False, activationDone=True)
                    self.save_state()
                    return {"state": "API_ACTIVATION_SKIPPED", "publicChatEnabled": False}
            else:
                if enabled and flags["publicChatEnabled"]:
                    api.update(activationPending=False, activationDone=True)
                    self.save_state()
                    return {"state": "API_ALREADY_ENABLED", "publicChatEnabled": True}
                activation_id = activation_id or secrets.token_hex(16)
                api["activationId"] = activation_id
                self.save_state()
                atomic_private_write(
                    path,
                    updated_env(
                        content, {"CHAT_ENABLED": "true", ACTIVATION_MARKER: activation_id}
                    ),
                )
            self.restart_api()
            if not api_readiness({**env, "CHAT_ENABLED": "true"})["publicChatEnabled"]:
                raise BootstrapError("API_PUBLIC_CHAT_NOT_ENABLED")
            api.update(activationPending=False, activationDone=True)
            self.save_state()
            return {"state": "API_ACTIVATED", "publicChatEnabled": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "prepare-ai", "prepare-api", "activate-api"))
    parser.add_argument("--directory", default="/opt/catchhole")
    parser.add_argument("--release")
    parser.add_argument("--image")
    parser.add_argument("--private-ip")
    parser.add_argument("--api-pool-size", type=int)
    args = parser.parse_args(argv)
    try:
        runtime = Bootstrap(args.directory, release=args.release, image=args.image)
        if args.mode == "inspect":
            result = runtime.inspect()
        elif args.mode == "prepare-ai":
            if args.api_pool_size is None:
                raise BootstrapError("API_POOL_SIZE_REQUIRED")
            result = runtime.prepare_ai(args.private_ip, args.api_pool_size)
        elif args.mode == "prepare-api":
            result = runtime.prepare_api(args.private_ip)
        else:
            result = runtime.activate_api(args.private_ip)
        print(json.dumps(result, sort_keys=True))
        return 0
    except BootstrapError as exc:
        print(json.dumps({"state": "FAILED", "code": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"state": "FAILED", "code": "BOOTSTRAP_CONFIGURATION_FAILURE"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
