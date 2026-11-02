"""Disposable loopback PostgreSQL for the experiment: Backend Flyway SQL, a synthetic member, the work and episodes.

Only `validation_experiment_*` databases on a loopback host are accepted. Migrations are read with `git show`
from the Backend repository (read-only) at an explicit ref, in numeric order, like the AI PostgreSQL tests.
"""

import re
import subprocess

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts.validation_experiment.common import HarnessError, local_db_url

MIGRATION_DIR = "src/main/resources/db/migration"


def create_database(url):
    """CREATE DATABASE (and pgvector) when missing, through the same server's `postgres` database."""
    parsed = make_url(local_db_url(url))
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": parsed.database})
            if not exists:
                connection.exec_driver_sql(f'CREATE DATABASE "{parsed.database}"')
    finally:
        admin.dispose()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    return engine


def migrations(repo, ref):
    """`[(name, sql)]` from `git show <ref>:<migration>`; the Backend checkout is never modified."""
    listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", f"{ref}:{MIGRATION_DIR}"],
                            check=True, capture_output=True, text=True).stdout.split()
    names = sorted((name for name in listed if re.fullmatch(r"V\d+__.+\.sql", name)),
                   key=lambda name: int(name.split("__")[0][1:]))
    if not any(name.startswith("V76__") for name in names):
        raise HarnessError("Backend migrations must include V76 retrieval tables")
    return [(name, subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{MIGRATION_DIR}/{name}"], check=True,
                                  capture_output=True, text=True).stdout) for name in names]


def apply_migrations(engine, repo, ref):
    with engine.begin() as connection:
        existing = connection.scalar(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='episodes'"))
    if existing:
        raise HarnessError("Database already has the Backend schema; drop it or use a fresh validation_experiment_* name")
    applied = []
    with engine.begin() as connection:
        for name, sql in migrations(repo, ref):
            connection.exec_driver_sql(sql, execution_options={"no_parameters": True})
            applied.append(name)
    return applied


def seed(engine, fixture, *, member_id=1):
    """Synthetic member, the exported work id and the prior episodes with their exported identities."""
    work = fixture["work"]["workId"]
    with engine.begin() as connection:
        connection.execute(text("""INSERT INTO members
            (id,email,password_hash,phone_number,phone_verified,display_name,status,role,created_at,updated_at)
            VALUES (:id,'validation-experiment@test.invalid','synthetic','00000000000',false,'experiment','ACTIVE','USER',
                    localtimestamp,localtimestamp)"""), {"id": member_id})
        connection.execute(text("""INSERT INTO works (id,member_id,title,genre,latest_episode_no,created_at,updated_at)
            VALUES (:work,:member,'validation-experiment','FANTASY',:latest,localtimestamp,localtimestamp)"""),
            {"work": work, "member": member_id, "latest": fixture["target"]["episodeNo"] - 1})
        for row in fixture["episodes"]:
            connection.execute(text("""INSERT INTO episodes
                (id,work_id,episode_no,content_s3_key,content_s3_version,content_hash,char_count,status,created_at,updated_at,
                 content_updated_at)
                VALUES (:id,:work,:no,:key,:version,:hash,0,'ANALYZED',localtimestamp,localtimestamp,localtimestamp)"""),
                {"id": row["episodeId"], "work": work, "no": row["episodeNo"], "key": row["contentS3Key"],
                 "version": row.get("contentS3Version"), "hash": row["contentHash"]})
    return {"memberId": member_id, "workId": work, "episodes": len(fixture["episodes"])}
