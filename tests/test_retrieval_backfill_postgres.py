"""Campaign enumeration against all Java migrations in disposable localhost schemas."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.retrieval.contracts import SourceChanged
from app.retrieval.store import RetrievalStore
from tests import test_retrieval_postgres as postgres_tests
from tests.test_retrieval_search_postgres import Corpus

pytestmark = pytest.mark.integration
database = postgres_tests.database


@pytest.fixture
def corpora(database):
    created = []

    def create():
        corpus = Corpus(database)
        created.append(corpus)
        return corpus

    yield create, RetrievalStore(sessionmaker(database, expire_on_commit=False))
    for corpus in reversed(created):
        corpus.mutate("DELETE FROM episode_source_purge_requests WHERE work_id=:work")
        corpus.mutate("DELETE FROM episodes WHERE work_id=:work")
        corpus.mutate("DELETE FROM works WHERE id=:work")
        corpus.mutate("DELETE FROM members WHERE id=:member")


def test_snapshot_selects_multiple_active_works_and_explicit_scope(corpora):
    create, store = corpora
    first, second, purging_work, purging_owner, archived, empty, deleted = [
        create() for _ in range(7)
    ]
    for corpus in [first, second, purging_work, purging_owner, archived, deleted]:
        corpus.episode(1, prepared=False)
    second.episode(5, prepared=False)
    purging_work.mutate("UPDATE works SET lifecycle_status='PURGING' WHERE id=:work")
    purging_owner.mutate("UPDATE members SET status='PURGING' WHERE id=:member")
    archived.mutate("UPDATE episodes SET status='ARCHIVED' WHERE work_id=:work")
    deleted.mutate("DELETE FROM episodes WHERE work_id=:work")
    deleted.mutate("DELETE FROM works WHERE id=:work")

    snapshot = store.backfill_snapshot()
    assert {work: upper for work, upper, _ in snapshot} == {first.work: 1, second.work: 5}
    assert len({at for _, _, at in snapshot}) == 1  # one statement snapshot for every work
    explicit = store.backfill_snapshot(
        (second.work, purging_work.work, purging_owner.work, uuid4())
    )
    assert [(work, upper) for work, upper, _ in explicit] == [(second.work, 5)]
    assert store.backfill_snapshot((empty.work, archived.work, deleted.work)) == []


def test_keyset_pages_keep_snapshot_bounds_after_insert_archive_and_delete(corpora):
    create, store = corpora
    corpus, unrelated = create(), create()
    original = {number: corpus.episode(number, prepared=False) for number in [1, 3, 7, 9]}
    unrelated.episode(1, prepared=False)
    corpus.mutate("UPDATE episodes SET status='ARCHIVED' WHERE id=:episode", episode=original[9])
    work, upper, at = store.backfill_snapshot((corpus.work,))[0]
    assert upper == 7
    first_page = store.backfill_sources(work, upper, at, 0, UUID(int=0), 1)
    assert [source.episode_id for source in first_page] == [original[1]]

    late_low = corpus.episode(2, prepared=False)
    late_middle = corpus.episode(6, prepared=False)
    late_high = corpus.episode(8, prepared=False)
    # Test the numeric upper bound independently of the creation timestamp filter.
    corpus.mutate(
        "UPDATE episodes SET created_at=CAST(:snapshot AS timestamptz)-interval '1 second' WHERE id=:episode",
        snapshot=at,
        episode=late_high,
    )
    corpus.mutate("DELETE FROM episodes WHERE id=:episode", episode=original[3])
    first = first_page[0]
    next_page = store.backfill_sources(work, upper, at, first.episode_no, first.episode_id, 1)
    assert [source.episode_id for source in next_page] == [original[7]]
    last = next_page[0]
    assert store.backfill_sources(work, upper, at, last.episode_no, last.episode_id, 100) == []
    assert late_low not in {source.episode_id for source in first_page + next_page}
    assert late_middle not in {source.episode_id for source in first_page + next_page}

    work, upper, at = store.backfill_snapshot((corpus.work,))[0]
    assert upper == 8
    cursor, seen, sizes = (0, UUID(int=0)), [], []
    while True:
        page = store.backfill_sources(work, upper, at, *cursor, 2)
        if not page:
            break
        sizes.append(len(page))
        seen.extend(source.episode_id for source in page)
        cursor = page[-1].episode_no, page[-1].episode_id
    assert sizes == [2, 2, 1]
    assert seen == [original[1], late_low, late_middle, original[7], late_high]
    # A current source change is still rejected by the existing store fence.
    corpus.mutate(
        "UPDATE episodes SET content_hash=repeat('b',64) WHERE id=:episode", episode=last.episode_id
    )
    with pytest.raises(SourceChanged):
        store.check_source(last)


def test_current_lifecycle_and_deletion_are_rechecked_inside_frozen_scope(corpora):
    create, store = corpora
    corpus = create()
    episode = corpus.episode(1, prepared=False)
    work, upper, at = store.backfill_snapshot((corpus.work,))[0]
    corpus.mutate("UPDATE works SET lifecycle_status='PURGING' WHERE id=:work")
    page = store.backfill_sources(work, upper, at, 0, UUID(int=0), 100)
    assert len(page) == 1 and page[0].state == "UNAVAILABLE"
    with pytest.raises(SourceChanged):
        store.check_source(page[0])
    corpus.mutate("DELETE FROM episodes WHERE id=:episode", episode=episode)
    corpus.mutate("DELETE FROM works WHERE id=:work")
    assert store.backfill_sources(work, upper, at, 0, UUID(int=0), 100) == []
    assert store.backfill_snapshot((work,)) == []
