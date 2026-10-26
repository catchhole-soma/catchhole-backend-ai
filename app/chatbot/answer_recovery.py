"""Conservative recovery of independently grounded request-part claims."""

from app.schemas.chat_hybrid import GroundedAnswer


def recover_claims(answer, requested, ground, *, evidence_refs, row_refs):
    """Keep verified statements; never repair words or treat uncertainty as coverage.

    Item/row completeness and conflicts need their own contracts. They deliberately
    stay on the existing validation retry path rather than silently losing a row.
    """
    requested = set(requested)
    if not requested or answer.item_answers or answer.conflicts or answer.unanswered_row_refs:
        return None
    covered = {f for c in answer.claims for f in c.answered_facets}
    missing = set(answer.missing_facets)
    if covered | missing != requested or any(
        not set(c.related_facets) <= requested
        or not set(c.evidence_refs) <= evidence_refs
        or not set(c.row_refs) <= row_refs
        for c in answer.claims
    ):
        return None

    kept, changed = [], False
    uncertain = set(missing)
    for claim in answer.claims:
        try:
            claim = ground(claim)
        except ValueError as exc:
            if str(exc) not in {"GROUNDING_QUOTE_NOT_FOUND", "GROUNDING_AMBIGUOUS_QUOTE"}:
                return None
            # Reject the whole statement, including any otherwise valid quotes.
            # Removing just the bad quote could leave unsupported words published.
            changed = True
            uncertain.update(claim.answered_facets)
            continue
        kept.append(claim)
    if not changed and not any(set(c.answered_facets) & uncertain for c in kept):
        return None
    if not kept:
        return None
    # A single facet can represent several requested facts. If any contributing
    # claim was dropped, another claim with the same facet cannot certify that the
    # entire request survived. Retain its verified context as related instead.
    normalized = []
    for claim in kept:
        if set(claim.answered_facets) & uncertain:
            claim = claim.model_copy(
                update={
                    "answered_facets": [f for f in claim.answered_facets if f not in uncertain],
                    "related_facets": list(
                        dict.fromkeys(
                            [
                                *claim.related_facets,
                                *(f for f in claim.answered_facets if f in uncertain),
                            ]
                        )
                    ),
                }
            )
        normalized.append(claim)
    kept = normalized
    covered = {f for c in kept for f in c.answered_facets}
    remaining = [f for f in requested if f not in covered]
    return GroundedAnswer.model_validate(
        {
            **answer.model_dump(),
            "claims": kept,
            "coverage": "PARTIAL",
            "missing_facets": sorted(remaining),
            "missing_explanations": [
                e for e in answer.missing_explanations if e.facet in remaining
            ],
        }
    )
