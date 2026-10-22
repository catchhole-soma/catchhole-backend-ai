"""Deterministic boundaries and explicitly reviewed semantics; no substring grading."""

from collections import Counter
from hashlib import sha256
from math import ceil
from pathlib import Path

from app.chatbot.evaluation.contracts import (
    MODES,
    Dataset,
    ReviewSet,
    Run,
    accessible,
    fingerprint,
)


def rate(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def scorer_hashes():
    return {
        p.name: sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(__file__).parent.glob("*.py"))
    }


def validate_run(dataset: Dataset, run: Run, reviews: ReviewSet | None):
    if run.manifest.dataset_sha256 != fingerprint(dataset):
        raise ValueError("Run dataset hash mismatch")
    questions = {q.id: q for q in dataset.questions}
    observations = {o.case_id: o for o in run.observations}
    if not observations.keys() <= questions.keys():
        raise ValueError("Run contains unknown cases")
    if reviews is None:
        return {}
    if reviews.dataset_sha256 != fingerprint(dataset) or reviews.run_sha256 != fingerprint(run):
        raise ValueError("Semantic reviews are bound to a different dataset or run")
    result = {}
    for review in reviews.reviews:
        observation = observations.get(review.case_id)
        if observation is None or observation.status != "COMPLETED":
            raise ValueError("Review has no completed observation")
        if review.observation_sha256 != fingerprint(observation):
            raise ValueError("Stale semantic review")
        facts = {f.id for f in questions[review.case_id].required_facts}
        cited = {c.evidence_id for c in observation.citations}
        spans = []
        for claim in review.claims:
            if observation.text[claim.start : claim.end] != claim.quote:
                raise ValueError("Reviewed assertion is not the exact answer slice")
            if claim.end <= claim.start or claim.end > len(observation.text):
                raise ValueError("Invalid assertion offsets")
            if any(claim.start < end and start < claim.end for start, end in spans):
                raise ValueError("Repeated or overlapping assertion review")
            spans.append((claim.start, claim.end))
            if not set(claim.fact_ids) <= facts or not set(claim.evidence_ids) <= cited:
                raise ValueError("Review invents an expected fact or an unobserved citation")
            if claim.support == "SUPPORTED" and not claim.evidence_ids:
                raise ValueError("Supported assertion needs observed evidence")
        if observation.outcome == "ANSWER" and review.all_assertions_reviewed and not review.claims:
            raise ValueError("An answer cannot receive a complete empty semantic review")
        result[review.case_id] = review
    return result


def citation_checks(dataset, question, observation, mode):
    corpus = {e.id: e for e in dataset.evidence}
    violations, valid = [], set()
    allowed = {key for key in question.evidence_ids if accessible(corpus[key], question, mode)}
    if not set(observation.observed_evidence_ids) <= allowed:
        violations.append("OBSERVED_EVIDENCE_OUTSIDE_MODE_OR_SCOPE")
    for citation in observation.citations:
        evidence = corpus.get(citation.evidence_id)
        if citation.evidence_id not in allowed:
            violations.append("CITATION_OUTSIDE_MODE_OR_SCOPE")
        elif citation.evidence_id not in observation.observed_evidence_ids:
            violations.append("CITATION_NOT_OBSERVED")
        elif citation.version != evidence.version or citation.sha256 != evidence.sha256:
            violations.append("STALE_EVIDENCE")
        elif (
            citation.end > len(evidence.text)
            or citation.end <= citation.start
            or evidence.text[citation.start : citation.end] != citation.quote
        ):
            violations.append("INVALID_QUOTE_SLICE")
        else:
            valid.add(citation.evidence_id)
    return sorted(set(violations)), valid, allowed


def score_case(dataset, question, observation, review, mode):
    corpus = {e.id: e for e in dataset.evidence}
    available = {key for key in question.evidence_ids if accessible(corpus[key], question, mode)}
    available_facts = {
        f.id
        for f in question.required_facts
        if any(set(group) <= available for group in f.support_sets)
    }
    information = "FULL" if len(available_facts) == len(question.required_facts) else "LIMITED"
    row = {
        "case_id": question.id,
        "category": question.category,
        "information": information,
        "status": "MISSING",
        "verdict": "PENDING",
        "violations": [],
        "answerable": information == "FULL" and "ANSWER" in question.expected_outcomes[mode],
        "answered": False,
        "outcome_matches": False,
        "semantic_reviewed": False,
        "reviewer_kind": None,
        "required_facts": len(question.required_facts),
        "available_facts": len(available_facts),
        "covered_facts": 0,
        "correct_claims": 0,
        "assessed_claims": 0,
        "uncertain_claims": 0,
        "supported_claims": 0,
        "assessed_support": 0,
        "uncertain_support": 0,
    }
    if observation is None:
        return row
    row["status"] = observation.status
    if observation.status != "COMPLETED":
        row["verdict"] = "FAIL"
        return row
    violations, valid, _ = citation_checks(dataset, question, observation, mode)
    row["violations"] = violations
    row["answered"] = observation.outcome == "ANSWER"
    row["outcome_matches"] = observation.outcome in question.expected_outcomes[mode]
    failed = bool(violations) or not row["outcome_matches"]
    if review is None:
        row["verdict"] = "FAIL" if failed else "PENDING"
        return row
    row["reviewer_kind"] = review.reviewer_kind
    covered = set()
    for claim in review.claims:
        row["uncertain_claims"] += claim.correctness == "UNCERTAIN"
        row["assessed_claims"] += claim.correctness != "UNCERTAIN"
        row["correct_claims"] += claim.correctness == "CORRECT"
        row["uncertain_support"] += claim.support == "UNCERTAIN"
        row["assessed_support"] += claim.support in {"SUPPORTED", "UNSUPPORTED"}
        supported = claim.support == "SUPPORTED" and set(claim.evidence_ids) <= valid
        row["supported_claims"] += supported
        failed |= claim.correctness == "INCORRECT" or claim.support == "UNSUPPORTED"
        if claim.correctness == "CORRECT" and supported:
            for fact in question.required_facts:
                if fact.id in claim.fact_ids and any(
                    set(group) <= set(claim.evidence_ids) for group in fact.support_sets
                ):
                    covered.add(fact.id)
    row["covered_facts"] = len(covered)
    complete = (
        review.all_assertions_reviewed
        and review.disposition_appropriate is not None
        and review.forbidden_assertion_found is not None
        and not row["uncertain_claims"]
        and not row["uncertain_support"]
        and review.reviewer_kind != "LLM_JUDGE"
    )
    row["semantic_reviewed"] = complete
    failed |= review.forbidden_assertion_found is True or review.disposition_appropriate is False
    # Missing required facts remain failures even when an answer is fluent or entirely refused.
    if complete and observation.outcome == "ANSWER" and not available_facts <= covered:
        failed = True
        row["violations"].append("REQUIRED_FACTS_MISSING")
    row["verdict"] = "FAIL" if failed else "PASS" if complete else "PENDING"
    return row


def summarize(rows):
    return {
        "cases": len(rows),
        "verdicts": dict(Counter(row["verdict"] for row in rows)),
        "missing": sum(row["status"] == "MISSING" for row in rows),
        "execution_completion": rate(sum(r["status"] == "COMPLETED" for r in rows), len(rows)),
        "outcome_match": rate(sum(r["outcome_matches"] for r in rows), len(rows)),
        "answerable_response_rate": rate(
            sum(r["answered"] and r["answerable"] for r in rows),
            sum(r["answerable"] for r in rows),
        ),
        "review_coverage": rate(sum(r["semantic_reviewed"] for r in rows), len(rows)),
        "reviewed_claim_correctness": rate(
            sum(r["correct_claims"] for r in rows),
            sum(r["assessed_claims"] for r in rows),
        ),
        "reviewed_evidence_support": rate(
            sum(r["supported_claims"] for r in rows),
            sum(r["assessed_support"] for r in rows),
        ),
        "required_fact_coverage": rate(
            sum(r["covered_facts"] for r in rows),
            sum(r["required_facts"] for r in rows),
        ),
        "uncertain_claims": sum(r["uncertain_claims"] for r in rows),
        "uncertain_support": sum(r["uncertain_support"] for r in rows),
    }


def measurements(run, expected_cases):
    calls = [call for observation in run.observations for call in observation.calls]
    live = run.manifest.execution_kind == "PROVIDER_CAPTURE"
    complete = len(run.observations) == expected_cases and all(
        o.telemetry_complete and o.status != "INCOMPLETE" for o in run.observations
    )
    usage_complete = live and complete and all(c.input_tokens is not None for c in calls)
    costs_complete = (
        usage_complete
        and run.manifest.pricing_basis is not None
        and all(c.cost_usd is not None for c in calls)
    )
    latencies = (
        sorted(o.latency_ms for o in run.observations if o.latency_ms is not None) if live else []
    )
    return {
        "kind": run.manifest.execution_kind,
        "provider_calls_recorded": len(calls),
        "unknown_usage_calls": sum(c.input_tokens is None for c in calls),
        "failed_calls": sum(c.outcome == "FAILURE" for c in calls),
        "telemetry_complete": complete,
        "input_tokens": sum(c.input_tokens for c in calls) if usage_complete else None,
        "cached_input_tokens": sum(c.cached_input_tokens for c in calls)
        if usage_complete
        else None,
        "output_tokens": sum(c.output_tokens for c in calls) if usage_complete else None,
        "total_cost_usd": sum(c.cost_usd for c in calls) if costs_complete else None,
        "known_cost_subtotal_usd": (
            sum(c.cost_usd for c in calls if c.cost_usd is not None)
            if live and run.manifest.pricing_basis is not None
            else None
        ),
        "latency_samples": len(latencies),
        "p50_ms": latencies[ceil(len(latencies) * 0.5) - 1] if latencies else None,
        "p95_ms": latencies[ceil(len(latencies) * 0.95) - 1] if latencies else None,
        "latency_note": "All recorded completions/failures; nearest-rank. "
        + ("SMALL_SAMPLE (<20)." if len(latencies) < 20 else "See sample count and failures.")
        if latencies
        else "UNMEASURED; authored/scripted durations are not provider latency.",
    }


def score(dataset: Dataset, run: Run, reviews: ReviewSet | None = None):
    reviewed = validate_run(dataset, run, reviews)
    observations = {o.case_id: o for o in run.observations}
    rows = [
        score_case(dataset, q, observations.get(q.id), reviewed.get(q.id), run.manifest.mode)
        for q in dataset.questions
    ]
    return {
        "schema_version": 1,
        "dataset_sha256": fingerprint(dataset),
        "run_sha256": fingerprint(run),
        "review_sha256": fingerprint(reviews) if reviews else None,
        "scorer_sha256": scorer_hashes(),
        "manifest": run.manifest.model_dump(mode="json"),
        "gold_status": dataset.gold_status,
        "release_ready": False,
        "interpretation": "Saved-observation audit; no provider or deployment verification. "
        "Semantic judgements are reviewer attestations, not automated entailment proofs.",
        "summary": summarize(rows),
        "measurements": measurements(run, len(dataset.questions)),
        "by_category": {
            key: summarize([r for r in rows if r["category"] == key])
            for key in sorted({r["category"] for r in rows})
        },
        "by_information": {
            key: summarize([r for r in rows if r["information"] == key])
            for key in ("FULL", "LIMITED")
        },
        "cases": rows,
    }


def compare(dataset, runs, reviews=None):
    if len(runs) != 3 or {r.manifest.mode for r in runs} != set(MODES):
        raise ValueError("Comparison needs exactly one run per mode")
    first = runs[0].manifest
    for run in runs[1:]:
        if (
            run.manifest.conditions != first.conditions
            or run.manifest.execution_kind != first.execution_kind
        ):
            raise ValueError("Comparison conditions differ; do not infer an improvement")
    reviews = reviews or {}
    reports = {r.manifest.mode: score(dataset, r, reviews.get(r.manifest.mode)) for r in runs}
    return {
        "schema_version": 1,
        "release_ready": False,
        "note": "Budgets are disclosed per mode. FULL/LIMITED separate information availability. "
        "AUTHORED_CONTROL/SCRIPTED_REPLAY comparisons are evaluator/plumbing checks only.",
        "reports": reports,
    }


def export_inputs(dataset):
    """No gold, outcomes, review guidance or scripted answers enter an execution input."""
    fields = {
        "id",
        "question",
        "work_id",
        "episode_from",
        "episode_to",
        "conversation",
        "conversation_truncated",
        "evidence_ids",
    }
    return {
        "dataset_sha256": fingerprint(dataset),
        "evidence": [e.model_dump(mode="json") for e in dataset.evidence],
        "questions": [q.model_dump(mode="json", include=fields) for q in dataset.questions],
    }
