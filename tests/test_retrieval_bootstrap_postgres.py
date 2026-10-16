"""Bootstrap privileges in an explicitly disposable localhost database only."""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.retrieval import bootstrap as b

pytestmark = pytest.mark.integration
PASSWORD = "synthetic-bootstrap-admin-password"
READER_PASSWORD = "synthetic-existing-reader-password"


@pytest.fixture(scope="module")
def bootstrap_database():
    raw = os.getenv("GH258_BOOTSTRAP_TEST_DATABASE_URL")
    checkout = os.getenv("GH258_BOOTSTRAP_JAVA_CHECKOUT")
    if not raw or not checkout:
        pytest.skip("Explicit disposable bootstrap DB and Java checkout required")
    url = make_url(raw)
    if url.host not in {"localhost", "127.0.0.1"} or url.database != "gh258_bootstrap_test":
        pytest.fail("Only disposable localhost gh258_bootstrap_test is allowed")
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True)
    with engine.begin() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")) == 0
        )
        connection.exec_driver_sql("CREATE EXTENSION vector")
        for migration in sorted(
            (Path(checkout) / "src/main/resources/db/migration").glob("V*__*.sql"),
            key=lambda path: int(path.name.split("__")[0][1:]),
        ):
            connection.exec_driver_sql(
                migration.read_text(), execution_options={"no_parameters": True}
            )
        # Exercise a CREATEROLE admin rather than a superuser: this matches managed
        # PostgreSQL/RDS setup and catches PG16 creator-membership behavior.
        connection.exec_driver_sql(
            f"CREATE ROLE bootstrap_admin LOGIN CREATEROLE PASSWORD '{PASSWORD}'"
        )
        connection.exec_driver_sql(
            "GRANT CONNECT ON DATABASE gh258_bootstrap_test TO bootstrap_admin WITH GRANT OPTION"
        )
        connection.exec_driver_sql(
            "GRANT USAGE ON SCHEMA public TO bootstrap_admin WITH GRANT OPTION"
        )
        connection.exec_driver_sql(
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO bootstrap_admin WITH GRANT OPTION"
        )
    yield engine, url.set(username="bootstrap_admin", password=PASSWORD)
    engine.dispose()


@pytest.fixture
def prepared_environment(bootstrap_database, tmp_path):
    engine, admin = bootstrap_database
    env = {
        "DATABASE_URL": admin.render_as_string(hide_password=False),
        "CHAT_BOOTSTRAP_OUTPUT_DIR": str(tmp_path),
    }
    yield engine, admin, env, tmp_path
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP FUNCTION IF EXISTS public.bootstrap_unsafe()")
        connection.exec_driver_sql("DROP SEQUENCE IF EXISTS public.bootstrap_read_sequence")
        connection.exec_driver_sql("REVOKE SELECT(storage_url) ON upload_files FROM PUBLIC")
        # Only roles created by this module in the disposable test container.
        for role in [b.ROLE, "existing_reader", "unexpected_membership"]:
            if connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}
            ):
                # PG16 tracks the original grantor. Revoke bootstrap_admin's grants
                # as that grantor before the superuser removes its own test grants.
                connection.exec_driver_sql("SET LOCAL ROLE bootstrap_admin")
                connection.exec_driver_sql(
                    f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"'
                )
                connection.exec_driver_sql(f'REVOKE ALL ON SCHEMA public FROM "{role}"')
                connection.exec_driver_sql(
                    f'REVOKE ALL ON DATABASE gh258_bootstrap_test FROM "{role}"'
                )
                connection.exec_driver_sql("RESET ROLE")
                connection.exec_driver_sql(f'DROP OWNED BY "{role}" CASCADE')
                connection.exec_driver_sql(f'DROP ROLE "{role}"')
        connection.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def test_non_superuser_bootstrap_is_readonly_and_crash_restart_never_rotates_password(
    prepared_environment,
):
    engine, admin, env, directory = prepared_environment
    result = b.run(env)
    assert result["state"] == "READY", result
    assert result["selectRelationCount"] == 7
    output = b._private_file(directory / "reader.json")
    intent = b._private_file(directory / "reader-intent.json")
    assert output["managed"] and output["password"] == intent["password"]
    with engine.connect() as connection:
        password_hash = connection.scalar(
            text("SELECT rolpassword FROM pg_authid WHERE rolname=:role"), {"role": b.ROLE}
        )
        assert connection.execute(
            text("""
            SELECT m.rolname,a.admin_option,a.inherit_option,a.set_option
            FROM pg_auth_members a JOIN pg_roles r ON r.oid=a.roleid
            JOIN pg_roles m ON m.oid=a.member
            WHERE r.rolname=:role OR m.rolname=:role
        """),
            {"role": b.ROLE},
        ).all() == [("bootstrap_admin", True, False, False)]
        grants = connection.execute(
            text(
                "SELECT table_name,privilege_type FROM information_schema.role_table_grants WHERE grantee=:role"
            ),
            {"role": b.ROLE},
        ).all()
        assert set(grants) == {(name, "SELECT") for name in b.RELATIONS}
    reader = create_engine(
        make_url(output["databaseUrl"]), poolclass=NullPool, hide_parameters=True
    )
    try:
        with reader.connect() as connection:
            assert connection.scalar(text("SHOW transaction_read_only")) == "on"
            connection.rollback()
            # ACLs still forbid writes if a caller disables the defensive default.
            connection.exec_driver_sql("SET transaction_read_only=off")
            with pytest.raises(Exception) as failure:
                connection.exec_driver_sql("DELETE FROM works WHERE false")
            assert getattr(failure.value.orig, "sqlstate", None) == "42501"
    finally:
        reader.dispose()
    (directory / "reader.json").unlink()  # crash after role commit, before output publication
    assert b.run(env)["state"] == "READY"
    assert b._private_file(directory / "reader.json") == output
    assert b._private_file(directory / "reader-intent.json") == intent
    # The host persists this DSN after first prepare, then supplies it on every rerun.
    env["CHAT_RETRIEVAL_DATABASE_URL"] = output["databaseUrl"]
    assert b.run(env)["state"] == "READY"
    assert b._private_file(directory / "reader.json") == output
    assert b._private_file(directory / "reader-intent.json") == intent
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT rolpassword FROM pg_authid WHERE rolname=:role"), {"role": b.ROLE}
            )
            == password_hash
        )


def test_unmanaged_role_name_collision_does_not_change_role_or_write_intent(prepared_environment):
    engine, _, env, directory = prepared_environment
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE ROLE catchhole_chat_reader LOGIN")
    result = b.run(env)
    assert result["code"] == "READ_ROLE_COLLISION"
    assert not (directory / "reader-intent.json").exists()
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=:role"),
                {"role": b.ROLE},
            )
            is None
        )


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("ALTER ROLE catchhole_chat_reader CREATEROLE", "READ_ROLE_ELEVATED"),
        (
            "GRANT UPDATE(display_name) ON members TO catchhole_chat_reader",
            "READ_ROLE_WRITE_PRIVILEGE",
        ),
        ("GRANT CREATE ON SCHEMA public TO PUBLIC", "READ_ROLE_WRITE_PRIVILEGE"),
        (
            "GRANT SELECT ON upload_files TO catchhole_chat_reader",
            "READ_ROLE_EXCESS_READ_PRIVILEGE",
        ),
        (
            "GRANT SELECT(storage_url) ON upload_files TO catchhole_chat_reader",
            "READ_ROLE_EXCESS_READ_PRIVILEGE",
        ),
        (
            "GRANT SELECT(storage_url) ON upload_files TO PUBLIC",
            "READ_ROLE_EXCESS_READ_PRIVILEGE",
        ),
        (
            "CREATE SEQUENCE bootstrap_read_sequence; GRANT SELECT ON bootstrap_read_sequence TO catchhole_chat_reader",
            "READ_ROLE_EXCESS_READ_PRIVILEGE",
        ),
        ("COMMENT ON ROLE catchhole_chat_reader IS NULL", "READ_ROLE_COLLISION"),
        (
            "CREATE ROLE unexpected_membership; GRANT unexpected_membership TO catchhole_chat_reader",
            "READ_ROLE_MEMBERSHIP",
        ),
        (
            "GRANT catchhole_chat_reader TO bootstrap_admin WITH SET TRUE",
            "READ_ROLE_MEMBERSHIP",
        ),
        (
            "CREATE FUNCTION public.bootstrap_unsafe() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
            "READ_ROLE_SECURITY_DEFINER_ACCESS",
        ),
    ],
)
@pytest.mark.parametrize("saved_dsn", [False, True], ids=["managed-prepare", "host-rerun"])
def test_existing_managed_reader_privilege_drift_fails_without_repair(
    prepared_environment, mutation, code, saved_dsn
):
    engine, _, env, directory = prepared_environment
    assert b.run(env)["state"] == "READY"
    before = (directory / "reader.json").read_bytes()
    if saved_dsn:
        env["CHAT_RETRIEVAL_DATABASE_URL"] = b._private_file(directory / "reader.json")[
            "databaseUrl"
        ]
    with engine.begin() as connection:
        connection.exec_driver_sql(mutation)
    result = b.run(env)
    assert result["code"] == code, result
    assert (directory / "reader.json").read_bytes() == before


def test_preexisting_public_column_select_is_rejected_during_first_prepare(prepared_environment):
    engine, _, env, directory = prepared_environment
    with engine.begin() as connection:
        connection.exec_driver_sql("GRANT SELECT(storage_url) ON upload_files TO PUBLIC")
    result = b.run(env)
    assert result["code"] == "READ_ROLE_EXCESS_READ_PRIVILEGE", result
    assert not (directory / "reader.json").exists()
    with engine.connect() as connection:
        assert not connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": b.ROLE}
        )


def test_existing_dsn_is_reused_without_password_or_role_changes(prepared_environment):
    engine, admin, env, directory = prepared_environment
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE ROLE existing_reader LOGIN PASSWORD '{READER_PASSWORD}'"
        )
        connection.exec_driver_sql(
            "GRANT CONNECT ON DATABASE gh258_bootstrap_test TO existing_reader"
        )
        connection.exec_driver_sql("GRANT USAGE ON SCHEMA public TO existing_reader")
        connection.exec_driver_sql(
            "GRANT SELECT ON " + ",".join(b.RELATIONS) + " TO existing_reader"
        )
        # User-provided read-only accounts retain their broader SELECT policy.
        connection.exec_driver_sql("GRANT SELECT ON upload_files TO existing_reader")
        connection.exec_driver_sql("GRANT SELECT(id) ON upload_batches TO existing_reader")
        before = connection.scalar(
            text("SELECT rolpassword FROM pg_authid WHERE rolname='existing_reader'")
        )
    reader = admin.set(username="existing_reader", password=READER_PASSWORD)
    env["CHAT_RETRIEVAL_DATABASE_URL"] = reader.render_as_string(hide_password=False)
    assert b.run(env)["state"] == "READY"
    output = b._private_file(directory / "reader.json")
    assert output["databaseUrl"] == env["CHAT_RETRIEVAL_DATABASE_URL"] and not output["managed"]
    assert not (directory / "reader-intent.json").exists()
    with engine.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT rolpassword FROM pg_authid WHERE rolname='existing_reader'")
            )
            == before
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM pg_roles WHERE rolname=:role"), {"role": b.ROLE}
            )
            == 0
        )
        connection.exec_driver_sql("GRANT DELETE ON episodes TO existing_reader")
    assert b.run(env)["code"] == "READ_ROLE_WRITE_PRIVILEGE"


def test_insufficient_capacity_fails_before_creating_credentials_or_role(prepared_environment):
    engine, _, env, directory = prepared_environment
    result = b.run({**env, "CHAT_BOOTSTRAP_REQUIRED_CONNECTIONS": "10000"})
    assert result["code"] == "DATABASE_CAPACITY_INSUFFICIENT"
    assert result["requiredConnections"] == 10000 and result["adminHeadroom"] == 5
    assert not (directory / "reader-intent.json").exists()
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM pg_roles WHERE rolname=:role"), {"role": b.ROLE}
            )
            == 0
        )
