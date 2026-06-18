# models

SQLAlchemy ORM models live here if the AI server reads/writes PostgreSQL directly.

Expected future models:

- `AnalysisJob`
- `Episode`
- `EpisodeChunk`
- `UploadBatch`
- `UploadFile`
- `SettingCandidate`
- `RagEmbeddingTarget`

These models should mirror the Spring/PostgreSQL schema, not redefine business ownership rules.

Current files:

- `base.py`: shared SQLAlchemy declarative base for future ORM models.
