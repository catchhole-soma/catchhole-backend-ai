"""Which SETTING failures belong to one question, and what a run does after one (the single policy switch).

Run-level failures (budget, lease/HTTP, input/source change, data access, quota, gate stop) always stop
dispatching new judges; judges already running finish and publish. A question-level failure is a problem with
one model answer or one search for one question. Owner decision ③ (2026-11-02):

- production: that question alone is published as an INCOMPLETE unit (`failure_code`, `failure_cause`,
  `failed_call`, the question's `target_spans`) and the remaining questions are still judged. The run then ends
  with QUESTIONS_INCOMPLETE_CODE, never as a clean success; the author's retry re-judges only INCOMPLETE and
  unpublished questions (published decisions are kept);
- experiment (`experiment=True`): the same continuation, recorded as `{code, cause, call}` per question without
  publishing anything.

A failed intake index fill is not a run failure either: judging continues on the index as it is, and only a
question that needs a search ends INCOMPLETE with VECTOR_INDEX_FILL_FAILED (cause INDEX_FILL_*).

A planner segment failure is not a question failure: without that PLANNER stage there is no merged plan, so the
SETTING branch still stops there (experiment mode records it and merges the other segments).
"""

QUESTION_LEVEL_CODES = frozenset({
    "MODEL_CONTRACT_INVALID", "MODEL_OUTPUT_TRUNCATED", "MODEL_CALL_FAILED", "REQUIRED_CONTEXT_TOO_LARGE",
    "INPUT_COUNT_FAILED", "INVALID_DECISION", "INVALID_FOLLOW_UP", "INVALID_INSPECTION_CONTRACT",
    "VECTOR_NOT_READY", "VECTOR_INDEX_PARTIAL", "VECTOR_INDEX_FILL_FAILED", "SEARCH_FAILED",
})
_QUESTION_LEVEL_PREFIXES = ("MODEL_CONTRACT_INVALID", "INVALID_DECISION_", "INVALID_FOLLOW_UP")
# The branch failure code of a run that judged every question it could but left some INCOMPLETE (Complete.failureCode).
QUESTIONS_INCOMPLETE_CODE = "SETTING_QUESTIONS_INCOMPLETE"
# The single switch: production continues after a question-level failure (decision ③). False restores fail-fast.
CONTINUE_AFTER_QUESTION_FAILURE = True


def is_question_level(code):
    return code in QUESTION_LEVEL_CODES or any(code.startswith(prefix) for prefix in _QUESTION_LEVEL_PREFIXES)


def continues_after_question_failure(experiment):
    """The policy switch: experiments always continue; production follows CONTINUE_AFTER_QUESTION_FAILURE."""
    return bool(experiment) or CONTINUE_AFTER_QUESTION_FAILURE
