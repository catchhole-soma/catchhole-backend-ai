# analysis

AI analysis use cases live here.

Expected future files:

- `setting_extractor.py`: extract setting candidates from episode chunks
- `evidence_locator.py`: map LLM evidence quotes back to chunk offsets
- `consistency_checker.py`: detect conflicts against existing confirmed settings
- `analysis_result_builder.py`: build summary JSON for `analysis_jobs`

This package should contain analysis decisions. It can call `llm`, `embeddings`, `chunking`, and repositories through services or worker orchestration.
