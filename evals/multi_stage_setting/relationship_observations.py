"""Private experiment sidecar; deliberately excluded from fact scoring and state application."""

import hashlib
import json
import os
from pathlib import Path

from app.analysis.relationship_observations import RELATIONSHIP_OBSERVATION_VERSION


class RecordingRelationshipExtractor:
    relationship_observation_version = RELATIONSHIP_OBSERVATION_VERSION

    def __init__(self, extractor, output: Path) -> None:
        self.extractor = extractor
        self.output = output
        self.chunks: list[dict] = []
        output.parent.mkdir(parents=True, exist_ok=True)
        # Refuse to overwrite another run's manuscript-bearing observations.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        self._write()

    async def extract_from_chunk(self, **kwargs):
        result = await self.extractor.extract_from_chunk(**kwargs)
        self.chunks.append({
            "episodeNo": kwargs.get("episode_no"),
            "sourceChunkId": str(kwargs["source_chunk_id"]),
            "sourceSha256": hashlib.sha256(kwargs["chunk_text"].encode("utf-8")).hexdigest(),
            "observations": [item.model_dump(mode="json") for item in result.relationship_observations],
            "rejections": [item.model_dump(mode="json") for item in result.relationship_observation_rejections],
        })
        self._write()
        return result

    def _write(self) -> None:
        self.output.write_text(json.dumps({
            "version": RELATIONSHIP_OBSERVATION_VERSION,
            "status": "UNCONFIRMED_OBSERVATIONS",
            "chunks": self.chunks,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
