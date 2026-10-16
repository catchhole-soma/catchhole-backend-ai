#!/usr/bin/env python3
"""GitHub runner orchestration for chatbot rollout; never relay remote logs."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

REPOSITORY = "catchhole-soma/catchhole-backend-ai"
API_DIRECTORY = "/opt/catchhole"
API_USER = "ubuntu"
RELEASE_FILES = (
    "bootstrap-chat-runtime.py",
    "manage-ai-runtime.py",
    "compose.worker.prod.yml",
    "compose.chatbot.prod.yml",
    "compose.retrieval.prod.yml",
    "install-chat-scheduler.sh",
    "chatbot.env.example",
    "retrieval.env.example",
)


class BootstrapError(Exception):
    pass


class Superseded(BootstrapError):
    pass


def execute(arguments, *, timeout=90):
    try:
        return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise BootstrapError("COMMAND_UNAVAILABLE_OR_TIMEOUT") from None


def private_ip(value):
    try:
        address = ipaddress.ip_address(value)
        if address.version != 4 or not any(
            address in ipaddress.ip_network(network)
            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ):
            raise ValueError()
        return str(address)
    except ValueError:
        raise BootstrapError("PRIVATE_IPV4_REQUIRED") from None


def safe_remote_result(output):
    # Setup commands and Compose can add banners; only consume the final JSON contract.
    for line in reversed(output.splitlines()):
        try:
            result = json.loads(line)
        except ValueError:
            continue
        if isinstance(result, dict) and isinstance(result.get("state"), str):
            return result
    raise BootstrapError("REMOTE_RESULT_MISSING")


class Aws:
    def call(self, service, operation, *arguments, allowed_errors=()):
        result = execute(["aws", service, operation, *arguments, "--output", "json"])
        if result.returncode:
            for code in allowed_errors:
                if f"({code})" in result.stderr:
                    return {"awsError": code}
            permission = any(
                code in result.stderr
                for code in ("AccessDenied", "UnauthorizedOperation", "not authorized")
            )
            suffix = "PERMISSION_DENIED" if permission else "FAILED"
            name = re.sub("[^A-Z0-9]", "_", f"{service}_{operation}".upper())
            raise BootstrapError(f"AWS_{name}_{suffix}")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise BootstrapError("AWS_RESPONSE_INVALID") from None

    def remote(self, instance, script, *, timeout=900):
        response = self.call(
            "ssm",
            "send-command",
            "--instance-ids",
            instance,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            json.dumps({"commands": [script], "executionTimeout": [str(timeout)]}),
        )
        command_id = response["Command"]["CommandId"]
        deadline = time.monotonic() + timeout + 60
        while time.monotonic() < deadline:
            invocation = self.call(
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance,
                allowed_errors=("InvocationDoesNotExist",),
            )
            status = invocation.get("Status")
            if status == "Success":
                return safe_remote_result(invocation.get("StandardOutputContent", ""))
            if status in {"Cancelled", "TimedOut", "Failed", "Cancelling"}:
                try:
                    failure = safe_remote_result(invocation.get("StandardOutputContent", ""))
                except BootstrapError:
                    failure = {}
                code = failure.get("code", "")
                if failure.get("state") == "FAILED" and re.fullmatch(
                    r"[A-Z][A-Z0-9_]{0,100}", code
                ):
                    raise BootstrapError("REMOTE_" + code)
                raise BootstrapError("REMOTE_COMMAND_FAILED")
            time.sleep(5)
        raise BootstrapError("REMOTE_COMMAND_TIMEOUT")


class Rollout:
    def __init__(self, sha, directory, user, *, aws=None):
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise BootstrapError("IMMUTABLE_SHA_REQUIRED")
        if (
            not re.fullmatch(r"/[A-Za-z0-9_./-]+", directory)
            or ".." in Path(directory).parts
            or str(Path(directory)) == "/"
        ):
            raise BootstrapError("DEPLOY_DIRECTORY_INVALID")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
            raise BootstrapError("DEPLOY_USER_INVALID")
        self.sha, self.directory, self.user = sha, directory.rstrip("/"), user
        self.release = f"{self.directory}/releases/{sha}"
        self.api_release = f"{API_DIRECTORY}/chat-bootstrap-releases/{sha}"
        self.image = f"ghcr.io/{REPOSITORY}:sha-{sha[:7]}"
        self.aws = aws or Aws()

    def current(self):
        result = execute(["gh", "api", f"repos/{REPOSITORY}/commits/main", "--jq", ".sha"])
        if result.returncode:
            raise BootstrapError("CURRENT_MAIN_CHECK_FAILED")
        if result.stdout.strip() != self.sha:
            raise Superseded("SUPERSEDED_PUBLISH")

    def remote(self, instance, script, *, mutation=False, timeout=900):
        if mutation:
            self.current()
        return self.aws.remote(instance, script, timeout=timeout)

    def instances(self, *arguments):
        result = self.aws.call("ec2", "describe-instances", *arguments)
        return [item for group in result.get("Reservations", []) for item in group["Instances"]]

    @staticmethod
    def group(instance, expected):
        groups = [
            g["GroupId"] for g in instance.get("SecurityGroups", []) if g["GroupName"] == expected
        ]
        if len(groups) != 1 or not re.fullmatch(r"sg-[a-f0-9]+", groups[0]):
            raise BootstrapError("EXPECTED_SECURITY_GROUP_NOT_ATTACHED")
        return groups[0]

    def download(self, instance, *, api=False):
        release = self.api_release if api else self.release
        user = API_USER if api else self.user
        files = ("bootstrap-chat-runtime.py",) if api else RELEASE_FILES
        lines = ["set -eu", f"mkdir -p {shlex.quote(release)}"]
        base = f"https://raw.githubusercontent.com/{REPOSITORY}/{self.sha}/deploy"
        for name in files:
            path = f"{release}/{name}"
            lines += [
                f"curl --connect-timeout 10 --max-time 60 -fsSL {shlex.quote(base + '/' + name)} -o {shlex.quote(path + '.tmp')}",
                f"mv {shlex.quote(path + '.tmp')} {shlex.quote(path)}",
            ]
        lines += [
            f"chown -R {user}:{user} {shlex.quote(release)}",
            'echo \'{"state":"DOWNLOADED"}\'',
        ]
        self.expect(self.remote(instance, "\n".join(lines), mutation=True), {"DOWNLOADED"})

    def host(self, instance, mode, *arguments, api=False, mutation=False):
        release = self.api_release if api else self.release
        user = API_USER if api else self.user
        directory = API_DIRECTORY if api else self.directory
        command = [
            "runuser",
            "-u",
            user,
            "--",
            "python3",
            f"{release}/bootstrap-chat-runtime.py",
            mode,
            "--directory",
            directory,
        ]
        if not api:
            command += ["--release", self.release, "--image", self.image]
        command += list(arguments)
        return self.remote(instance, "set -eu\n" + shlex.join(command), mutation=mutation)

    @staticmethod
    def expect(result, states):
        if result.get("state") not in states:
            raise BootstrapError("REMOTE_STATE_UNEXPECTED")

    def ensure_ingress(self, worker_group, api_group, vpc):
        groups = self.aws.call(
            "ec2", "describe-security-groups", "--group-ids", worker_group, api_group
        ).get("SecurityGroups", [])
        by_id = {g["GroupId"]: g for g in groups}
        if set(by_id) != {worker_group, api_group} or any(g.get("VpcId") != vpc for g in groups):
            raise BootstrapError("SECURITY_GROUP_VPC_MISMATCH")
        permission = {
            "IpProtocol": "tcp",
            "FromPort": 8082,
            "ToPort": 8082,
            "UserIdGroupPairs": [{"GroupId": api_group}],
        }
        for rule in by_id[worker_group].get("IpPermissions", []):
            if (
                rule.get("IpProtocol") == "tcp"
                and rule.get("FromPort") == 8082
                and rule.get("ToPort") == 8082
            ):
                if any(
                    pair.get("GroupId") == api_group for pair in rule.get("UserIdGroupPairs", [])
                ):
                    return
        self.current()
        self.aws.call(
            "ec2",
            "authorize-security-group-ingress",
            "--group-id",
            worker_group,
            "--ip-permissions",
            json.dumps([permission]),
            allowed_errors=("InvalidPermission.Duplicate",),
        )

    def prepare(self, worker_id):
        self.current()
        if not re.fullmatch(r"i-[a-f0-9]+", worker_id):
            raise BootstrapError("WORKER_INSTANCE_ID_INVALID")
        workers = self.instances("--instance-ids", worker_id)
        if len(workers) != 1 or workers[0].get("State", {}).get("Name") != "running":
            raise BootstrapError("WORKER_INSTANCE_NOT_RUNNING")
        worker = workers[0]
        worker_ip = private_ip(worker["PrivateIpAddress"])
        worker_group = self.group(worker, "catchhole-worker-prod-sg")
        self.download(worker_id)
        inspected = self.host(worker_id, "inspect")
        self.expect(inspected, {"INSPECTED"})
        api_ip = private_ip(inspected.get("apiPrivateIp", ""))
        if type(inspected.get("apiPort")) is not int or inspected["apiPort"] != 8080:
            raise BootstrapError("API_PRIVATE_PORT_UNEXPECTED")
        candidates = self.instances(
            "--filters",
            f"Name=private-ip-address,Values={api_ip}",
            f"Name=vpc-id,Values={worker['VpcId']}",
            "Name=instance-state-name,Values=running",
        )
        if len(candidates) != 1 or candidates[0].get("InstanceId") == worker_id:
            raise BootstrapError("API_INSTANCE_AMBIGUOUS")
        api = candidates[0]
        if api.get("VpcId") != worker["VpcId"] or api.get("PrivateIpAddress") != api_ip:
            raise BootstrapError("API_INSTANCE_SCOPE_MISMATCH")
        api_group = self.group(api, "catchhole-api-prod-sg")
        self.ensure_ingress(worker_group, api_group, worker["VpcId"])
        self.download(api["InstanceId"], api=True)
        prepared = self.host(
            api["InstanceId"], "prepare-api", "--private-ip", worker_ip, api=True, mutation=True
        )
        self.expect(prepared, {"API_PREPARED"})
        if type(prepared.get("apiPoolSize")) is not int or prepared["apiPoolSize"] < 1:
            raise BootstrapError("API_POOL_SIZE_INVALID")
        if type(prepared.get("activationPending")) is not bool:
            raise BootstrapError("API_ACTIVATION_STATE_INVALID")
        self.expect(
            self.host(
                worker_id,
                "prepare-ai",
                "--private-ip",
                worker_ip,
                "--api-pool-size",
                str(prepared["apiPoolSize"]),
                mutation=True,
            ),
            {"AI_PREPARED"},
        )
        return {
            "state": "PREPARED",
            "sha": self.sha,
            "workerInstanceId": worker_id,
            "apiInstanceId": api["InstanceId"],
            "workerPrivateIp": worker_ip,
            "apiPrivateIp": api_ip,
            "activationPending": prepared["activationPending"],
        }

    def deploy(self, context):
        # Java owns its checkout; only AI-owned files are installed on the worker host.
        d, r, user, image = map(shlex.quote, (self.directory, self.release, self.user, self.image))
        script = f"""set -eu
runuser -u {user} -- python3 {r}/manage-ai-runtime.py deploy --directory {d} --release {r} --image {image}
ln -sfn {r} {d}/.current-chat-release.new
mv -Tf {d}/.current-chat-release.new {d}/current-chat-release
sed -i 's|^AI_IMAGE=.*|AI_IMAGE={self.image}|' {d}/worker.env
for kind in worker chatbot retrieval; do
  cp {r}/compose.$kind.prod.yml {d}/compose.$kind.prod.yml
  chown {user}:{user} {d}/compose.$kind.prod.yml
done
bash {r}/install-chat-scheduler.sh {d} {user}
echo '{{"state":"DEPLOYED"}}'
"""
        result = self.remote(context["workerInstanceId"], script, mutation=True, timeout=1800)
        self.expect(result, {"DEPLOYED"})
        return {"state": "DEPLOYED", "activationPending": context["activationPending"]}

    def backfill_status(self, context):
        command = [
            "runuser",
            "-u",
            self.user,
            "--",
            "python3",
            f"{self.release}/manage-ai-runtime.py",
            "status",
            "--directory",
            self.directory,
            "--release",
            self.release,
            "--image",
            self.image,
        ]
        return self.remote(
            context["workerInstanceId"], "set -eu\n" + shlex.join(command), timeout=120
        )

    def verify_runtime(self, context):
        # Reuse the existing readiness implementation without printing Compose config.
        # A READY SQLite snapshot alone cannot override an operator disabling a gate.
        command = shlex.join(
            [
                "runuser",
                "-u",
                self.user,
                "--",
                "python3",
                "-",
                self.release,
                self.directory,
                self.image,
            ]
        )
        script = (
            command
            + " <<'PY'\n"
            + """import importlib.util
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("runtime", Path(sys.argv[1]) / "manage-ai-runtime.py")
module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(module)
    runtime = module.Runtime(sys.argv[2], sys.argv[1], sys.argv[3])
    flags = runtime.validate()
    chat = runtime.config("chatbot")["chatbot"]["environment"]
    if not flags["indexing"] or not module.enabled(chat.get("CHAT_RETRIEVAL_ENABLED")):
        raise ValueError()
    runtime.preflight()
    output = runtime.compose("chatbot", "exec", "-T", "chatbot", "python", "-m", "app.chatbot.readiness", timeout=60, allowed_codes=(0, 2))
    runtime.verify_ready(output, "READY")
    live = json.loads(output)
    if live.get("checks", {}).get("retrieval") != "READY" or live.get("operationsEnabled") is not True:
        raise ValueError()
    print(json.dumps({"state": "AI_READY"}))
except Exception:
    print(json.dumps({"state": "FAILED", "code": "INITIAL_ACTIVATION_RUNTIME_NOT_READY"}))
    sys.exit(2)
PY
"""
        )
        self.expect(self.remote(context["workerInstanceId"], script, timeout=900), {"AI_READY"})

    @staticmethod
    def indexed(result):
        if any(
            type(result.get(key)) is not int or result[key] != 0
            for key in ("excludedOrUnavailableSources", "pendingReports")
        ):
            return False
        if (
            result.get("uncertainAttempt") is not False
            or result.get("sweepInProgress") is not False
        ):
            return False
        if type(result.get("sweep")) is not int or result["sweep"] < 1:
            return False
        checked, ready = result.get("checkedSources"), result.get("readySources")
        if type(checked) is not int or type(ready) is not int or checked != ready:
            return False
        return (result.get("state") == "READY" and checked > 0) or (
            result.get("state") == "EMPTY" and checked == 0
        )

    def finish(self, context, *, wait_seconds=1800):
        self.current()
        if not context["activationPending"]:
            return {"state": "INITIAL_ACTIVATION_NOT_PENDING"}
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            self.current()
            status = self.backfill_status(context)
            if self.indexed(status):
                self.verify_runtime(context)
                if not self.indexed(self.backfill_status(context)):
                    time.sleep(30)
                    continue
                result = self.host(
                    context["apiInstanceId"],
                    "activate-api",
                    "--private-ip",
                    context["workerPrivateIp"],
                    api=True,
                    mutation=True,
                )
                self.expect(
                    result, {"API_ACTIVATED", "API_ALREADY_ENABLED", "API_ACTIVATION_SKIPPED"}
                )
                return {"state": result["state"]}
            if status.get("state") in {"BLOCKED", "PARTIAL", "BUDGET_EXHAUSTED", "DISABLED"}:
                raise BootstrapError("INITIAL_ACTIVATION_BLOCKED_" + status["state"])
            time.sleep(30)
        raise BootstrapError("INITIAL_BACKFILL_PENDING_PUBLIC_CHAT_REMAINS_CLOSED")


def save_context(path, context):
    # Only identifiers and addresses; never env files or remote raw output.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(context, stream)


def load_context(path, sha):
    result = json.loads(Path(path).read_text())
    expected = {
        "state",
        "sha",
        "workerInstanceId",
        "apiInstanceId",
        "workerPrivateIp",
        "apiPrivateIp",
        "activationPending",
    }
    if set(result) != expected or result["state"] != "PREPARED" or result["sha"] != sha:
        raise BootstrapError("BOOTSTRAP_CONTEXT_INVALID")
    if type(result["activationPending"]) is not bool:
        raise BootstrapError("BOOTSTRAP_CONTEXT_INVALID")
    for key in ("workerInstanceId", "apiInstanceId"):
        if not re.fullmatch(r"i-[a-f0-9]+", result[key]):
            raise BootstrapError("BOOTSTRAP_CONTEXT_INVALID")
    for key in ("workerPrivateIp", "apiPrivateIp"):
        private_ip(result[key])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "deploy", "finish"))
    parser.add_argument("--worker-instance-id")
    parser.add_argument("--directory", default="/opt/catchhole")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    try:
        rollout = Rollout(args.sha, args.directory, args.user)
        if args.mode == "prepare":
            context = rollout.prepare(args.worker_instance_id or "")
            save_context(args.context, context)
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write("prepared=true\n")
            result = {"state": "PREPARED", "activationPending": context["activationPending"]}
        else:
            context = load_context(args.context, args.sha)
            result = getattr(rollout, args.mode)(context)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Superseded:
        print(json.dumps({"state": "SKIPPED", "code": "SUPERSEDED_PUBLISH"}))
        return 0
    except BootstrapError as exc:
        print(json.dumps({"state": "FAILED", "code": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"state": "FAILED", "code": "BOOTSTRAP_UNEXPECTED_FAILURE"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
