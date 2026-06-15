# chunking

Text normalization, episode splitting, and chunk splitting live here.

Expected future files:

- `text_normalizer.py`: normalize line endings, whitespace, encoding artifacts
- `episode_splitter.py`: split one uploaded file into one or more episodes
- `chunk_splitter.py`: split episode text into deterministic chunks
- `offsets.py`: calculate source offsets for evidence highlighting

Chunking is owned by the AI server. Spring stores uploads and analysis jobs, then Python produces chunks and evidence positions.
