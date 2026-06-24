# repositories

Repository classes live here.

Spring equivalent:

```text
JpaRepository / Repository
```

Expected future repositories:

- `AnalysisJobRepository`: read analysis jobs
- `EpisodeRepository`: read episode metadata and update processing status
- `EpisodeChunkRepository`: save deterministic chunks and source offsets
- `UploadFileRepository`: read uploaded file metadata and detected episode ranges
- `SettingCandidateRepository`: save AI-extracted candidates for user review
- `RagEmbeddingTargetRepository`: save/search embedding targets

Repositories should focus on database access only. They should not call LLMs, S3, or contain analysis decisions.

Current files:

- `analysis_job_repository.py`: reads analysis jobs. Execution status changes are domain methods on `AnalysisJob`.
