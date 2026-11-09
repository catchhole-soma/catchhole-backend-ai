# Setting book character context implementation plan

> Execute inline with test-driven-development and a fresh whole-change review.

**Goal:** Reduce the seven reconstructed setting-book requests below the unchanged 64,000 guard while retaining selected current-state semantics.
**Architecture:** Pure BOOK-only provider projection over the fully validated frozen ordered+semantic inputs. Full identity catalogue; explicit named numeric-row detail selection with whole-context fallback; lossless duplicate consolidation.
**Tech Stack:** Python 3.12, Pydantic, pytest, tiktoken o200k_base.
**Spec:** ../specs/2026-11-09-setting-book-context-design.md

## Global Constraints

No source/state writes or provider requests in production. No real manuscripts in repository fixtures.
Do not change model, response schema, budget, source/lease/fingerprint or downstream identity/comparison.
All selected slots and provenance survive. Unknown scope keeps all details. EPISODE remains unchanged.

## Review Focus

- Shared names/aliases and reordered/short names must not collapse identities.
- Numeric era prefixes and longer words containing a name must not become person matches.
- Global/unnamed documents must keep full current context even when a name also appears.
- Different STATUS display values must never be deduplicated as equal; protection/time survive.
- Catalogue-only entries are identification hints, never empty CURRENT or proof of absence.

### Task 1: Implement and cover provider projection

Files: create app/analysis/book_character_context.py; update app/analysis/setting_extractor.py;
create tests/test_book_character_context.py.

- [x] Write provider-boundary tests for catalogue/detail separation, global fallback, alias/short/same-name matches, immutable inputs, exact-only STATUS consolidation, retry and guard.
- [x] Run new tests against the baseline; expected missing projection assertions fail.
- [x] Implement pure selection and merge; wire only BOOK ordered+semantic branch and update cache suffix/instructions.
- [x] Run focused extraction/book/semantic/pending regression; expected all pass.

### Task 2: Measure and finish

Files: synthetic benchmark script/fixture, docs/ai-logic-versions/{details/,}v0053.md,
docs/ai-logic-versions/README.md, AGENTS.md.

- [x] Replay seven frozen inputs against baseline and revised provider builders with no external LLM calls.
- [x] Record raw/guard counts, total/selected owners, reduction, margin, fingerprints and code hashes.
- [x] Run deterministic synthetic regression and full pytest; retain baseline and final counts.
- [x] Review whole change, address behavioral findings with failing tests before fixes.
- [x] Update issue #147 and logic-version records with measured outcomes and quality limitations.

## Progress

Baseline origin/main 33c74039085ac5f0776bd5c380341cd7f17d931c: 5,720 passed, 186 skipped, 1 existing warning.
Native worktree tool rejected nested repository sourceRoot; Git worktree created at .worktrees/gh-147-ai. User naming preference applied: fix/gh-147-setting-book-context (no codex prefix).

Independent review found mixed named/unnamed scope and compound-particle/one-character omissions.
Ruling: select only when every nonempty row has an explicit registered name/alias/short-name header;
otherwise keep all details. Cost: less input reduction for prose and mixed documents, without guessing scope.
Six reproductions failed before this change and passed afterward. System prompt field mapping was also corrected.
Actual model quality and historical reconstruction authenticity were outside the read-only review;
reported separately as unmeasured / fingerprint-backed reconstruction, respectively.

Final verification: 5,748 passed, 186 skipped, one unchanged Starlette deprecation warning. Real reconstructed inputs: 7/7 guard passes, 54,063–55,347 guarded tokens. Code f127f06; benchmark 0efb7ac. No deployment or real model run.

## User-authorized live extension (2026-11-09)

- [x] Freeze 7 reconstructed inputs and create counterfactual model controls without production state/ledger writes.
- [x] Keep the new guard enabled; disable only the old application guard in the isolated evaluation subclass.
- [x] Freeze source-derived gold before viewing outputs; blind variant labels for independent review.
- [x] Restrict selection to numeric cells after indirect prose/freeform-attribute regressions.
- [x] Preserve repeated CURRENT facts and explicit roster qualifiers after real-model omissions.
- [x] Re-run 12 synthetic cases 3 times and all 7 real inputs on final prompts.
- [x] Run full suite: 5760 passed, 186 skipped, 1 existing warning.
- [x] Publish detailed final evidence and open Draft PR [#148](https://github.com/catchhole-soma/catchhole-backend-ai/pull/148) (no deployment;2specific-institution qualifiers still missing).

The earlier Progress paragraph records the initial offline stage. Final live results supersede its
unmeasured-quality and initial-token-count status; see the v0053 summary/details.

## User-authorized integration

The user requested merging PR #148 and tracking the remaining two institution qualifiers in follow-up [#149](https://github.com/catchhole-soma/catchhole-backend-ai/issues/149). The quality limitation remains explicit; the current change does not claim lossless extraction for all source forms.
