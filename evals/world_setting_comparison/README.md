# World-setting comparison replay

This evaluator replays the latest stored `world_setting_candidates` snapshots for
episodes 1 through 4. It compares the legacy candidate-at-a-time call pattern with the
current canonical-subject batch call pattern using one model.

It does not download manuscript text, call S3, reserve production quota, invoke Spring,
or write to PostgreSQL. The loader uses one repeatable-read, read-only transaction so all
tables come from the same database snapshot. It rolls the current canonical properties
back through confirmed candidate history to reconstruct each episode's pre-comparison
context. Legacy or author-edited UPDATE/MERGE rows can lack a stored before-value. In that
case the evaluator removes the future property instead of leaking it into the past context
and reports `contextReconstructionExact=false` plus the number of conservative snapshot
fallbacks. This count measures fallback applications across episode snapshots, so one
stored mutation can be counted more than once. Both arms always receive the same
reconstructed snapshot.

```bash
python -m evals.world_setting_comparison.replay_cli \
  --work-id 00000000-0000-0000-0000-000000000000 \
  --model gpt-5.6-luna \
  --confirm-external-provider-data-transfer \
  --output artifacts/world-setting-comparison-ab.json
```

The confirmation flag is mandatory because the comparison sends stored private candidate
values and evidence quotes to the configured external model provider. Obtain explicit data
owner approval before running it. PostgreSQL remains read-only and the saved report remains
aggregate-only.

The sanitized preflight and run record lives in
[`docs/world-setting-comparison-ab-episodes-1-4.md`](../../docs/world-setting-comparison-ab-episodes-1-4.md).

`--work-id` may be omitted only when exactly one work has a stored candidate snapshot
for every episode from 1 through 4. Standard output and the output file contain the same
JSON aggregate. The report contains hashes and counts only; it excludes work, episode,
candidate, and target IDs as well as raw subjects, paths, values, and evidence.
