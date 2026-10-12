"""Durable delivery of completed reports, not provider calls.

Single HTTP process per volume. Payloads expire and are erased after delivery or
revocation; secure_delete prevents deleted text lingering in free database pages.
"""

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import UUID
from app.schemas.chat_execution import Dispatch
from app.chatbot.client import ChatExecutionClient, ChatTransportError


class Outbox:
    def __init__(self, path: str, max_rows: int = 1000):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(destination)
        os.chmod(destination, 0o600)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("""CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY, dispatch TEXT NOT NULL, runner TEXT NOT NULL,
            action TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL)""")
        self.max_rows = max_rows
        self._reserved_slots = 0

    def count(self) -> int:
        return self.db.execute("SELECT count(*) FROM reports").fetchone()[0]

    def ensure_capacity(self, required: int = 1):
        if self.count() + self._reserved_slots + required > self.max_rows:
            raise RuntimeError("Chat report backlog is full")

    def reserve_slot(self):
        self.ensure_capacity()
        self._reserved_slots += 1

    def release_slot(self):
        self._reserved_slots -= 1
        if self._reserved_slots < 0:
            raise RuntimeError("Unbalanced report reservation")

    def put(
        self,
        key: str,
        dispatch: Dispatch,
        runner: UUID,
        action: str,
        payload: dict,
        expires: float,
        *,
        reserved: bool = False,
    ):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        old = self.db.execute("SELECT payload FROM reports WHERE id=?", (key,)).fetchone()
        if old:
            if old[0] != encoded:
                raise RuntimeError("Conflicting chat report")
            return
        if not reserved:
            self.ensure_capacity()
        elif self._reserved_slots < 1:
            raise RuntimeError("Missing report reservation")
        with self.db:
            self.db.execute(
                "INSERT INTO reports VALUES (?,?,?,?,?,?)",
                (
                    key,
                    dispatch.model_dump_json(by_alias=True),
                    str(runner),
                    action,
                    encoded,
                    expires,
                ),
            )

    async def flush(self, client: ChatExecutionClient):
        # Different report types recover independently. A broken usage endpoint must
        # not prevent persisting a finished answer (Spring marks settlement pending).
        rows = self.db.execute("SELECT * FROM reports ORDER BY rowid LIMIT 16").fetchall()
        slots = asyncio.Semaphore(4)

        async def deliver(row):
            key, raw, runner, action, payload, expires = row
            discard = expires <= time.time()
            if not discard:
                async with slots:
                    try:
                        await client.request(
                            Dispatch.model_validate_json(raw),
                            UUID(runner),
                            action,
                            json.loads(payload),
                        )
                        discard = True
                    except ChatTransportError as exc:
                        discard = exc.terminal
            if discard:
                with self.db:
                    self.db.execute("DELETE FROM reports WHERE id=?", (key,))

        await asyncio.gather(*(deliver(row) for row in rows))

    def close(self):
        self.db.close()
