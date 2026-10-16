import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.retrieval import bootstrap as b


ADMIN = "postgresql+psycopg://admin:synthetic-secret@localhost:5432/test"


def config(tmp_path, **extra):
    return b.Config.from_env(
        {"DATABASE_URL": ADMIN, "CHAT_BOOTSTRAP_OUTPUT_DIR": str(tmp_path), **extra}
    )


def test_configuration_requires_explicit_admin_and_matches_existing_reader_target(tmp_path):
    with pytest.raises(b.BootstrapFailure, match="DATABASE_CONFIGURATION_INVALID"):
        b.Config.from_env({})
    with pytest.raises(b.BootstrapFailure, match="READER_TARGET_MISMATCH"):
        config(tmp_path, CHAT_RETRIEVAL_DATABASE_URL=ADMIN)
    with pytest.raises(b.BootstrapFailure, match="READER_TARGET_MISMATCH"):
        config(
            tmp_path,
            CHAT_RETRIEVAL_DATABASE_URL="postgresql+psycopg://reader:secret@other.invalid/test",
        )
    cfg = config(
        tmp_path,
        CHAT_RETRIEVAL_DATABASE_URL="postgresql+psycopg://reader:p%40ss%2Fword@localhost:5432/test",
    )
    assert cfg.existing_url.password == "p@ss/word"
    assert cfg.required_connections == 32
    with pytest.raises(b.BootstrapFailure, match="BOOTSTRAP_CONFIGURATION_INVALID"):
        config(tmp_path, CHAT_BOOTSTRAP_REQUIRED_CONNECTIONS="0")
    assert config(tmp_path, CHAT_BOOTSTRAP_REQUIRED_CONNECTIONS="20").required_connections == 20


def test_private_atomic_files_and_lock_are_enforced(tmp_path):
    path = tmp_path / "private"
    with b._private_directory(path):
        assert path.stat().st_mode & 0o777 == 0o700
        with pytest.raises(b.BootstrapFailure, match="BOOTSTRAP_BUSY"):
            with b._private_directory(path):
                pass
        destination = path / "reader.json"
        b._write_private(destination, {"password": "first"})
        b._write_private(destination, {"password": "second"})
        assert b._private_file(destination) == {"password": "second"}
        assert destination.stat().st_mode & 0o777 == 0o600
        assert not list(path.glob("*.tmp"))
        destination.chmod(0o644)
        with pytest.raises(b.BootstrapFailure, match="PRIVATE_FILE_PERMISSIONS_INVALID"):
            b._write_private(destination, {"password": "rejected"})
        assert json.loads(destination.read_text())["password"] == "second"
    path.chmod(0o755)
    with pytest.raises(b.BootstrapFailure, match="PRIVATE_DIRECTORY_PERMISSIONS_INVALID"):
        with b._private_directory(path):
            pass


def test_symlinks_cannot_redirect_private_credentials(tmp_path):
    target = tmp_path / "target"
    target.write_text("unchanged")
    link = tmp_path / "reader.json"
    link.symlink_to(target)
    with pytest.raises(b.BootstrapFailure):
        b._write_private(link, {"password": "secret"})
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize(
    "maximum,reserved,required,passes",
    [(40, 3, 32, True), (39, 3, 32, False), (100, 7, 88, True), (100, 7, 89, False)],
)
def test_connection_capacity_reserves_admin_headroom(maximum, reserved, required, passes):
    connection = SimpleNamespace(
        execute=lambda _: SimpleNamespace(
            mappings=lambda: SimpleNamespace(one=lambda: {"maximum": maximum, "reserved": reserved})
        )
    )
    if passes:
        assert (
            b._capacity(connection, required)["availableApplicationConnections"]
            == maximum - reserved - 5
        )
    else:
        with pytest.raises(b.BootstrapFailure) as failure:
            b._capacity(connection, required)
        assert failure.value.code == "DATABASE_CAPACITY_INSUFFICIENT"
        assert failure.value.counts["adminHeadroom"] == 5


def test_password_intent_is_durable_before_create_and_reused_after_interruption(
    tmp_path, monkeypatch
):
    cfg = config(tmp_path)
    monkeypatch.setattr(b, "_role", lambda *args: None)
    monkeypatch.setattr(b, "_validate_role", lambda *args, **kw: None)
    monkeypatch.setattr(b, "_quote", lambda _, value: '"' + value + '"')
    attempts = []

    class Connection:
        def execute(self, query, params=None):
            if "CREATE ROLE" in str(query):
                saved = b._private_file(tmp_path / "reader-intent.json")
                attempts.append(saved)
                raise RuntimeError("simulated death before role creation")

        def scalar(self, *args):
            return False

    for _ in range(2):
        with pytest.raises(RuntimeError):
            b._managed_reader(Connection(), cfg)
    assert attempts[0] == attempts[1]
    assert len(attempts[0]["password"]) >= 32
    assert (tmp_path / "reader-intent.json").stat().st_mode & 0o777 == 0o600


def test_role_collision_does_not_create_new_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "_role", lambda *args: {"marker": None})
    connection = SimpleNamespace(execute=lambda *args: None)
    with pytest.raises(b.BootstrapFailure, match="READ_ROLE_COLLISION"):
        b._managed_reader(connection, config(tmp_path))
    assert not (tmp_path / "reader-intent.json").exists()


def test_changed_database_target_cannot_reuse_private_intent(tmp_path):
    b._write_private(
        tmp_path / "reader-intent.json",
        {
            "schemaVersion": 1,
            "role": b.ROLE,
            "target": {"host": "wrong.invalid"},
            "password": "a" * 48,
            "marker": b.MARKER + "a" * 32,
        },
    )
    with pytest.raises(b.BootstrapFailure, match="READER_INTENT_INVALID"):
        b._intent(config(tmp_path))


def test_saved_managed_dsn_keeps_exact_scope_and_supplied_credentials(tmp_path, monkeypatch):
    cfg = config(
        tmp_path,
        CHAT_RETRIEVAL_DATABASE_URL=ADMIN.replace("admin:synthetic-secret", b.ROLE + ":new-secret"),
    )
    intent = {
        "schemaVersion": 1,
        "role": b.ROLE,
        "target": b._target(cfg.admin_url),
        "password": "old-intent-password" * 3,
        "marker": b.MARKER + "a" * 32,
    }
    b._write_private(tmp_path / "reader-intent.json", intent)
    monkeypatch.setattr(b, "_role", lambda *args: {"marker": intent["marker"]})
    validate = Mock()
    monkeypatch.setattr(b, "_validate_role", validate)
    connection = object()
    reader, saved_intent = b._existing_reader(connection, cfg)
    assert reader == cfg.existing_url and reader.password == "new-secret"
    assert saved_intent == intent
    validate.assert_called_once_with(connection, b.ROLE, exact_select=True)


@pytest.mark.parametrize("missing", ["intent", "marker", "role", "target"])
def test_managed_proof_cannot_fall_back_to_custom_reader_policy(tmp_path, monkeypatch, missing):
    cfg = config(
        tmp_path,
        CHAT_RETRIEVAL_DATABASE_URL=ADMIN.replace("admin:", b.ROLE + ":"),
    )
    intent = {
        "schemaVersion": 1,
        "role": b.ROLE,
        "target": b._target(cfg.admin_url),
        "password": "a" * 48,
        "marker": b.MARKER + "a" * 32,
    }
    if missing == "target":
        intent["target"] = {"host": "other.invalid"}
    if missing != "intent":
        b._write_private(tmp_path / "reader-intent.json", intent)
    role = (
        None if missing == "role" else {"marker": None if missing == "marker" else intent["marker"]}
    )
    monkeypatch.setattr(b, "_role", lambda *args: role)
    validate = Mock()
    monkeypatch.setattr(b, "_validate_role", validate)
    code = "READER_INTENT_INVALID" if missing == "target" else "READ_ROLE_COLLISION"
    with pytest.raises(b.BootstrapFailure, match=code):
        b._existing_reader(object(), cfg)
    validate.assert_not_called()


@pytest.mark.parametrize("name", ["custom_reader", b.ROLE])
def test_custom_reader_preserves_readonly_policy_without_managed_proof(tmp_path, monkeypatch, name):
    cfg = config(tmp_path, CHAT_RETRIEVAL_DATABASE_URL=ADMIN.replace("admin:", name + ":"))
    monkeypatch.setattr(b, "_role", lambda *args: {"marker": None})
    validate = Mock()
    monkeypatch.setattr(b, "_validate_role", validate)
    connection = object()
    assert b._existing_reader(connection, cfg) == (cfg.existing_url, None)
    validate.assert_called_once_with(connection, name, exact_select=False)


@pytest.mark.parametrize(
    "flag", ["rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"]
)
def test_elevated_readers_are_rejected_before_grants(monkeypatch, flag):
    role = dict(
        oid=1,
        rolcanlogin=True,
        **{
            key: False
            for key in [
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            ]
        },
    )
    role[flag] = True
    monkeypatch.setattr(b, "_role", lambda *args: role)
    with pytest.raises(b.BootstrapFailure, match="READ_ROLE_ELEVATED"):
        b._validate_role(SimpleNamespace(), "reader")


def test_public_result_never_exposes_exception_or_configuration_secrets(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        b, "prepare", Mock(side_effect=RuntimeError("postgresql://admin:secret@private/db"))
    )
    result = b.run({"DATABASE_URL": ADMIN, "CHAT_BOOTSTRAP_OUTPUT_DIR": str(tmp_path)})
    assert result == {
        "schemaVersion": 1,
        "state": "FAILED",
        "code": "BOOTSTRAP_DATABASE_OR_STATE_FAILURE",
    }
    assert not capsys.readouterr().out
    assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "sqlstate,code",
    [
        ("42501", "DB_BOOTSTRAP_PERMISSION_DENIED"),
        ("28P01", "DB_BOOTSTRAP_AUTHENTICATION_FAILED"),
        ("42P01", "RETRIEVAL_SCHEMA_NOT_READY"),
    ],
)
def test_database_errors_expose_only_actionable_fixed_codes(tmp_path, monkeypatch, sqlstate, code):
    from sqlalchemy.exc import DBAPIError

    original = RuntimeError("private SQL and secret credentials")
    original.sqlstate = sqlstate
    monkeypatch.setattr(
        b, "prepare", Mock(side_effect=DBAPIError("secret SQL", {"password": "secret"}, original))
    )
    result = b.run({"DATABASE_URL": ADMIN, "CHAT_BOOTSTRAP_OUTPUT_DIR": str(tmp_path)})
    assert result == {"schemaVersion": 1, "state": "FAILED", "code": code}
