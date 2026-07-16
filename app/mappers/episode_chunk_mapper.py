from uuid import UUID, uuid4

from app.chunking.chunk_splitter import EpisodeChunkDraft
from app.models.episode_chunk import EpisodeChunk


class EpisodeChunkMapper:
    @staticmethod
    def to_entity(
        episode_id: UUID,
        draft: EpisodeChunkDraft,
        metadata_json: dict | None = None,
    ) -> EpisodeChunk:
        return EpisodeChunk(
            id=uuid4(),
            episode_id=episode_id,
            chunk_index=draft.chunk_index,
            chunk_text=draft.chunk_text,
            start_offset=draft.start_offset,
            end_offset=draft.end_offset,
            paragraph_start_index=draft.paragraph_start_index,
            paragraph_end_index=draft.paragraph_end_index,
            metadata_json=metadata_json,
        )
