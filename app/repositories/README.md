# repositories

Repository classes live here.

Spring equivalent:

```text
JpaRepository / Repository
```

Expected future repositories:

- `AnalysisJobRepository`: read and update job status, progress, current step
- `EpisodeRepository`: read episode metadata and update processing status
- `EpisodeChunkRepository`: save deterministic chunks and source offsets
- `UploadFileRepository`: read uploaded file metadata and detected episode ranges
- `SettingCandidateRepository`: save AI-extracted candidates for user review
- `RagEmbeddingTargetRepository`: save/search embedding targets

Repositories should focus on database access only. They should not call LLMs, S3, or contain analysis decisions.
