# Validation RAG and Presuppositions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent unsearched DB evidence gaps and modality-based false negatives while preserving normal dialogue.
**Architecture:** Add a model-only sufficiency field and a bounded Worker retrieval guard; update atomic planning and modality prompts. Keep Java persistence DTOs and existing frozen retrieval unchanged.
**Tech Stack:** Python, Pydantic, pytest, existing OpenAI Responses client and frozen retrieval.
**Spec:** docs/superpowers/specs/2026-11-08-validation-rag-presuppositions-design.md

## Global Constraints

- No production DB direct writes, new retries, budget increases, or unrelated model configuration changes.
- At most 3 judge calls; retain scope/hash/quote validation and failure semantics.
- Raw novel inputs, Gold, credentials, and individual predictions stay outside the repository.
- User authorized issue, push, PR, merge, and real validation runs; proceed through completion.

## Review Focus

- Unsupported NO_CONFLICT with a broad but valid DB quote: require the explicit sufficiency assessment.
- Context-only FOLLOW_UP: still search before accepting an unsupported final decision.
- Search unavailable/partial/failed: incomplete, never a clean negative.
- Previously searched/empty-scope questions: no repeated search loop.
- Pure future wish, uncertain belief, explicit mistake, valid retrospective facts: no new false positives.

### Task 1: Retrieval guard and model contracts

**Files:** app/validation/contracts.py, models.py, judging.py; tests/test_validation_judging.py, test_validation_model_shapes.py, tests/support/validation_synthetic.py.
**Interfaces:** model-only db_evidence_sufficient; JudgeFlow uses validated B quotes and successful VECTOR receipts. Published ValidationDecision remains unchanged.

- [ ] Write regression asserting a DB gap causes VECTOR and a second FINAL call; observe failure on current main.
- [ ] Add strict sufficiency field, v4 schema cache name, and one bounded retrieval guard.
- [ ] Verify unavailable/failed/empty scope, valid direct quote, context-only follow-up, invalid quote, and already searched cases.
- [ ] Run focused validation suite, inspect every failure, commit code with a body.

### Task 2: Atomic planning and modality

**Files:** app/validation/models.py; tests/test_validation_presuppositions_live.py.
**Interfaces:** existing QuestionDraft property_or_rule/check_intent; no new wire fields.

- [ ] Add opt-in real-model cases for independent role/wish questions, factual party-count premise, pure wish, uncertain belief, explicit mistake, compatible recall and role changes.
- [ ] Replace blanket speculative/retrospective exemptions with scope-aware factual-premise rules.
- [ ] Run paid semantic checks with explicit models/medium; preserve aggregate and private raw evidence.
- [ ] Reinspect the private 101-v2 manuscript with the actual pipeline; score 14 planted errors and normal controls separately.

### Task 3: Document, review, merge, deployment verification

**Files:** AGENTS.md, docs/setting-validation-v1.md, docs/ai-logic-versions/{README,vNNNN,details/vNNNN}.md.
**Interfaces:** same existing execution and publication contracts.

- [ ] Record code SHA, data hashes, actual conditions, remaining misses/false positives/failures and reproducible commands.
- [ ] Run full pytest and changed-file lint; obtain fresh whole-branch review and resolve material findings.
- [ ] Publish ready PR linked to #139, verify CI and merge current tested head into main.
- [ ] Verify publish/deploy outcome; preserve execution evidence and report limitations accurately.
