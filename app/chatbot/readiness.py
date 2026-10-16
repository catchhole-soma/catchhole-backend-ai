"""Deployment preflight without paid calls or user-corpus reads.

This checks service configuration and dependencies, not model access, answer quality,
or completion of a particular work's retrieval index. Neither Spring feature gate
has to be enabled while preparing a rollout.
"""

import asyncio
import json
import os
from uuid import UUID

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.chatbot.hybrid_config import HybridSettings
from app.chatbot.qa_config import ChatQaSettings
from app.retrieval.contracts import IndexSpec
from app.retrieval.search_repository import CURRENT_FROM, FIELDS


class ReadinessFailure(Exception):
    """Only fixed, locally selected codes cross the command boundary."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _configuration():
    try:
        qa, hybrid = ChatQaSettings(), HybridSettings()
        if not qa.enabled:
            raise ReadinessFailure("QA_DISABLED")
        if not qa.model.strip() or not os.getenv("LLM_API_KEY", "").strip():
            raise ReadinessFailure("MODEL_CONFIGURATION_INVALID")
        key = os.getenv("SPRING_INTERNAL_API_KEY", "")
        if len(key) < 16:
            raise ReadinessFailure("INTERNAL_KEY_INVALID")
        base = httpx.URL(os.environ.get("SPRING_INTERNAL_API_BASE_URL", ""))
        if (
            base.scheme not in {"http", "https"}
            or not base.host
            or base.username
            or base.password
            or base.query
            or base.fragment
        ):
            raise ReadinessFailure("SPRING_URL_INVALID")
        if hybrid.enabled:
            url = make_url(hybrid.database_url)
            if url.drivername != "postgresql+psycopg":
                raise ReadinessFailure("RETRIEVAL_DATABASE_URL_INVALID")
            IndexSpec(version=hybrid.embedding_version)
        return hybrid, str(base).rstrip("/"), key
    except ReadinessFailure:
        raise
    except Exception:
        # Pydantic/SQLAlchemy/httpx errors may contain the rejected URL or secret.
        raise ReadinessFailure("CONFIGURATION_INVALID") from None


async def _spring_readiness(http, base, key):
    try:
        health = await http.get(f"{base}/actuator/health", follow_redirects=False, timeout=5)
        if health.status_code != 200 or health.json().get("status") != "UP":
            raise ReadinessFailure("SPRING_UNHEALTHY")
    except httpx.RequestError:
        raise ReadinessFailure("SPRING_UNREACHABLE") from None
    except (ValueError, AttributeError, TypeError):
        raise ReadinessFailure("SPRING_UNHEALTHY") from None
    try:
        response = await http.get(
            f"{base}/api/internal/v1/chat-runs/operations/readiness",
            headers={"X-Internal-Api-Key": key},
            follow_redirects=False,
            timeout=5,
        )
        if response.status_code in {401, 403}:
            raise ReadinessFailure("SPRING_AUTH_FAILED")
        if response.status_code != 200:
            raise ReadinessFailure("SPRING_READINESS_FAILED")
        envelope = response.json()
        data = envelope["data"]
        if envelope["success"] is not True or not isinstance(data, dict):
            raise ValueError()
        if any(
            type(data[name]) is not bool
            for name in ("operationsEnabled", "publicChatEnabled", "capabilityConfigured")
        ):
            raise ValueError()
        if not data["capabilityConfigured"]:
            raise ReadinessFailure("CAPABILITY_NOT_CONFIGURED")
        return {name: data[name] for name in ("operationsEnabled", "publicChatEnabled")}
    except httpx.RequestError:
        raise ReadinessFailure("SPRING_UNREACHABLE") from None
    except (ValueError, KeyError, TypeError):
        raise ReadinessFailure("SPRING_READINESS_INVALID") from None


def _retrieval_readiness(hybrid):
    """Parse the production query with all real grants; never select corpus rows."""
    engine = None
    try:
        spec = IndexSpec(version=hybrid.embedding_version)
        vector = "[1," + ",".join(["0"] * (spec.dimensions - 1)) + "]"
        engine = create_engine(
            make_url(hybrid.database_url),
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "options": "-c default_transaction_read_only=on -c statement_timeout=5000",
            },
        )
        with engine.connect() as connection:
            if connection.scalar(text("SHOW transaction_read_only")) != "on":
                raise ReadinessFailure("RETRIEVAL_TRANSACTION_NOT_READ_ONLY")
            dimensions = connection.scalar(
                text(
                    "SELECT a.atttypmod FROM pg_attribute a "
                    "JOIN pg_type t ON t.oid=a.atttypid "
                    "WHERE a.attrelid='retrieval_chunks'::regclass "
                    "AND a.attname='embedding' AND NOT a.attisdropped AND t.typname='vector'"
                )
            )
            if dimensions != spec.dimensions:
                raise ReadinessFailure("RETRIEVAL_VECTOR_SCHEMA_INVALID")
            if (
                connection.scalar(
                    text("SELECT vector_dims(CAST(:vector AS vector(1536)))"), {"vector": vector}
                )
                != spec.dimensions
            ):
                raise ReadinessFailure("RETRIEVAL_VECTOR_SCHEMA_INVALID")
            # LIMIT 0 still checks grants on the base tables, purge table and view,
            # column types and operators, without loading any personal source text.
            connection.execute(
                text(
                    f"SELECT {FIELDS},c.status,s.chunk_count,c.embedding_model,"
                    "c.embedding_version,c.embedding <=> CAST(:vector AS vector(1536)) AS score "
                    f"{CURRENT_FROM} AND c.id IN (SELECT id FROM retrieval_current_chunks) "
                    "LIMIT 0"
                ),
                dict(
                    work=UUID(int=0),
                    member=0,
                    lo=1,
                    hi=1,
                    policy=spec.policy,
                    model=spec.model,
                    version=spec.version,
                    vector=vector,
                ),
            )
    except ReadinessFailure:
        raise
    except Exception:
        raise ReadinessFailure("RETRIEVAL_DATABASE_NOT_READY") from None
    finally:
        if engine is not None:
            engine.dispose()


async def check_readiness(*, http=None):
    """Return a secret-free result; dependency failures never escape into a traceback."""
    try:
        hybrid, base, key = _configuration()
        if http is None:
            async with httpx.AsyncClient() as client:
                flags = await _spring_readiness(client, base, key)
        else:
            flags = await _spring_readiness(http, base, key)
        if hybrid.enabled:
            await asyncio.to_thread(_retrieval_readiness, hybrid)
        return {
            "schemaVersion": 1,
            "state": "READY",
            "checks": {
                "qa": "READY",
                "spring": "READY",
                "retrieval": "READY" if hybrid.enabled else "DISABLED",
            },
            **flags,
        }
    except ReadinessFailure as exc:
        return {"schemaVersion": 1, "state": "NOT_READY", "code": exc.code}
    except Exception:
        return {"schemaVersion": 1, "state": "NOT_READY", "code": "PREFLIGHT_FAILED"}


def entrypoint():
    try:
        result = asyncio.run(check_readiness())
    except KeyboardInterrupt:
        result = {"schemaVersion": 1, "state": "NOT_READY", "code": "INTERRUPTED"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(entrypoint())
