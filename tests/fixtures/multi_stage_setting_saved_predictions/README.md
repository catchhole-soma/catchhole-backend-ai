# Legacy scorer regression fixtures

These are synthetic, deliberately imperfect saved predictions. They contain no user
manuscript, production model response, or Notion annotation. `FINAL` is the synthetic
fixture contract, not a claim that a private evaluation dataset has been reviewed.

`expected-reports.json` was produced before the #180 evaluator edits, at AI commit
`80d5184c006a42248ab214b36d0898c071887900`, using the unchanged v3 evaluator and
`semantic_judge=None`. Do not regenerate it merely to make a failing test pass.

Two episodes include a missing character comparison, a subsequent STATUS removal,
a world value requiring semantic review, and a harmful ADD for an expected EXCLUDE.
ORACLE, FIXED, and ROLLING preserve their original state policies. The test compares
the complete report, including state hashes and every reported denominator.

This verifies scoring compatibility. It does not measure LLM accuracy, establish
production scheduler behavior, or certify the new ordered-provisional policy.

## Current main baseline (PR65 excluded)

The active regression compares `expected-reports-main-05d9c2d.json`, captured from an
independent Git archive of main `05d9c2d65498d28a1208b7f9bc8f0087307f64fc` using the
same saved predictions and `semantic_judge=None`. The three full reports match the
previous PR65-6deac04 reports: PR65 itself changes prompts, not this saved-input scorer.
All older `expected-reports*.json` remain historical evidence; no new model was called.
