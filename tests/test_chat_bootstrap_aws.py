"""Synthetic AWS/SSM boundaries: no credentials, instances, or paid calls."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "bootstrap_aws", ROOT / "deploy/bootstrap-chat-aws.py"
)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
SHA = "a" * 40


class FakeAws:
    def __init__(self):
        self.calls = []
        self.scripts = []
        self.worker = {
            "InstanceId": "i-aaa",
            "PrivateIpAddress": "10.0.0.2",
            "VpcId": "vpc-aaa",
            "State": {"Name": "running"},
            "SecurityGroups": [{"GroupId": "sg-aaa", "GroupName": "catchhole-worker-prod-sg"}],
        }
        self.api = {
            "InstanceId": "i-bbb",
            "PrivateIpAddress": "10.0.0.1",
            "VpcId": "vpc-aaa",
            "State": {"Name": "running"},
            "SecurityGroups": [{"GroupId": "sg-bbb", "GroupName": "catchhole-api-prod-sg"}],
        }
        self.groups = [
            {"GroupId": "sg-aaa", "VpcId": "vpc-aaa", "IpPermissions": []},
            {"GroupId": "sg-bbb", "VpcId": "vpc-aaa", "IpPermissions": []},
        ]
        self.candidates = [self.api]
        self.pending = True
        self.status = ready_status()

    def call(self, service, operation, *arguments, **kwargs):
        self.calls.append((service, operation, arguments, kwargs))
        if operation == "describe-instances":
            instances = [self.worker] if "--instance-ids" in arguments else self.candidates
            return {"Reservations": [{"Instances": instances}]}
        if operation == "describe-security-groups":
            return {"SecurityGroups": self.groups}
        if operation == "authorize-security-group-ingress":
            return {"Return": True}
        raise AssertionError(operation)

    def remote(self, instance, script, **kwargs):
        self.scripts.append((instance, script))
        if "curl --connect-timeout" in script:
            return {"state": "DOWNLOADED"}
        if "bootstrap-chat-runtime.py inspect " in script:
            return {"state": "INSPECTED", "apiPrivateIp": "10.0.0.1", "apiPort": 8080}
        if "prepare-api" in script:
            return {"state": "API_PREPARED", "apiPoolSize": 10, "activationPending": self.pending}
        if "prepare-ai" in script:
            return {"state": "AI_PREPARED"}
        if "activate-api" in script:
            return {"state": "API_ACTIVATED", "publicChatEnabled": True}
        if "INITIAL_ACTIVATION_RUNTIME_NOT_READY" in script:
            return {"state": "AI_READY"}
        if "manage-ai-runtime.py status" in script:
            return self.status
        if "manage-ai-runtime.py deploy" in script:
            return {"state": "DEPLOYED"}
        raise AssertionError(script)


def ready_status(**changes):
    return {
        "state": "READY",
        "sweep": 1,
        "checkedSources": 400,
        "readySources": 400,
        "excludedOrUnavailableSources": 0,
        "pendingReports": 0,
        "uncertainAttempt": False,
        "sweepInProgress": False,
        **changes,
    }


@pytest.fixture
def rollout(monkeypatch):
    aws = FakeAws()
    obj = bootstrap.Rollout(SHA, "/opt/catchhole", "ubuntu", aws=aws)
    monkeypatch.setattr(obj, "current", lambda: None)
    return obj, aws


def test_prepare_discovers_api_in_worker_vpc_and_adds_only_exact_group_rule(rollout):
    obj, aws = rollout
    context = obj.prepare("i-aaa")
    assert context["apiInstanceId"] == "i-bbb"
    assert context["activationPending"] is True
    rules = [call for call in aws.calls if call[1] == "authorize-security-group-ingress"]
    assert len(rules) == 1
    args = rules[0][2]
    permission = json.loads(args[args.index("--ip-permissions") + 1])
    assert permission == [
        {
            "IpProtocol": "tcp",
            "FromPort": 8082,
            "ToPort": 8082,
            "UserIdGroupPairs": [{"GroupId": "sg-bbb"}],
        }
    ]
    assert "Name=vpc-id,Values=vpc-aaa" in aws.calls[1][2]


def test_release_download_is_sha_pinned_and_api_compose_is_never_replaced(rollout):
    obj, aws = rollout
    obj.prepare("i-aaa")
    downloads = [script for _, script in aws.scripts if "curl --connect-timeout" in script]
    assert len(downloads) == 2
    assert all(f"/{SHA}/deploy/" in script and "/main/" not in script for script in downloads)
    api_download = next(
        script for instance, script in aws.scripts if instance == "i-bbb" and "curl" in script
    )
    assert f"/chat-bootstrap-releases/{SHA}/bootstrap-chat-runtime.py" in api_download
    assert "compose.api" not in api_download and "compose.worker" not in api_download


@pytest.mark.parametrize("target", ["worker", "api"])
def test_unexpected_attached_security_group_is_not_modified(rollout, target):
    obj, aws = rollout
    getattr(aws, target)["SecurityGroups"][0]["GroupName"] = "unrelated-sg"
    with pytest.raises(bootstrap.BootstrapError, match="EXPECTED_SECURITY_GROUP"):
        obj.prepare("i-aaa")
    assert not any(call[1] == "authorize-security-group-ingress" for call in aws.calls)
    assert not any("prepare-api" in script for _, script in aws.scripts)


@pytest.mark.parametrize("count", [0, 2])
def test_api_discovery_requires_unique_instance(rollout, count):
    obj, aws = rollout
    aws.candidates = [aws.api] * count
    with pytest.raises(bootstrap.BootstrapError, match="API_INSTANCE_AMBIGUOUS"):
        obj.prepare("i-aaa")
    assert not any(call[1] == "authorize-security-group-ingress" for call in aws.calls)


def test_group_from_different_vpc_cannot_receive_rule(rollout):
    obj, aws = rollout
    aws.groups[0]["VpcId"] = "vpc-other"
    with pytest.raises(bootstrap.BootstrapError, match="VPC_MISMATCH"):
        obj.prepare("i-aaa")


def test_existing_rule_makes_prepare_idempotent(rollout):
    obj, aws = rollout
    aws.groups[0]["IpPermissions"] = [
        {
            "IpProtocol": "tcp",
            "FromPort": 8082,
            "ToPort": 8082,
            "UserIdGroupPairs": [{"GroupId": "sg-bbb"}],
        }
    ]
    obj.prepare("i-aaa")
    assert not any(call[1] == "authorize-security-group-ingress" for call in aws.calls)


def test_aws_permission_failure_is_specific_without_secret(monkeypatch):
    monkeypatch.setattr(
        bootstrap,
        "execute",
        lambda args: subprocess.CompletedProcess(
            args, 1, "private-output", "An error occurred (UnauthorizedOperation): private-password"
        ),
    )
    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap.Aws().call("ec2", "authorize-security-group-ingress")
    assert str(exc.value) == "AWS_EC2_AUTHORIZE_SECURITY_GROUP_INGRESS_PERMISSION_DENIED"


def test_duplicate_permission_race_is_successful(monkeypatch):
    monkeypatch.setattr(
        bootstrap,
        "execute",
        lambda args: subprocess.CompletedProcess(
            args, 1, "", "An error occurred (InvalidPermission.Duplicate)"
        ),
    )
    assert bootstrap.Aws().call(
        "ec2", "authorize-security-group-ingress", allowed_errors=("InvalidPermission.Duplicate",)
    ) == {"awsError": "InvalidPermission.Duplicate"}


@pytest.mark.parametrize(
    "output,expected",
    [
        (
            '{"state":"FAILED","code":"DATABASE_PERMISSION_REQUIRED"}',
            "REMOTE_DATABASE_PERMISSION_REQUIRED",
        ),
        ("private-password postgres://secret@host/db", "REMOTE_COMMAND_FAILED"),
    ],
)
def test_failed_ssm_never_relays_raw_output_or_stderr(monkeypatch, output, expected):
    aws = bootstrap.Aws()
    values = iter(
        [
            {"Command": {"CommandId": "test"}},
            {
                "Status": "Failed",
                "StandardOutputContent": output,
                "StandardErrorContent": "private-password",
            },
        ]
    )
    monkeypatch.setattr(aws, "call", lambda *args, **kwargs: next(values))
    with pytest.raises(bootstrap.BootstrapError) as exc:
        aws.remote("i-aaa", "safe-script")
    assert str(exc.value) == expected


def test_ssm_eventual_consistency_waits_without_printing_remote_output(monkeypatch):
    aws = bootstrap.Aws()
    values = iter(
        [
            {"Command": {"CommandId": "test"}},
            {"awsError": "InvocationDoesNotExist"},
            {
                "Status": "Success",
                "StandardOutputContent": 'private-banner\n{"state":"READY"}',
                "StandardErrorContent": "private",
            },
        ]
    )
    monkeypatch.setattr(aws, "call", lambda *args, **kwargs: next(values))
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    assert aws.remote("i-aaa", "safe-script") == {"state": "READY"}


def test_ssm_and_ingress_mutations_recheck_current_main(rollout, monkeypatch):
    obj, aws = rollout
    checks = []
    monkeypatch.setattr(obj, "current", lambda: checks.append(True))
    obj.prepare("i-aaa")
    # Initial gate + worker download + ingress + API download + both prepares.
    assert len(checks) == 6
    monkeypatch.setattr(obj, "current", lambda: (_ for _ in ()).throw(bootstrap.Superseded()))
    before = len(aws.scripts)
    with pytest.raises(bootstrap.Superseded):
        obj.remote("i-aaa", "mutation", mutation=True)
    assert len(aws.scripts) == before


def test_finish_only_opens_after_complete_indexing(rollout):
    obj, aws = rollout
    context = obj.prepare("i-aaa")
    assert obj.finish(context) == {"state": "API_ACTIVATED"}
    assert "activate-api" in aws.scripts[-1][1]


def test_already_finished_initialization_respects_later_manual_disable(rollout):
    obj, aws = rollout
    aws.pending = False
    context = obj.prepare("i-aaa")
    before = len(aws.scripts)
    assert obj.finish(context) == {"state": "INITIAL_ACTIVATION_NOT_PENDING"}
    assert len(aws.scripts) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"pendingReports": 1},
        {"pendingReports": None},
        {"uncertainAttempt": True},
        {"sweepInProgress": True},
        {"excludedOrUnavailableSources": 1},
        {"sweep": 0},
        {"checkedSources": 401},
        {"checkedSources": 0, "readySources": 0},
        {"state": "PARTIAL"},
        {"state": "INDEX_BUSY"},
        {"readySources": True},
    ],
)
def test_incomplete_status_never_qualifies_for_activation(changes):
    assert not bootstrap.Rollout.indexed(ready_status(**changes))


def test_empty_completed_sweep_is_legitimate_without_fake_sources():
    assert bootstrap.Rollout.indexed(ready_status(state="EMPTY", checkedSources=0, readySources=0))


@pytest.mark.parametrize("state", ["BLOCKED", "PARTIAL", "BUDGET_EXHAUSTED", "DISABLED"])
def test_blocked_backfill_stops_activation_and_keeps_timer(rollout, state):
    obj, aws = rollout
    context = obj.prepare("i-aaa")
    aws.status = ready_status(state=state)
    with pytest.raises(bootstrap.BootstrapError, match="INITIAL_ACTIVATION_BLOCKED_" + state):
        obj.finish(context)
    assert not any(
        "activate-api" in script or "systemctl stop" in script for _, script in aws.scripts
    )


def test_first_backfill_timeout_leaves_public_gate_closed(rollout):
    obj, aws = rollout
    context = obj.prepare("i-aaa")
    with pytest.raises(bootstrap.BootstrapError, match="INITIAL_BACKFILL_PENDING"):
        obj.finish(context, wait_seconds=0)
    assert not any("activate-api" in script for _, script in aws.scripts)


def test_deploy_preserves_scheduler_and_immutable_release(rollout):
    obj, aws = rollout
    context = obj.prepare("i-aaa")
    obj.deploy(context)
    script = aws.scripts[-1][1]
    assert f"/releases/{SHA}/manage-ai-runtime.py deploy" in script
    assert "install-chat-scheduler.sh" in script
    assert script.index("manage-ai-runtime.py deploy") < script.index("current-chat-release")
    assert "systemctl stop" not in script
    subprocess.run(["sh", "-n"], input=script, text=True, check=True)


def test_context_file_is_private_and_contains_no_extra_remote_fields(rollout, tmp_path):
    obj, _ = rollout
    context = obj.prepare("i-aaa")
    path = tmp_path / "context.json"
    bootstrap.save_context(path, context)
    assert path.stat().st_mode & 0o777 == 0o600
    assert bootstrap.load_context(path, SHA) == context
    context["password"] = "private"
    path.write_text(json.dumps(context))
    with pytest.raises(bootstrap.BootstrapError, match="CONTEXT_INVALID"):
        bootstrap.load_context(path, SHA)


def test_workflow_keeps_publish_and_java_gates_and_never_dumps_ssm_logs():
    workflow = (ROOT / ".github/workflows/deploy-worker-ec2.yml").read_text()
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert ".api-deploy.lock" in workflow
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert workflow.index("Wait for Spring deployment") < workflow.index(
        "bootstrap-chat-aws.py prepare"
    )
    assert workflow.index("bootstrap-chat-aws.py deploy") < workflow.index(
        "bootstrap-chat-aws.py finish"
    )
    assert "StandardOutputContent" not in workflow and "StandardErrorContent" not in workflow


def test_custom_worker_directory_and_user_do_not_redirect_api_configuration(monkeypatch):
    aws = FakeAws()
    obj = bootstrap.Rollout(SHA, "/opt/worker-custom", "workeruser", aws=aws)
    monkeypatch.setattr(obj, "current", lambda: None)
    obj.prepare("i-aaa")
    api_scripts = [script for instance, script in aws.scripts if instance == "i-bbb"]
    assert all(
        "/opt/worker-custom" not in script and "workeruser" not in script for script in api_scripts
    )
    assert any(
        "runuser -u ubuntu" in script and "--directory /opt/catchhole" in script
        for script in api_scripts
    )


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "169.254.169.254", "8.8.8.8", "::1"])
def test_discovery_only_accepts_private_vpc_ipv4(address):
    with pytest.raises(bootstrap.BootstrapError, match="PRIVATE_IPV4_REQUIRED"):
        bootstrap.private_ip(address)


def test_ready_snapshot_does_not_override_runtime_off_switch(rollout, monkeypatch):
    obj, aws = rollout
    context = obj.prepare("i-aaa")
    original = aws.remote

    def remote(instance, script, **kwargs):
        if "INITIAL_ACTIVATION_RUNTIME_NOT_READY" in script:
            raise bootstrap.BootstrapError("REMOTE_INITIAL_ACTIVATION_RUNTIME_NOT_READY")
        return original(instance, script, **kwargs)

    monkeypatch.setattr(aws, "remote", remote)
    with pytest.raises(bootstrap.BootstrapError, match="RUNTIME_NOT_READY"):
        obj.finish(context)
    assert not any("activate-api" in script for _, script in aws.scripts)


def test_finish_checks_live_flags_and_container_before_opening_api(rollout):
    obj, aws = rollout
    context = obj.prepare("i-aaa")
    obj.finish(context)
    scripts = [script for _, script in aws.scripts]
    verify = next(script for script in scripts if "INITIAL_ACTIVATION_RUNTIME_NOT_READY" in script)
    assert 'flags["indexing"]' in verify
    assert 'chat.get("CHAT_RETRIEVAL_ENABLED")' in verify
    assert "runtime.preflight()" in verify
    assert '"exec", "-T", "chatbot"' in verify
    assert scripts.index(verify) < len(scripts) - 1
    subprocess.run(["sh", "-n"], input=verify, text=True, check=True)


@pytest.mark.parametrize(
    "retrieval,operations,expected",
    [
        ("READY", True, True),
        ("DISABLED", True, False),
        ("READY", False, False),
    ],
)
def test_activation_probe_evaluates_live_container_readiness(
    rollout, tmp_path, monkeypatch, retrieval, operations, expected
):
    obj, aws = rollout
    readiness = {
        "state": "READY",
        "checks": {"retrieval": retrieval},
        "operationsEnabled": operations,
    }
    fake_runtime = """import json
class Runtime:
    def __init__(self, *args): pass
    def validate(self): return {"indexing": True}
    def config(self, kind): return {"chatbot": {"environment": {"CHAT_RETRIEVAL_ENABLED": "true"}}}
    def preflight(self): pass
    def compose(self, *args, **kwargs): return json.dumps(READINESS)
    def verify_ready(self, *args): pass
def enabled(value): return value == "true"
"""
    (tmp_path / "manage-ai-runtime.py").write_text(
        "READINESS = " + repr(readiness) + "\n" + fake_runtime
    )

    def remote(instance, script, **kwargs):
        source = script.split(" <<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
        result = subprocess.run(
            ["python3", "-", str(tmp_path), str(tmp_path), obj.image],
            input=source,
            text=True,
            capture_output=True,
        )
        assert "private" not in result.stdout
        return bootstrap.safe_remote_result(result.stdout)

    monkeypatch.setattr(aws, "remote", remote)
    if expected:
        obj.verify_runtime({"workerInstanceId": "i-aaa"})
    else:
        with pytest.raises(bootstrap.BootstrapError, match="REMOTE_STATE_UNEXPECTED"):
            obj.verify_runtime({"workerInstanceId": "i-aaa"})
