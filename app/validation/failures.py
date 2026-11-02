"""Which SETTING failures belong to one question, and what a run does after one (the single policy switch).

Run-level failures (budget, lease/HTTP, input/source change, data access, quota, gate stop) always stop
dispatching new judges; judges already running finish and publish. A question-level failure is a problem with
one model answer or one search. Its policy is an open product decision, so it lives here only:

- production (`experiment=False`): fail fast like grid v3, i.e. stop dispatching new judges, let in-flight
  judges finish and publish, and report the first failure as the branch failure code;
- experiment (`experiment=True`): record `{code, cause, call}` per question and keep judging the rest.
"""

QUESTION_LEVEL_CODES = frozenset({
    "MODEL_CONTRACT_INVALID", "MODEL_OUTPUT_TRUNCATED", "MODEL_CALL_FAILED", "REQUIRED_CONTEXT_TOO_LARGE",
    "INPUT_COUNT_FAILED", "INVALID_DECISION", "INVALID_FOLLOW_UP", "INVALID_INSPECTION_CONTRACT",
    "VECTOR_NOT_READY", "VECTOR_INDEX_PARTIAL", "SEARCH_FAILED",
})
_QUESTION_LEVEL_PREFIXES = ("MODEL_CONTRACT_INVALID", "INVALID_DECISION_", "INVALID_FOLLOW_UP")


def is_question_level(code):
    return code in QUESTION_LEVEL_CODES or any(code.startswith(prefix) for prefix in _QUESTION_LEVEL_PREFIXES)


def continues_after_question_failure(experiment):
    """The policy switch. Production keeps fail-fast until the owner revisits it after the experiment."""
    return bool(experiment)
