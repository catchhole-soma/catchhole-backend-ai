# GH287 Episode Pending Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. User-selected execution is Sol ultra implementation, then Astra verification; do not add agents or approval gates.

**Goal:** Restore lean episode extraction and provide only related same-domain pending advisory references to identity/comparison without changing frozen inputs.

**Architecture:** Add optional typed selection metadata to existing frozen references, then derive pure stage-specific prompt projections. Reuse current identity resolution and batch targets; leave complete analysisContext available for equality and retry validation. Explicitly preserve BOOK behavior.

**Tech Stack:** Python/Pydantic/pytest, Java/Spring/Jackson/Gradle; existing JSONB frozen state, no migration.

**Spec:** `docs/superpowers/specs/2026-10-24-gh287-pending-context-design.md`

## Global Constraints

- Initial implementation used only the two GH287 worktrees without commit/push/PR/deploy. The user subsequently approved integration and merge on 2026-10-24; direct production-data mutations and paid quality benchmarks remain excluded.
- Preserve immutable job input, hash, claim/context equality, source/lease/provenance/chronology validation, journals, retry/split/recovery and token accounting. No token cap/upload limit/model changes.
- Scope is EPISODE; preserve SETTING_BOOK behavior. Never filter persisted history or mutate analysisContext.
- Pending references are advisory, never selectable identities/current targets or automatically approved old candidates.
- Use synthetic fixtures; no private manuscripts, credentials or model outputs in repository artifacts.

## Review Focus

1. Same display name with two actual IDs: contradictory linked identity must be excluded; legacy ambiguity must not silently merge identities (Tasks 1/3).
2. Ended/unknown STATUS: keep typed semantics and avoid current-state promotion; old untyped refs remain usable for comparison (Tasks 1/2).
3. Injury/recovery and world scope/root conflicts: related cross-key/type/scope references survive selection (Task 3).
4. Retry/split/recovery: measured prompt equals submitted selected context; stored context/hash/equality remain complete (Tasks 3/4).
5. BOOK input and old frozen jobs: no accidental semantic reduction or live rehydration; strict old-worker rollout limitation documented (Tasks 1/4).

---

### Task 1: Preserve optional typed metadata through frozen reference transport

**Files (Java relative to backend worktree):**
- Modify `src/main/java/org/monitoring/catchholebackend/domain/analysis/service/AnalysisPendingReferenceSource.java`
- Modify `src/main/java/org/monitoring/catchholebackend/domain/analysis/dto/response/WorkerAnalysisReferencePayload.java`
- Modify `src/main/java/org/monitoring/catchholebackend/domain/analysis/mapper/AnalysisRunContextMapper.java`
- Modify `src/main/java/org/monitoring/catchholebackend/domain/character/service/CharacterFactComparisonBatchWorker.java` and `domain/worldsetting/service/OrderedWorldSettingWorker.java` (same Java prefix).
- Capability propagation: `domain/analysis/{controller/AnalysisJobWorkerController,service/AnalysisJobWorkerService,mapper/AnalysisJobWorkerMapper}.java`, `domain/character/{controller/CharacterFactComparisonWorkerController,service/CharacterFactComparisonWorkerService}.java`, `domain/worldsetting/{controller/WorldSettingWorkerController,service/WorldSettingWorkerService}.java`. Test corresponding controller/client wire behavior.
- AI capability header: `app/clients/spring_worker_client.py` and its existing tests.
- Tests: matching existing `AnalysisPendingReferenceSourceTest`, `AnalysisRunContextMapperTest`, `CharacterFactComparisonBatchWorkerTest` under `src/test/java/org/monitoring/catchholebackend/domain/`.
**Files (AI):** `app/schemas/analysis_context.py`, `app/schemas/worker.py`, `tests/test_automatic_analysis_context.py`.

**Interfaces:** Add optional `selection` record/model using exact fields in spec; Python `AnalysisReferenceSelection` uses frozen/extra-forbid and aliases. `WorkerAnalysisReference.selection: AnalysisReferenceSelection | None = None`. Reuse existing `advisoryCurrentContext.characterId` and `provisionalSubjectKey` for comparison target identity; no duplicate DTO field. Add explicit `includeReferenceSelection: boolean` argument through response-producing calls to `AnalysisRunContextMapper.toResponse(job, includeReferenceSelection)`, legacy overload false. Header `X-Analysis-Reference-Selection: true` enables it; missing/false omits selection entirely. Existing constructors/legacy payloads remain accepted. `WorkerAnalysisReference.prompt_data()` excludes selection metadata.

- [x] Add Java capture tests for linked character, provisional character, discovery vs STATUS, active=false JSON, plural world links/category, edited/invalid comparison guards and source provenance. Assert no metadata is fabricated for unknown links.
- [x] Add mapper replay tests with old JSON (selection absent), new JSON and changed live candidate after freeze. Assert mapping depends exclusively on frozen JSON, original input/hash unchanged and order stable across JSON field order.
- [x] Add Python parsing/prompt tests: old payload accepted, new optional metadata preserved, prompt contains no UUID/provisional key/selection object, active false remains false.
- [x] Run targeted tests red: `python -m pytest tests/test_automatic_analysis_context.py -q`; backend `./gradlew test --tests '*AnalysisPendingReferenceSourceTest' --tests '*AnalysisRunContextMapperTest' --tests '*CharacterFactComparisonBatchWorkerTest'`.
- [x] Implement capture/DTO/mapper/schema changes; use candidate's established fact schema mapping for type instead of guessing display labels. Copy JSON, retain existing source and pending guards. Read existing advisory context characterId before prompt projection strips it; retain absence fallback without changing K1 or snapshot entries. Implement consistent capability header and explicit propagation on every response with analysisContext. Freeze metadata independently of header; emit only when opted in. Do not add migration/backfill/live lookup.
- [x] Rerun targeted tests green, including constructor compile compatibility. Verify absent/false header yields old strict-compatible JSON without selection, true yields metadata consistently at claim/context/subject endpoints, new AI sends the header on all required calls, and old Java/no metadata remains accepted. Stored hash is identical across capability representations.

### Task 2: Lean episode first-stage projections with pending name/STATUS hints

**Files:** Create `app/analysis/pending_reference_projection.py`; modify `app/analysis/setting_extractor.py`, `app/analysis/world_setting_extractor.py`, and only if needed `app/analysis/ordered_context.py` / `app/worker/analysis_job_worker.py`. Tests: `tests/test_setting_extractor.py`, `tests/test_world_setting_extractor.py`, `tests/test_semantic_current_extraction_v5.py`, `tests/test_semantic_current_worker_v5.py`.

**Interfaces:** `episode_character_hints(references: tuple[WorkerAnalysisReference, ...], schema_hints: tuple[CharacterSettingSchemaHint, ...]) -> dict` returns only pending character names and typed STATUS hints with explicit unresolved/active semantics; no evidence/reason/IDs. Keep extractor signatures and rich ordered/semantic context objects intact for downstream stages. Existing known names/status serializers remain the base representation.

- [x] Add captured-request tests asserting episode character includes chunk/metadata/schema, names and active statuses once, pending names/typed STATUS hints, and verified_time_context; excludes pending non-STATUS settings, evidence/reasons, ordered identity evidence and full semantic_current_state.
- [x] Add ended/unknown status and two same-name identities fixtures. Assert active=false is neither dropped nor active/current; name hints contain no discovery suppression instruction or identity claim. Old metadata-free references yield names and schema-resolvable STATUS values (active unknown), never guessed arbitrary profile-as-STATUS. Held/failed new candidates retain STATUS from original schema mapping even without comparison result.
- [x] Add episode world captured-request tests: only metadata/chunk/static instructions despite populated analysis/semantic context; assert BOOK still receives its previous layout/current/pending context and source rules.
- [x] Run these tests red with `python -m pytest tests/test_setting_extractor.py tests/test_world_setting_extractor.py tests/test_semantic_current_extraction_v5.py tests/test_semantic_current_worker_v5.py -q`.
- [x] Implement explicit source branch (`setting_book is not None` at extractor boundary). Episode renders lean projection, BOOK retains prior behavior. Remove episode discovery-suppression instruction; retain temporal extraction rules, validated time context, bounded request client, retries and schema. Bump only changed episode prompt cache suffixes.
- [x] Rerun tests green. Update old assertions requiring rich episode first-stage state to assert the newly approved contract; preserve BOOK assertions.

### Task 3: Related pending reference selection through identity and comparison

**Files:** Extend `app/analysis/pending_reference_projection.py`; modify `app/analysis/ordered_character_subjects.py`, `app/analysis/world_subject_batch.py`, `app/analysis/character_fact_comparison_pipeline.py`, `app/analysis/character_fact_comparator.py`, `app/analysis/world_setting_comparator.py`, `app/analysis/world_setting_manuscript_pipeline.py`. Audit other callers with `rg -n 'ordered_reference_data|unresolved_references' app/analysis` and route episode model-facing builders through the same projection.
**Tests:** Create `tests/test_pending_reference_projection.py`; extend `tests/test_ordered_character_subject_contract.py`, `tests/test_character_fact_comparison_pipeline.py`, `tests/test_character_fact_comparator.py`, `tests/test_world_subject_batch.py`, `tests/test_world_setting_comparator.py`, `tests/test_world_setting_manuscript_pipeline.py`.

**Interfaces:** Add frozen `PendingSubjectScope(domain: str, actual_subject_ids: tuple[UUID, ...] = (), provisional_subject_keys: tuple[str, ...] = (), names: tuple[str, ...] = (), world_category: str | None = None)` and `select_pending_references(references: tuple[WorkerAnalysisReference, ...], scopes: tuple[PendingSubjectScope, ...]) -> tuple[WorkerAnalysisReference, ...]`. Input scope builders consume existing candidate entity/raw names, target aliases and actual/provisional identities. Output retains input order and each reference once; does not mutate context or objects.

- [x] Add selector tests: domain isolation; same names/different known IDs exclusion; alias/raw-mention inclusion; provisional match; ambiguous/unlinked/legacy conservative name inclusion; nameless unlinked omission; unknown world category fallback; known category mismatch exclusion. Match by existing character normalization and overlap semantics; do not introduce a new fuzzy matcher.
- [x] Add related-subject tests proving all fact types/keys survive (injury vs recovery) and all world scopes/properties survive (root/scoped conflict); unrelated subjects are excluded even when fact keys coincide. Assert selections never create targets/current entries.
- [x] Add real prompt-builder tests for identity resolution and comparisons. Candidate-scope identity selection uses only plausible targets, not every target in the work. Character comparison consumes existing advisoryCurrentContext.characterId plus provisional key/name/raw mentions; world comparison consumes existing target identity_key and category.
- [x] Run targeted selector and six existing test modules red.
- [x] Implement pure selection and call-site projection. Known comparable ID mismatch takes precedence over name fallback; absent comparable identity uses conservative existing name/alias matching. Preserve uncertainty for multiple name matches. Restrict episode current request only; BOOK bypasses selective reduction.
- [x] Route selected refs into serialization before token accounting; keep complete analysisContext for equality. Ensure world manuscript grouping, character partial recovery, split and retry use the same selected prompt path. No new model calls, exact-key filtering or arbitrary caps.
- [x] Rerun targeted tests green and inspect captured prompts for no metadata leak, cross-domain context or pending-target promotion.

### Task 4: Regression evidence, full checks and logic documentation

**Files:** Relevant existing `tests/test_ordered_analysis_runtime.py`, `tests/test_ordered_character_partial_recovery.py`, BOOK test modules; `AGENTS.md` in AI/backend where existing context guidance needs clarification; AI `docs/ai-logic-versions/details/v0020.md`, `docs/ai-logic-versions/v0020.md`, `docs/ai-logic-versions/README.md`.

**Interfaces:** No new runtime interface. Existing count/preflight/provider builders must produce identical selected content; existing hash/context/source validators continue to operate on full frozen input.

- [x] Add retry/recovery captured-request regressions: unselected refs remain in claim/context and equality; related refs remain in retry and split calls; unrelated refs never reappear; changing full frozen context is still rejected. Assert no live reference fetch during replay.
- [x] Add synthetic token-budget test using existing counter, comparing full versus selected projection for a large unrelated-reference fixture. Assert fewer tokens and count exactly the submitted prompt/schema; do not change token/output limits or claim a measured model-quality improvement.
- [x] Run BOOK/source/reference-time/hidden-worker/identity regressions and old frozen-job tests. Ensure reference sourceKind=SETTING_BOOK inside an EPISODE request does not incorrectly bypass selection.
- [x] Run full AI `python -m pytest -q`; backend `./gradlew test bootJar`. Use known local runtimes/caches and isolated test DBs only; record exact commands/pass/fail/skip counts and blockers. Do not substitute targeted success for full-suite success.
- [x] Read AI version templates; recheck v0020 is available (v0019 latest at planning baseline), write detailed record before 20–35-line human summary and index. Record base SHAs plus uncommitted status, no fabricated final SHA/PR. Quality score is unmeasured; owner is task implementer and re-evaluation requires separately authorized real-model run. GH284 remains open.
- [x] Clarify AGENTS context rules: immutable full transport versus selected episode prompt projection, BOOK preservation and first-stage contract. Retain existing safeguards.
- [x] Review `git diff --check`, both worktree diffs/status and primary checkout unchanged evidence. Hand implementation to requested Astra verifier; no commit/push/PR/deploy.

## Planner self-review

Requirements map to Tasks 1 (frozen metadata/legacy replay), 2 (first-stage), 3 (related selection), 4 (regression/evidence/docs). Five review-focus cases each have explicit tests. No migration is necessary. Remaining risks are conservative ambiguous-name over-inclusion, capability consistency across all context-returning endpoints and unmeasured real-model quality; none justifies changing stored history, guessing identity or claiming GH284 complete.

## Implementation handoff — Sol, 2026-10-24 KST

Tasks 1–3 implemented and targeted regressions passed; Task 4 implementation/docs completed with final independent verification delegated to Astra. Final AI suite: 4,242 passed/139 skipped/1 existing warning in19.34s. Java full test+bootJar before final alias-response gating: 3,531 passed/236 skipped/0 failed, 3m51s; final gate targeted tests:82 passed. Final fresh Java full test+bootJar is intentionally pending Astra handoff. Exact commands/logs: workspace `output/gh287-verification/implementation-report.md`.

The implementation ledger is preserved locally as workspace `output/gh287-verification/implementation-ledger.md` and is excluded from the published repository. Added rulings preserve independent subject resolution after content failure, carry frozen aliases through existing advisory metadata under the same capability gate, keep explicit comparison scope through forced split/recovery, and preserve static temporal rules/historical goldens. The implementation verification stage did not commit/push/create a PR/deploy or mutate production/run a real-model evaluation. Later integration is authorized separately as recorded above.

## Final independent verification — Astra, 2026-10-24 02:02 KST

Task 4 complete. Final unchanged code passed AI 4,242 tests/139 skipped/1 existing warning (28.53s), Java 3,531 tests/236 skipped/0 failures or errors, and bootJar (4m10s, all 7 tasks executed with `--rerun-tasks`). All five review findings are resolved; source manifests and both diff-checks passed. Full evidence: workspace `output/gh287-verification/final-verification-report.md`. Root subsequently updated only documentation to record this outcome. Real-model quality remains unmeasured; GH284 stays open.
