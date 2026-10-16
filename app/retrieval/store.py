"""Bounded, trusted work scope. Serialize source writes with Spring's Work lock."""

from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import delete, select, text

from app.retrieval.contracts import IndexBusy, Source, SourceChanged
from app.retrieval.models import RetrievalChunk, RetrievalSource

SOURCE_SQL = """
SELECT e.id, e.work_id, e.episode_no, e.content_s3_key, e.content_s3_version, e.content_hash,
 CASE WHEN w.lifecycle_status <> 'ACTIVE' OR m.status <> 'ACTIVE' THEN 'UNAVAILABLE'
      WHEN e.status = 'ARCHIVED' THEN 'ARCHIVED'
      WHEN EXISTS (SELECT 1 FROM episode_source_purge_requests p WHERE p.episode_id=e.id)
        THEN 'PURGE_PENDING'
      WHEN e.content_s3_key IS NULL OR e.content_s3_key='' OR e.content_hash IS NULL
        OR e.content_hash !~ '^[a-f0-9]{64}$' THEN 'SOURCE_UNIDENTIFIED'
      ELSE 'AVAILABLE' END AS source_state
FROM episodes e JOIN works w ON w.id=e.work_id JOIN members m ON m.id=w.member_id
"""


class RetrievalStore:
    def __init__(self, session_factory):
        self.sessions = session_factory

    def backfill_snapshot(self, work_ids=()):
        """Freeze eligible works and episode upper bounds in one database snapshot.

        Only private IDs, a timestamp and numeric bounds reach the durable cursor.
        New works/episodes and changes behind the cursor are discovered next sweep.
        """
        with self.sessions() as session:
            return [
                (row["work_id"], row["episode_to"], row["snapshot_at"].isoformat())
                for row in session.execute(
                    text("""
                    SELECT w.id AS work_id, max(e.episode_no) AS episode_to,
                           statement_timestamp() AS snapshot_at
                    FROM works w JOIN members m ON m.id=w.member_id
                    JOIN episodes e ON e.work_id=w.id
                    WHERE w.lifecycle_status='ACTIVE' AND m.status='ACTIVE'
                      AND e.status <> 'ARCHIVED'
                      AND (:all_works OR w.id=ANY(:ids))
                    GROUP BY w.id ORDER BY w.id"""),
                    {"all_works": not work_ids, "ids": list(work_ids)},
                ).mappings()
            ]

    def backfill_sources(self, work_id, episode_to, snapshot_at, after_no, after_id, limit):
        """Keyset page: equal episode numbers cannot be lost at a page boundary."""
        if not 1 <= limit <= 100:
            raise ValueError("Invalid backfill page size")
        with self.sessions() as session:
            return [
                self._source(row)
                for row in session.execute(
                    text(
                        SOURCE_SQL
                        + """ WHERE e.work_id=:work AND e.status <> 'ARCHIVED'
                        AND e.episode_no <= :hi AND e.created_at <= :snapshot
                        AND (e.episode_no > :after_no
                             OR (e.episode_no=:after_no AND e.id > :after_id))
                        ORDER BY e.episode_no,e.id LIMIT :limit"""
                    ),
                    dict(
                        work=work_id,
                        hi=episode_to,
                        snapshot=datetime.fromisoformat(snapshot_at),
                        after_no=after_no,
                        after_id=after_id,
                        limit=limit,
                    ),
                ).mappings()
            ]

    @contextmanager
    def exclusive(self, work_id):
        # Session-level advisory lock survives short transaction commits; no row
        # transaction is held during S3/provider I/O. Dedicated CLI needs two DB connections.
        key = int.from_bytes(
            sha256(b"retrieval-v1" + work_id.bytes).digest()[:8], "big", signed=True
        )
        with self.sessions() as session:
            connection = session.connection()
            locked = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            connection.commit()
            if not locked:
                raise IndexBusy()
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()

    def sources(self, scope):
        with self.sessions() as session:
            rows = session.execute(
                text(
                    SOURCE_SQL
                    + """
                WHERE e.work_id=:work AND e.episode_no BETWEEN :lo AND :hi
                  AND (:all_ids OR e.id=ANY(:ids))
                  AND (e.status <> 'ARCHIVED' OR NOT :all_ids)
                ORDER BY e.episode_no,e.id LIMIT :limit"""
                ),
                {
                    "work": scope.work_id,
                    "lo": scope.episode_from,
                    "hi": scope.episode_to,
                    "all_ids": not scope.episode_ids,
                    "ids": list(scope.episode_ids),
                    "limit": scope.max_sources + 1,
                },
            ).mappings()
            # Result streaming bounds application memory; explicit IDs are operator supplied.
            chosen, more = [], False
            for row in rows:
                if scope.episode_ids and row["id"] not in scope.episode_ids:
                    continue
                if len(chosen) == scope.max_sources:
                    more = True
                    break
                chosen.append(self._source(row))
            if (
                scope.episode_ids
                and not more
                and {s.episode_id for s in chosen} != set(scope.episode_ids)
            ):
                raise ValueError("Requested episodes are outside the selected work/range")
            return chosen, more

    def excluded_sources(self, scope):
        with self.sessions() as session:
            books = session.scalar(
                text("""
                SELECT count(*) FROM upload_files f JOIN upload_batches b ON b.id=f.batch_id
                WHERE b.work_id=:work AND f.file_role='SETTING_BOOK'"""),
                {"work": scope.work_id},
            )
            archived = session.scalar(
                text("""
                SELECT count(*) FROM episodes WHERE work_id=:work AND status='ARCHIVED'
                AND episode_no BETWEEN :lo AND :hi"""),
                {"work": scope.work_id, "lo": scope.episode_from, "hi": scope.episode_to},
            )
            return {
                "settingBooksInWork": books,
                "archivedEpisodesInRange": archived,
                "pastEpisodeVersions": None,
            }

    @staticmethod
    def _source(row):
        return Source(
            row["work_id"],
            row["id"],
            row["episode_no"],
            row["content_s3_key"],
            row["content_s3_version"],
            row["content_hash"],
            row["source_state"],
        )

    def _current(self, session, source, *, lock=False):
        if lock:
            session.execute(
                text("SELECT id FROM works WHERE id=:work FOR UPDATE"), {"work": source.work_id}
            ).first()
        row = (
            session.execute(
                text(SOURCE_SQL + " WHERE e.id=:id AND e.work_id=:work"),
                {"id": source.episode_id, "work": source.work_id},
            )
            .mappings()
            .first()
        )
        if not row:
            raise SourceChanged()
        current = self._source(row)
        if current.state != "AVAILABLE" or current.fingerprint != source.fingerprint:
            raise SourceChanged()

    @staticmethod
    def _matches(header, source, spec):
        return (
            header is not None
            and header.work_id == source.work_id
            and header.source_key == source.key
            and header.source_version == source.version
            and header.source_hash == source.content_hash
            and header.chunk_policy == spec.policy
        )

    @staticmethod
    def _chunks(session, episode_id):
        return list(
            session.scalars(
                select(RetrievalChunk)
                .where(RetrievalChunk.episode_id == episode_id)
                .order_by(RetrievalChunk.chunk_index)
            )
        )

    @staticmethod
    def chunk_state(chunk, spec):
        if sha256(chunk.chunk_text.encode("utf-8")).hexdigest() != chunk.chunk_hash:
            return "CHUNK_MISMATCH"
        if chunk.status == "FAILED":
            return "FAILED"
        if chunk.embedding is None:
            return "MISSING"
        if chunk.embedding_model != spec.model or chunk.embedding_version != spec.version:
            return "MODEL_VERSION_MISMATCH"
        return "READY" if chunk.status == "READY" else "MISSING"

    def check_source(self, source):
        with self.sessions() as session:
            self._current(session, source)

    def existing(self, source, spec):
        with self.sessions() as session:
            self._current(session, source)
            if not self._matches(session.get(RetrievalSource, source.episode_id), source, spec):
                return []
            return self._chunks(session, source.episode_id)

    def inspect(self, source, spec):
        with self.sessions() as session:
            header = session.get(RetrievalSource, source.episode_id)
            chunks = self._chunks(session, source.episode_id)
            if source.state != "AVAILABLE":
                return {
                    "state": source.state,
                    "targetChunks": None,
                    "counts": {},
                    "storedChunks": len(chunks),
                }
            if header is None:
                return {
                    "state": "UNPREPARED",
                    "targetChunks": None,
                    "counts": {},
                    "storedChunks": 0,
                }
            if not self._matches(header, source, spec):
                return {
                    "state": "SOURCE_POLICY_MISMATCH",
                    "targetChunks": None,
                    "counts": {},
                    "storedChunks": len(chunks),
                }
            try:
                self._current(session, source)
            except SourceChanged:
                return {
                    "state": "SOURCE_CHANGED",
                    "targetChunks": None,
                    "counts": {},
                    "storedChunks": len(chunks),
                }
            counts = dict(Counter(self.chunk_state(c, spec) for c in chunks))
            complete = (
                len(chunks) == header.chunk_count and counts.get("READY", 0) == header.chunk_count
            )
            return {
                "state": "READY" if complete else "PARTIAL",
                "targetChunks": header.chunk_count,
                "counts": counts,
                "storedChunks": len(chunks),
            }

    def prepare(self, source, spec, slices):
        with self.sessions() as session, session.begin():
            self._current(session, source, lock=True)
            header = session.get(RetrievalSource, source.episode_id)
            old = self._chunks(session, source.episode_id)
            intact = len(old) == len(slices) and all(
                c.chunk_index == s.index
                and c.chunk_hash == s.content_hash
                and c.chunk_text == s.text
                and c.start_offset == s.start
                and c.end_offset == s.end
                and c.token_count == s.tokens
                for c, s in zip(old, slices)
            )
            if self._matches(header, source, spec) and intact:
                return old
            session.execute(
                delete(RetrievalChunk).where(RetrievalChunk.episode_id == source.episode_id)
            )
            if header is None:
                header = RetrievalSource(episode_id=source.episode_id)
                session.add(header)
            header.work_id, header.source_key = source.work_id, source.key
            header.source_version, header.source_hash = source.version, source.content_hash
            header.chunk_policy, header.chunk_count = spec.policy, len(slices)
            header.prepared_at = datetime.now()
            session.flush()
            chunks = [
                RetrievalChunk(
                    id=uuid4(),
                    episode_id=source.episode_id,
                    chunk_index=s.index,
                    chunk_text=s.text,
                    start_offset=s.start,
                    end_offset=s.end,
                    chunk_hash=s.content_hash,
                    token_count=s.tokens,
                    status="MISSING",
                    attempt_count=0,
                )
                for s in slices
            ]
            session.add_all(chunks)
            session.flush()
            return chunks

    def _validate(self, session, source, spec, chunks):
        self._current(session, source, lock=True)
        if not self._matches(session.get(RetrievalSource, source.episode_id), source, spec):
            raise SourceChanged()
        actual = list(
            session.scalars(
                select(RetrievalChunk)
                .where(RetrievalChunk.id.in_([c.id for c in chunks]))
                .with_for_update()
            )
        )
        by_id = {c.id: c for c in actual}
        for previous in chunks:
            row = by_id.get(previous.id)
            if (
                row is None
                or row.episode_id != source.episode_id
                or row.chunk_hash != previous.chunk_hash
                or row.chunk_text != previous.chunk_text
                or row.start_offset != previous.start_offset
                or row.end_offset != previous.end_offset
                or row.token_count != previous.token_count
                or sha256(row.chunk_text.encode("utf-8")).hexdigest() != row.chunk_hash
            ):
                raise SourceChanged()
        return by_id

    def validate(self, source, spec, chunks):
        with self.sessions() as session, session.begin():
            self._validate(session, source, spec, chunks)

    def attempted(self, source, spec, chunks, operation_id):
        with self.sessions() as session, session.begin():
            for row in self._validate(session, source, spec, chunks).values():
                row.attempt_count += 1
                row.last_operation_id = operation_id

    def save(self, source, spec, chunks, vectors):
        with self.sessions() as session, session.begin():
            actual = self._validate(session, source, spec, chunks)
            for chunk, vector in zip(chunks, vectors, strict=True):
                row = actual[chunk.id]
                row.embedding, row.embedding_model, row.embedding_version = (
                    vector,
                    spec.model,
                    spec.version,
                )
                row.embedded_at, row.status, row.failure_code = datetime.now(), "READY", None

    def failed(self, source, spec, chunks, code):
        with self.sessions() as session, session.begin():
            for row in self._validate(session, source, spec, chunks).values():
                row.status, row.failure_code = "FAILED", code

    def valid_chunks(self, work_id, spec, *, episode_from=1, episode_to=2147483647, limit=100):
        """Data contract for #240, not a user endpoint or semantic ranker."""
        if not 1 <= limit <= 100 or not 1 <= episode_from <= episode_to:
            raise ValueError("Invalid retrieval read bounds")
        with self.sessions() as session:
            return [
                dict(row)
                for row in session.execute(
                    text("""
                SELECT id,episode_id,episode_no,start_offset,end_offset,chunk_text,chunk_hash,
                       source_hash,source_version,chunk_policy,embedding_model,embedding_version
                FROM retrieval_current_chunks
                WHERE work_id=:work AND chunk_policy=:policy AND embedding_model=:model
                  AND embedding_version=:version AND episode_no BETWEEN :lo AND :hi
                ORDER BY episode_no,episode_id,chunk_index LIMIT :limit"""),
                    dict(
                        work=work_id,
                        policy=spec.policy,
                        model=spec.model,
                        version=spec.version,
                        lo=episode_from,
                        hi=episode_to,
                        limit=limit,
                    ),
                ).mappings()
                if sha256(row["chunk_text"].encode("utf-8")).hexdigest() == row["chunk_hash"]
            ]
