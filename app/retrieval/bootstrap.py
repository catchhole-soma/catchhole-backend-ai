"""Prepare a private, read-only retrieval identity without schema migrations.

Run ``python -m app.retrieval.bootstrap prepare`` with an explicit admin DATABASE_URL.
Only fixed status codes/counts reach stdout; credentials stay in the private output directory.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.chatbot.hybrid_config import HybridSettings
from app.chatbot.readiness import ReadinessFailure, _retrieval_readiness

ROLE = "catchhole_chat_reader"
MARKER = "catchhole.chat-reader.bootstrap.v1:"
RELATIONS = (
    "retrieval_chunks",
    "retrieval_sources",
    "retrieval_current_chunks",
    "episodes",
    "works",
    "members",
    "episode_source_purge_requests",
)
ADMIN_HEADROOM = 5


class BootstrapFailure(Exception):
    def __init__(self, code, counts=None):
        self.code, self.counts = code, counts or {}
        super().__init__(code)


def _url(raw):
    try:
        url = make_url(raw)
        if (
            url.drivername != "postgresql+psycopg"
            or not url.host
            or not url.database
            or not url.username
            or not url.password
        ):
            raise ValueError()
        return url
    except Exception:
        raise BootstrapFailure("DATABASE_CONFIGURATION_INVALID") from None


def _target(url):
    return {"host": url.host, "port": url.port or 5432, "database": url.database}


@dataclass(frozen=True)
class Config:
    admin_url: object
    existing_url: object | None
    output_dir: Path
    required_connections: int = 32

    @classmethod
    def from_env(cls, env):
        try:
            required = int(env.get("CHAT_BOOTSTRAP_REQUIRED_CONNECTIONS", "32"))
            if not 1 <= required <= 10000:
                raise ValueError()
            output = Path(env.get("CHAT_BOOTSTRAP_OUTPUT_DIR", "/bootstrap"))
            if not output.is_absolute():
                raise ValueError()
        except (ValueError, TypeError):
            raise BootstrapFailure("BOOTSTRAP_CONFIGURATION_INVALID") from None
        admin = _url(env.get("DATABASE_URL", ""))
        supplied = env.get("CHAT_RETRIEVAL_DATABASE_URL", "").strip()
        existing = _url(supplied) if supplied else None
        if existing is not None and (
            _target(existing) != _target(admin) or existing.username == admin.username
        ):
            raise BootstrapFailure("READER_TARGET_MISMATCH")
        return cls(admin, existing, output, required)


def _private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BootstrapFailure("PRIVATE_FILE_PERMISSIONS_INVALID")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            return json.load(stream)
    finally:
        if fd is not None:
            os.close(fd)


def _write_private(path, value):
    if path.is_symlink():
        raise BootstrapFailure("PRIVATE_FILE_PERMISSIONS_INVALID")
    if path.exists():
        _private_file(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _private_directory(path):
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise BootstrapFailure("PRIVATE_DIRECTORY_PERMISSIONS_INVALID")
    fd = os.open(path / ".reader.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BootstrapFailure("BOOTSTRAP_BUSY") from None
        yield
    finally:
        os.close(fd)


def _engine(url):
    return create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=10000 -c lock_timeout=5000 -c search_path=public",
        },
    )


def _capacity(connection, required):
    row = (
        connection.execute(
            text("""
        SELECT current_setting('max_connections')::int AS maximum,
          current_setting('superuser_reserved_connections')::int
          + COALESCE(NULLIF(current_setting('reserved_connections',true),''),'0')::int
          + COALESCE(NULLIF(current_setting('rds.rds_superuser_reserved_connections',true),''),'0')::int AS reserved
    """)
        )
        .mappings()
        .one()
    )
    counts = {
        "maxConnections": row["maximum"],
        "reservedConnections": row["reserved"],
        "requiredConnections": required,
        "adminHeadroom": ADMIN_HEADROOM,
        "availableApplicationConnections": row["maximum"] - row["reserved"] - ADMIN_HEADROOM,
    }
    if counts["availableApplicationConnections"] < required:
        raise BootstrapFailure("DATABASE_CAPACITY_INSUFFICIENT", counts)
    return counts


def _role(connection, name):
    return (
        connection.execute(
            text("""
        SELECT r.oid,r.rolname,r.rolcanlogin,r.rolsuper,r.rolcreatedb,r.rolcreaterole,
               r.rolreplication,r.rolbypassrls,
               shobj_description(r.oid,'pg_authid') AS marker
        FROM pg_roles r WHERE r.rolname=:role
    """),
            {"role": name},
        )
        .mappings()
        .first()
    )


def _validate_role(connection, name, *, exact_select=False):
    row = _role(connection, name)
    if row is None or not row["rolcanlogin"]:
        raise BootstrapFailure("READ_ROLE_NOT_AVAILABLE")
    if any(
        row[key]
        for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")
    ):
        raise BootstrapFailure("READ_ROLE_ELEVATED")
    params = {"role": name, "oid": row["oid"]}
    # PG16's superuser-granted creator ADMIN edge points from the existing admin
    # to the reader, with INHERIT/SET disabled. It grants the reader no authority.
    # Every other edge (especially reader -> another role) is rejected.
    if connection.scalar(
        text("""
        SELECT EXISTS (SELECT 1 FROM pg_auth_members a
          JOIN pg_roles m ON m.oid=a.member JOIN pg_roles g ON g.oid=a.grantor
          WHERE (a.member=:oid OR a.roleid=:oid)
            AND NOT (a.roleid=:oid AND m.rolname=current_user AND a.admin_option
              AND NOT COALESCE((to_jsonb(a)->>'inherit_option')::boolean,true)
              AND NOT COALESCE((to_jsonb(a)->>'set_option')::boolean,true)
              AND g.rolsuper))
    """),
        params,
    ):
        raise BootstrapFailure("READ_ROLE_MEMBERSHIP")
    # Ownership and CREATE privileges can bypass table grants. PUBLIC TEMP is allowed:
    # it affects only temporary data and cannot be denied to one role by a local REVOKE.
    if connection.scalar(
        text("""
        SELECT EXISTS (SELECT 1 FROM pg_database WHERE datdba=:oid)
          OR has_database_privilege(:role,current_database(),'CREATE')
          OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                     AND (n.nspowner=:oid OR has_schema_privilege(:role,n.oid,'CREATE')))
    """),
        params,
    ):
        raise BootstrapFailure("READ_ROLE_WRITE_PRIVILEGE")
    if connection.scalar(
        text("""
        SELECT EXISTS (
          SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
          AND (c.relowner=:oid OR
            (c.relkind IN ('r','p','v','m','f') AND
              (has_table_privilege(:role,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR has_any_column_privilege(:role,c.oid,'INSERT,UPDATE,REFERENCES')))
            OR (c.relkind='S' AND has_sequence_privilege(:role,c.oid,'USAGE,UPDATE'))))
    """),
        params,
    ):
        raise BootstrapFailure("READ_ROLE_WRITE_PRIVILEGE")
    if connection.scalar(
        text("""
        SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            AND p.prosecdef AND has_function_privilege(:role,p.oid,'EXECUTE'))
    """),
        params,
    ):
        raise BootstrapFailure("READ_ROLE_SECURITY_DEFINER_ACCESS")
    if exact_select:
        accessible = connection.execute(
            text("""
            SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
              AND ((c.relkind IN ('r','p','v','m','f')
                    AND has_any_column_privilege(:role,c.oid,'SELECT'))
                   OR (c.relkind='S' AND has_sequence_privilege(:role,c.oid,'SELECT')))
        """),
            params,
        ).all()
        if any(schema != "public" or name not in RELATIONS for schema, name in accessible):
            raise BootstrapFailure("READ_ROLE_EXCESS_READ_PRIVILEGE")
    return row


def _intent(config):
    path = config.output_dir / "reader-intent.json"
    if not path.exists():
        return None
    value = _private_file(path)
    if (
        value.get("schemaVersion") != 1
        or value.get("role") != ROLE
        or value.get("target") != _target(config.admin_url)
        or not isinstance(value.get("password"), str)
        or len(value["password"]) < 32
        or not isinstance(value.get("marker"), str)
        or not value["marker"].startswith(MARKER)
        or len(value["marker"]) != len(MARKER) + 32
    ):
        raise BootstrapFailure("READER_INTENT_INVALID")
    return value


def _quote(connection, identifier):
    return connection.dialect.identifier_preparer.quote_identifier(identifier)


def _managed_reader(connection, config):
    # The same fixed advisory lock coordinates separate hosts without keeping an
    # open transaction during filesystem fsync or later reader connection checks.
    connection.execute(text("SELECT pg_advisory_xact_lock(25823901)"))
    existing = _role(connection, ROLE)
    intent = _intent(config)
    if existing is not None and (intent is None or existing["marker"] != intent["marker"]):
        raise BootstrapFailure("READ_ROLE_COLLISION")
    if intent is None:
        intent = {
            "schemaVersion": 1,
            "role": ROLE,
            "target": _target(config.admin_url),
            "password": secrets.token_urlsafe(48),
            "marker": MARKER + uuid4().hex,
        }
        # Durable intent precedes every database role change, including CREATE ROLE.
        _write_private(config.output_dir / "reader-intent.json", intent)
    if existing is None:
        # Utility statements cannot bind PASSWORD parameters. A transaction-local
        # setting keeps the secret out of SQL text and is erased on commit/rollback.
        connection.execute(
            text("SELECT set_config('catchhole.bootstrap_password',:password,true)"),
            {"password": intent["password"]},
        )
        connection.execute(
            text("""DO $bootstrap$
          BEGIN EXECUTE format('CREATE ROLE catchhole_chat_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
            current_setting('catchhole.bootstrap_password')); END $bootstrap$""")
        )
        connection.execute(
            text("SELECT set_config('catchhole.bootstrap_marker',:marker,true)"),
            {"marker": intent["marker"]},
        )
        connection.execute(
            text("""DO $bootstrap$
          BEGIN EXECUTE format('COMMENT ON ROLE catchhole_chat_reader IS %L',current_setting('catchhole.bootstrap_marker')); END $bootstrap$""")
        )
        connection.exec_driver_sql(
            "ALTER ROLE catchhole_chat_reader SET default_transaction_read_only=on"
        )
    _validate_role(connection, ROLE, exact_select=True)
    role = _quote(connection, ROLE)
    database = _quote(connection, config.admin_url.database)
    connection.exec_driver_sql(f"GRANT CONNECT ON DATABASE {database} TO {role}")
    connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
    relations = ",".join("public." + _quote(connection, relation) for relation in RELATIONS)
    connection.exec_driver_sql(f"GRANT SELECT ON {relations} TO {role}")
    _validate_role(connection, ROLE, exact_select=True)
    return config.admin_url.set(username=ROLE, password=intent["password"]), intent


def _existing_reader(connection, config):
    reader, intent = config.existing_url, None
    if reader.username == ROLE:
        role = _role(connection, ROLE)
        intent = _intent(config)  # Also verifies the persisted database target.
        marked = role is not None and (role["marker"] or "").startswith(MARKER)
        if intent is not None or marked:
            # A saved managed DSN must not fall back to the broader custom-reader
            # policy when either half of its ownership proof is missing/changed.
            if intent is None or role is None or role["marker"] != intent["marker"]:
                raise BootstrapFailure("READ_ROLE_COLLISION")
    _validate_role(connection, reader.username, exact_select=intent is not None)
    return reader, intent


def prepare(config):
    with _private_directory(config.output_dir):
        engine = _engine(config.admin_url)
        try:
            with engine.begin() as connection:
                counts = _capacity(connection, config.required_connections)
                if (
                    connection.scalar(text("SELECT current_database()"))
                    != config.admin_url.database
                ):
                    raise BootstrapFailure("DATABASE_TARGET_MISMATCH")
                if config.existing_url is not None:
                    reader, intent = _existing_reader(connection, config)
                else:
                    reader, intent = _managed_reader(connection, config)
        finally:
            engine.dispose()
        # Actual authentication and complete production SQL parsing happen only after
        # the creation transaction commits. No corpus row or paid provider is read.
        try:
            _retrieval_readiness(
                HybridSettings(
                    enabled=True, database_url=reader.render_as_string(hide_password=False)
                )
            )
        except ReadinessFailure as exc:
            raise BootstrapFailure(exc.code) from None
        output = {
            "schemaVersion": 1,
            "role": reader.username,
            "databaseUrl": reader.render_as_string(hide_password=False),
            "managed": intent is not None,
        }
        if intent is not None:
            output["password"] = reader.password
        _write_private(config.output_dir / "reader.json", output)
        return {
            "schemaVersion": 1,
            "state": "READY",
            "selectRelationCount": len(RELATIONS),
            **counts,
        }


def run(env=None):
    try:
        return prepare(Config.from_env(os.environ if env is None else env))
    except BootstrapFailure as exc:
        return {"schemaVersion": 1, "state": "FAILED", "code": exc.code, **exc.counts}
    except DBAPIError as exc:
        code = {
            "42501": "DB_BOOTSTRAP_PERMISSION_DENIED",
            "28P01": "DB_BOOTSTRAP_AUTHENTICATION_FAILED",
            "42P01": "RETRIEVAL_SCHEMA_NOT_READY",
        }.get(getattr(exc.orig, "sqlstate", None), "BOOTSTRAP_DATABASE_OR_STATE_FAILURE")
        return {"schemaVersion": 1, "state": "FAILED", "code": code}
    except Exception:
        # Do not expose SQLAlchemy statements, passwords, DSNs or exception messages.
        return {
            "schemaVersion": 1,
            "state": "FAILED",
            "code": "BOOTSTRAP_DATABASE_OR_STATE_FAILURE",
        }


def entrypoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare",))
    parser.parse_args()
    result = run()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(entrypoint())
