"""Operational backfill also fills setting books (2026-11-03): fake stores, S3, provider and ledger only.

Per work the sweep indexes current episodes first and then the current revision of each non-archived setting
book, under the same lock, campaign budget, ledger path and result rules. The production campaign's SQLite state
written by the episode-only code must continue in place (same ID, identity and admitted budget).
"""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx

from app.chatbot.client import ChatExecutionClient
from app.chatbot.outbox import Outbox
from app.embeddings.client import OpenAIEmbeddingsClient
from app.retrieval.backfill import ZERO_UUID, CampaignRunner, CampaignState, exclusive_campaign
from app.retrieval.contracts import IndexSpec
from app.retrieval.provider import OperationalEmbeddingProvider
from app.retrieval.service import MAX_SOURCE_BYTES
from tests.test_retrieval_backfill import BOOK, SHORT, Corpus, config, invoke
from tests.test_retrieval_service import Provider

PER_CALL = len(SHORT.encode()) + 256  # conservative admission of one single-chunk batch


class Recording(Provider):
    """Synthetic provider that also records which source kind each paid call embedded."""

    def __init__(self, action=None):
        super().__init__(action)
        self.kinds = []

    async def embed(self, source, chunks, before_call):
        self.kinds.append("BOOK" if hasattr(source, "revision_id") else "EPISODE")
        return await super().embed(source, chunks, before_call)


def ready(memory):
    return bool(memory.chunks) and all(chunk.status == "READY" for chunk in memory.chunks)


def test_sweep_indexes_each_works_episodes_then_its_current_setting_books(tmp_path):
    corpus, work, other = Corpus(), UUID(int=1), UUID(int=2)
    episodes = [corpus.add(work, number) for number in (1, 2)]
    books = [corpus.books.add(work), corpus.books.add(work)]
    episodes.append(corpus.add(other, 1))
    provider = Recording()
    report = invoke(config(tmp_path), corpus, provider)
    assert report["state"] == "READY" and report["providerAttempts"] == 5
    assert provider.kinds == ["EPISODE", "EPISODE", "BOOK", "BOOK", "EPISODE"]
    assert report["checkedSources"] == report["readySources"] == 5
    assert report["checkedSettingBooks"] == report["readySettingBooks"] == 2
    assert report["excludedOrUnavailableSettingBooks"] == 0
    assert all(ready(memory) for memory in episodes + books)
    # Both kinds share the campaign's durable admission budget and the per-work lock.
    assert report["admittedInputTokenUpperBound"] == 3 * PER_CALL + 2 * (len(BOOK.encode()) + 256)
    assert corpus.locked.count(work) >= 1 and set(corpus.locked) == {work, other}
    assert sorted(corpus.books.reads) == sorted(book.source.revision_id for book in books)
    again = invoke(config(tmp_path), corpus)
    assert again["state"] == "READY" and again["providerAttempts"] == 0
    assert again["checkedSettingBooks"] == again["readySettingBooks"] == 2
    assert all(str(book.source.setting_book_id) not in json.dumps(report) for book in books)


def test_work_with_only_setting_books_enters_the_snapshot(tmp_path):
    corpus, work = Corpus(), uuid4()
    book = corpus.books.add(work)
    assert corpus.backfill_snapshot(())[0][:2] == (work, 0)
    report = invoke(config(tmp_path), corpus)
    assert report["state"] == "READY" and report["providerAttempts"] == 1
    assert report["checkedSources"] == report["checkedSettingBooks"] == 1 and ready(book)


def test_new_revision_is_indexed_next_sweep_and_the_old_revision_is_never_written(tmp_path):
    corpus, work = Corpus(), uuid4()
    first = corpus.books.add(work, "First revision of the synthetic book.")
    cfg = config(tmp_path)
    assert invoke(cfg, corpus)["readySettingBooks"] == 1 and ready(first)
    # Spring: new current revision; SettingBookRetrievalIndexCleanup deletes every revision's index rows.
    second = corpus.books.revise(first.source.setting_book_id, "Second revision of the synthetic book.")
    assert not first.chunks
    report = invoke(cfg, corpus)
    assert report["state"] == "READY" and report["providerAttempts"] == 1
    assert ready(second) and not first.chunks
    assert corpus.books.reads[-1] == second.source.revision_id


def test_revision_switch_during_the_paid_call_saves_nothing_and_next_run_indexes_the_new_one(tmp_path):
    corpus, work = Corpus(), uuid4()
    first = corpus.books.add(work, "Before the edit.")
    revised = {}

    def edit(_):
        revised["memory"] = corpus.books.revise(first.source.setting_book_id, "After the edit.")

    cfg = config(tmp_path)
    changed = invoke(cfg, corpus, Provider(edit))
    assert changed["state"] == "PROGRESS" and changed["reason"] == "SOURCE_CHANGED"
    assert changed["checkedSettingBooks"] == 0 and not changed["uncertainAttempt"]
    assert not first.chunks and not revised["memory"].chunks  # no vector on either revision
    report = invoke(cfg, corpus)
    assert report["state"] == "READY" and report["readySettingBooks"] == 1
    assert ready(revised["memory"]) and not first.chunks
    assert report["admittedInputTokenUpperBound"] > changed["admittedInputTokenUpperBound"]


def test_archived_books_are_skipped_and_archiving_during_the_call_writes_nothing(tmp_path):
    corpus, work = Corpus(), uuid4()
    corpus.add(work, 1)
    archived, racing = corpus.books.add(work), corpus.books.add(work)
    corpus.books.archive(archived.source.setting_book_id)

    def archive(_):
        corpus.books.archive(racing.source.setting_book_id)

    calls = {"n": 0}

    def maybe_archive(provider):
        calls["n"] += 1
        if calls["n"] == 2:  # the book's call, after the episode's
            archive(provider)

    cfg = config(tmp_path)
    first = invoke(cfg, corpus, Provider(maybe_archive))
    assert first["state"] == "PROGRESS" and first["reason"] == "SOURCE_CHANGED"
    assert not racing.chunks and not archived.chunks
    report = invoke(cfg, corpus)
    assert report["state"] == "READY" and report["providerAttempts"] == 0
    assert report["checkedSettingBooks"] == 0 and report["checkedSources"] == 1
    assert archived.source.revision_id not in corpus.books.reads
    assert not racing.chunks and not archived.chunks


def test_oversized_setting_book_is_excluded_and_the_sweep_continues(tmp_path):
    # Owner decision 2026-11-03: unlike an episode, one oversized book must not stop the whole campaign.
    corpus, work, other = Corpus(), uuid4(), uuid4()
    corpus.add(work, 1)
    big = corpus.books.add(work, "😀" * (MAX_SOURCE_BYTES // 4) + "a")
    small = corpus.books.add(work, SHORT)
    corpus.add(other, 1)
    report = invoke(config(tmp_path), corpus)
    assert report["state"] == "PARTIAL" and report["reason"] is None
    assert not big.chunks and ready(small)
    assert report["checkedSettingBooks"] == 2 and report["readySettingBooks"] == 1
    assert report["excludedOrUnavailableSettingBooks"] == 1
    assert report["readySources"] == report["checkedSources"] - 1 == 3
    assert not report["uncertainAttempt"]


def test_oversized_episode_still_blocks_the_campaign(tmp_path):
    corpus, work = Corpus(), uuid4()
    corpus.add(work, 1, "😀" * (MAX_SOURCE_BYTES // 4) + "a")
    report = invoke(config(tmp_path), corpus)
    assert report["state"] == "BLOCKED" and report["reason"] == "SOURCE_UNREADABLE"


def test_unidentified_book_is_excluded_and_the_sweep_is_partial(tmp_path):
    corpus, work = Corpus(), uuid4()
    corpus.add(work, 1)
    good = corpus.books.add(work)
    bad = corpus.books.add(work)
    bad.source = replace(bad.source, state="SOURCE_UNIDENTIFIED")
    report = invoke(config(tmp_path), corpus)
    assert report["state"] == "PARTIAL" and ready(good) and not bad.chunks
    assert report["checkedSources"] == 3 and report["readySources"] == 2
    assert report["excludedOrUnavailableSources"] == 1
    assert report["checkedSettingBooks"] == 2 and report["readySettingBooks"] == 1
    assert report["excludedOrUnavailableSettingBooks"] == 1
    assert report["supportedSourceTypes"] == ["CURRENT_EPISODE", "SETTING_BOOK"]
    assert report["unsupportedSourceTypes"] == ["PAST_EPISODE_VERSION"]


def test_setting_books_share_the_campaign_budget_and_exhaustion_is_durable(tmp_path):
    corpus, work = Corpus(), uuid4()
    corpus.add(work, 1)
    corpus.books.add(work, SHORT)
    cfg = config(tmp_path, max_input_tokens=PER_CALL + 1)
    first = invoke(cfg, corpus)
    assert first["state"] == "BUDGET_EXHAUSTED" and first["providerAttempts"] == 1
    assert first["readySources"] == 1 and first["readySettingBooks"] == 0
    for resume in (False, True):
        report = invoke(cfg, corpus, resume=resume)
        assert report["state"] == "BUDGET_EXHAUSTED" and report["providerAttempts"] == 0
        assert report["admittedInputTokenUpperBound"] == PER_CALL
        assert report["remainingInputTokenAllowance"] == 1


def test_status_resets_book_counts_each_sweep(tmp_path):
    corpus, work = Corpus(), uuid4()
    corpus.books.add(work)
    cfg = config(tmp_path)
    assert invoke(cfg, corpus)["checkedSettingBooks"] == 1
    state = CampaignState(cfg, IndexSpec())
    try:
        state.start_sweep([(work, 0, datetime.now(UTC).isoformat())])
        status = state.status()
        assert status["sweepInProgress"] and status["sweep"] == 2
        assert status["checkedSettingBooks"] == status["readySettingBooks"] == 0
        assert state.next_work()["book_after"] == str(ZERO_UUID)
    finally:
        state.close()


# The episode-only schema exactly as the code before 2026-11-03 created it.
OLD_SCHEMA = """
    CREATE TABLE campaigns (
        id TEXT PRIMARY KEY, identity TEXT NOT NULL, admitted INTEGER NOT NULL DEFAULT 0,
        inflight INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'NOT_STARTED',
        reason TEXT, sweep INTEGER NOT NULL DEFAULT 0, scanning INTEGER NOT NULL DEFAULT 0,
        seen INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0,
        excluded INTEGER NOT NULL DEFAULT 0, no_progress INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE work_cursors (
        campaign TEXT NOT NULL, work TEXT NOT NULL, episode_to INTEGER NOT NULL,
        snapshot_at TEXT NOT NULL, after_no INTEGER NOT NULL DEFAULT 0,
        after_id TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (campaign, work));
"""


def test_old_episode_only_state_continues_the_same_campaign_and_budget(tmp_path):
    cfg = config(tmp_path, campaign_id="retrieval-v1-rollout", max_input_tokens=50_000_000)
    # The identity JSON must stay byte-identical to what the running campaign stored.
    identity = json.dumps(
        {
            "schemaVersion": 1,
            "spec": {"model": "text-embedding-3-small", "version": "v1", "dimensions": 1536,
                     "policy": IndexSpec().policy},
            "maxInputTokens": 50_000_000,
            "scope": "active-works",
            "workIds": [],
            "actor": "synthetic-operator",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert cfg.identity(IndexSpec()) == identity
    corpus, mid, finished = Corpus(), UUID(int=1), UUID(int=2)
    done = [corpus.add(mid, 1), corpus.add(mid, 2), corpus.add(finished, 1)]
    pending = corpus.add(mid, 3)
    mid_book = corpus.books.add(mid)
    late_book = corpus.books.add(finished)
    for memory in done:  # embedded by the old image before the deploy
        corpus.prepare(memory.source, IndexSpec(), corpus.service().splitter.split(SHORT))
        for chunk in memory.chunks:
            chunk.status, chunk.embedding = "READY", [1.0] * 1536
            chunk.embedding_model, chunk.embedding_version = "text-embedding-3-small", "v1"
    at = corpus.clock.isoformat()
    db = sqlite3.connect(cfg.state_path)
    db.executescript(OLD_SCHEMA)
    db.execute(
        "INSERT INTO campaigns (id,identity,admitted,state,sweep,scanning,seen,ready) "
        "VALUES (?,?,12345,'PROGRESS',4,1,2,2)",
        (cfg.campaign_id, identity),
    )
    db.executemany(
        "INSERT INTO work_cursors (campaign,work,episode_to,snapshot_at,after_no,after_id,done) VALUES (?,?,?,?,?,?,?)",
        [
            (cfg.campaign_id, str(mid), 3, at, 2, str(done[1].source.episode_id), 0),
            (cfg.campaign_id, str(finished), 1, at, 1, str(UUID(int=9)), 1),
        ],
    )
    db.commit()
    db.close()

    provider = Recording()
    resumed = invoke(cfg, corpus, provider)
    # Same sweep continues: only episode 3 and the unfinished work's book are paid; nothing is re-embedded.
    assert resumed["state"] == "READY" and resumed["sweep"] == 4
    assert provider.kinds == ["EPISODE", "BOOK"] and ready(pending) and ready(mid_book)
    assert resumed["admittedInputTokenUpperBound"] == 12345 + PER_CALL + len(BOOK.encode()) + 256
    assert resumed["maxInputTokens"] == 50_000_000
    assert resumed["checkedSources"] == 2 + 2 and resumed["checkedSettingBooks"] == 1
    assert not late_book.chunks  # its work had finished before the deploy: next sweep
    following = invoke(cfg, corpus, Recording())
    assert following["state"] == "READY" and following["sweep"] == 5
    assert following["providerAttempts"] == 1 and ready(late_book)
    assert following["admittedInputTokenUpperBound"] == (
        resumed["admittedInputTokenUpperBound"] + len(BOOK.encode()) + 256
    )
    assert following["checkedSettingBooks"] == following["readySettingBooks"] == 2
    assert following["checkedSources"] == following["readySources"] == 4 + 2
    # The previous image's statements still work on the migrated file (rollback stays possible).
    db = sqlite3.connect(cfg.state_path)
    try:
        db.execute(
            "INSERT INTO work_cursors (campaign,work,episode_to,snapshot_at,after_id) VALUES (?,?,?,?,?)",
            ("rollback-check", str(uuid4()), 1, at, str(ZERO_UUID)),
        )
        db.execute("INSERT INTO campaigns (id,identity) VALUES ('rollback-check','{}')")
        row = db.execute("SELECT admitted, identity FROM campaigns WHERE id=?", (cfg.campaign_id,)).fetchone()
        assert row == (following["admittedInputTokenUpperBound"], identity)
    finally:
        db.rollback()
        db.close()


def test_book_embedding_uses_the_same_service_paid_operational_ledger_and_actor(tmp_path):
    """Real ledger and embedding HTTP adapters over mock transports: the wire carries no source identity."""
    corpus, work = Corpus(), uuid4()
    corpus.books.add(work)
    records = []
    deadline = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()

    def spring(request):
        body = json.loads(request.content or b"null")
        path = request.url.path
        records.append((path, body))
        if "/operations/" in path:
            data = {"schemaVersion": 1, "attemptId": path.split("/")[-1], "capability": "a" * 64, "deadline": deadline}
        elif path.endswith("/start"):
            data = {
                "schemaVersion": 1, "questionId": None, "attemptId": path.split("/")[-2], "workId": str(work),
                "memberId": 1, "question": None, "episodeFrom": None, "episodeTo": None, "deadline": deadline,
                "tokenBudget": next(b["tokenBudget"] for p, b in records if "/operations/" in p),
                "maxCalls": 8, "maxToolCalls": 20, "operational": True,
            }
        elif path.endswith("/calls"):
            data = {"callId": body["callId"], "status": "RESERVED", "reservedTokens": body["reservedTokens"],
                    "inputTokens": None, "cachedInputTokens": None, "outputTokens": None}
        else:
            data = {}
        return httpx.Response(200, json={"success": True, "data": data})

    def embeddings(request):
        values = json.loads(request.content)["input"]
        return httpx.Response(200, json={
            "model": "text-embedding-3-small", "usage": {"prompt_tokens": 7},
            "data": [{"index": i, "embedding": [1.0] * 1536} for i, _ in enumerate(values)],
        })

    cfg = config(tmp_path)

    async def run():
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(spring)) as spring_http,
            httpx.AsyncClient(transport=httpx.MockTransport(embeddings)) as embedding_http,
        ):
            raw = OpenAIEmbeddingsClient("fake", "text-embedding-3-small", 1536, "v1",
                                         "https://provider.invalid/embeddings", embedding_http)
            outbox = Outbox(cfg.outbox_path)
            try:
                provider = OperationalEmbeddingProvider(
                    ChatExecutionClient("http://spring.invalid", "fake", spring_http), raw, outbox,
                    IndexSpec(), cfg.actor,
                )
                with exclusive_campaign(cfg.state_path):
                    state = CampaignState(cfg, IndexSpec())
                    try:
                        return await CampaignRunner(
                            cfg, state, corpus.service(), provider, book_service=corpus.books.service()
                        ).run(asyncio.get_running_loop().time() + cfg.max_seconds)
                    finally:
                        state.close()
            finally:
                outbox.close()

    report = asyncio.run(run())
    assert report["state"] == "READY" and report["readySettingBooks"] == 1
    operations = [body for path, body in records if "/operations/" in path]
    assert operations == [{
        "workId": str(work), "actor": cfg.actor, "policy": "RETRIEVAL_INDEX_SERVICE_PAID_V1",
        "tokenBudget": len(BOOK.encode()) + 256,
    }]
    calls = [body for path, body in records if path.endswith("/calls")]
    assert [call["purpose"] for call in calls] == ["OPERATIONAL_BACKFILL"]
    wire = json.dumps(records)
    book = corpus.books.current_books()[0]
    assert str(book.revision_id) not in wire and str(book.setting_book_id) not in wire
    assert report["userTokenDebit"] is False
