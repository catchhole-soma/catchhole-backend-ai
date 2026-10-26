# GH287 episode pending context design

Issue: https://github.com/catchhole-soma/catchhole-backend-java/issues/287

Approved scope: episode analysis input projection. User has approved autonomous execution using Astra planning, Sol ultra implementation and Astra verification. The initial implementation phase excluded commits, push, PR and deployment. After implementation and independent verification, the user authorized integration and merge on 2026-10-24. Direct production-data mutations and paid quality benchmarks remain outside this work.

## Baselines and boundaries

- AI worktree `.worktrees/gh287-pending-context/ai`, base `8121bbbdef8f20b9f5c84a7e7b75c0039b0be484`.
- Java worktree `.worktrees/gh287-pending-context/backend`, base `0bf469df9c6db274ca12ce6a88f7d10115515899`.
- Initial branches were `codex/gh287-pending-context`; Backend publication uses `feat/gh-287-pending-context` under its repository convention. Primary checkouts remain unchanged.
- Preserve immutable job input, hash, claim/context equality, source/lease/provenance/chronology validation, journals, retry/split/recovery and token accounting. No token cap/upload limit/model changes.
- Scope is EPISODE. Preserve SETTING_BOOK extraction, identity proofs, layout, current-function context, comparison and reference-time behavior. A reference originating in a book can still be relevant to an episode; gate by current request source, not reference origin.
- CONFIRMED_ONLY retains its basic contract. Episode first-stage semantic-current injection is removed even where supplied; current-state machinery remains available to identity/comparison stages.

## Episode first extraction

Character input is manuscript chunk, episode metadata, schema/rules, one existing-name list, one active STATUS key/value context, and separate unconfirmed hints. Ordered accepted/provisional current subjects may contribute their existing name/status context, with provenance distinctions retained where needed, but pending references do not become current subjects.

Unconfirmed hints contain candidate names and typed STATUS key/value/active state only. No evidence, reason, full earlier settings, identity evidence, actual IDs, provisional keys or full semantic_current_state. Deduplicate redundant display context without merging identities or conflating distinct statuses. Names are spelling hints, never proof of identity and never instructions to suppress discovery. Remove the episode instruction that existing provisional names need not be discovered again.

Preserve STATUS active=true, active=false and unknown distinctly. Ended status is an unconfirmed ended hint, not active current state; missing active is unknown. Never infer STATUS solely from a textual setting label when typed metadata or schema mapping cannot establish it. Old frozen references can supply names; use frozen schema hints to classify their key as STATUS when that mapping establishes it, retaining value with unknown active state when no active bit exists. Truly unclassifiable values are omitted from first-stage status hints, retained for later related comparison. For new held/failed candidates derive fact type from original candidate/schema mapping even when comparisonTargetFactType is absent; comparison success must not be required. Existing located narrative-time context and static temporal extraction rules remain intact.

World input is manuscript chunk + source metadata + static extraction rules. No pending references, ordered current-value instructions or semantic_current_state in episode first extraction. Preserve validation, bounded client and recovery even when context data is not rendered.

## Frozen metadata, not new storage

Extend existing reference JSON with optional `selection` metadata, copied at original freeze time. No DB migration, backfill, additional live lookup or mutation of an existing frozen input. Transport metadata must never be emitted by `prompt_data()`.

Selection shape (camelCase wire; Python snake_case aliases):

- `actualSubjectIds: UUID[]`, `provisionalSubjectKeys: str[]`: existing subject links, not claimed identity truth. Singular character target is a list of at most one; worlds preserve plural resolutions.
- `rawSubjectMention: str | null`, `candidateKind: str | null`, `factType: str | null`, `canonicalFactKey: str | null`, `valueJson: object | null`, `worldCategory: str | null`.
- Optional fields default absent/empty. Only preserve genuine existing candidate linkage; failed/stale comparison selections must not become links. Respect edited candidate final subject/value and existing validity guards.
- Preserve scopeName/matchedScopeName/matchedPropertyName and reference source provenance already carried.
- Mapper reads only `automaticInputState`, in existing sorted reference-ID order. Missing metadata is supported; never rehydrate it from current candidates.

Character comparison reuses `advisoryCurrentContext.characterId` (actual field name verified in Java) and existing `provisionalSubjectKey`; do not add duplicate target-ID DTO fields. If advisory context is absent, use the provisional key and conservative candidate/name fallback. `characterRef=K1` remains prompt-local and must not be used as a persistent identity. World context already exposes identity_key()/actual and provisional targets and requires no replacement linkage system.

Old frozen references remain valid without metadata. Hashes of persisted inputs do not change. New frozen inputs naturally hash their extra metadata using existing machinery. Protect strict old consumers with request capability header `X-Analysis-Reference-Selection: true`, default false. New AI sends it consistently on main claim, ordered subject/compare claim and context reads; Java includes `selection` only when opted in and omits the field entirely otherwise. Propagate an explicit boolean through controller/service/mapper calls; do not use request thread-locals or persist per-worker capability into frozen state. Legacy mapper overload defaults false. New freeze always preserves metadata irrespective of requesting worker, so later capable retries can read the same stored input. All responses within one capable attempt receive the same full metadata; comparison equality stays strict. New AI against old Java uses metadata-free conservative behavior. Test both rollout directions; integrate Java before AI through the existing repository workflows.

## Related advisory selection

Use one pure Python projection module. Operate on original frozen reference objects, return an ordered tuple subset, never modify WorkerAnalysisContext. Selection is domain-first, subject-second; content remains advisory.

- Character identity resolution: union candidate entity/raw mention matches and plausible existing targets using existing normalization/alias matching. Include matching typed links and conservative unlinked/legacy name matches. Ambiguous matches remain advisory and do not create targets or resolve identity. Do not treat every available work-level target as relevant.
- Character comparison: use actual ID/provisional key from batch context; include all facts for that subject across types/keys. Injury/recovery and competing status keys must survive. For linked refs, a known contradictory identity excludes the ref even when names coincide. If no comparable identity is available (old server/legacy refs), use candidate names/raw mention and matched name/known aliases conservatively; include ambiguous name matches as unconfirmed, never as snapshot entries.
- World identity/comparison: same-domain related subject IDs/provisional keys or conservative name/alias matches; typed incompatible category is excluded. Unknown legacy category does not cause wholesale loss. Keep all scopes/properties of a related subject so scope, root and hierarchy conflicts remain visible. Do not require exact setting key or scope equality.
- A missing/unusable name with no known comparable identity cannot establish relevance; omit only that unmatchable ref. No invented semantic similarity, embedding/RAG, extra provider call, or fixed count truncation.
- All selected references retain unresolved/applied=false and source information. Reference IDs and linking metadata never become selectable K/D/P/Q/T targets, snapshot properties, approvals or dependencies.

Apply selection before serialization and token measurement. Shared builders must feed initial call, schema retry, split children, world manuscript groups and partial recovery. Keep full claim/context equality checks on the unprojected input. Smaller split segments may select their own relevant subset; count exactly their rendered prompt/schema with existing limits.

## Evidence and completion

Synthetic tests must capture actual provider prompts, prove inclusion and exclusion, and exercise integration through existing pipelines, not just selector output. Cover IDs vs same names, aliases/raw mention, ambiguous/new/legacy subjects, ended/unknown statuses, injury/recovery, world category/scope conflicts, frozen replay and BOOK isolation. Java tests prove capture, immutable mapping, old JSON compatibility and actual target transport.

Run full pytest and Java test/bootJar when feasible; use only isolated test DBs, never existing .env DB URLs. Report skips and environment blockers accurately. Synthetic token counts are input-budget evidence, not quality scores. Record v0020 (verified absent at planning baseline) following AI version templates, with quality unmeasured and future re-evaluation conditioned on explicit authorization. GH284 wider quality acceptance stays open.
