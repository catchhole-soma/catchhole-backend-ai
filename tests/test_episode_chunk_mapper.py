from uuid import uuid4

from app.chunking.chunk_splitter import EpisodeChunkDraft
from app.mappers.episode_chunk_mapper import EpisodeChunkMapper


def test_to_entity_maps_draft_to_episode_chunk() -> None:
    # 청킹 알고리즘 결과에 episode_id를 붙여 DB 저장용 EpisodeChunk로 변환하는지 확인한다.
    episode_id = uuid4()
    draft = _chunk_draft()

    entity = EpisodeChunkMapper.to_entity(
        episode_id=episode_id,
        draft=draft,
        metadata_json={"source": "single_episode_upload"},
    )

    assert entity.id is not None
    assert entity.episode_id == episode_id
    assert entity.chunk_index == draft.chunk_index
    assert entity.chunk_text == draft.chunk_text
    assert entity.start_offset == draft.start_offset
    assert entity.end_offset == draft.end_offset
    assert entity.paragraph_start_index == draft.paragraph_start_index
    assert entity.paragraph_end_index == draft.paragraph_end_index
    assert entity.metadata_json == {"source": "single_episode_upload"}


def _chunk_draft() -> EpisodeChunkDraft:
    return EpisodeChunkDraft(
        chunk_index=0,
        chunk_text="첫 번째 문단입니다.\n두 번째 문단입니다.",
        start_offset=0,
        end_offset=22,
        paragraph_start_index=0,
        paragraph_end_index=1,
    )
