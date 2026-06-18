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
- `analysis_job.py`: analysis job state and metadata mapping.
- `episode.py`: episode metadata and S3 content key mapping.
- `upload_batch.py`: upload batch target mapping.
- `upload_file.py`: uploaded file and detected episode range mapping.
- `work.py`: work metadata mapping used by analysis jobs.
