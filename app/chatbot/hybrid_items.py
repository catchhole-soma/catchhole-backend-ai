"""Logical setting coverage, independent of source retrieval and model transport."""

from dataclasses import dataclass

from app.chatbot.qa_render import safe_text, setting_label
from app.schemas.chat_hybrid import GroundedAnswer, ItemAnswer


def select_context_seeds(results, *, maximum):
    """Cover requested items before spending context on repeated mentions.

    A hit shared by several item queries is a useful common anchor (for example a
    capability list). Keep the strongest lexical tier per query, then greedily
    cover still-uncovered items. No story-specific words or episode numbers enter.
    """
    candidates, covers, scores = {}, {}, {}
    for item_ref, chunks in results:
        if not chunks:
            continue
        best = max(chunk.score for chunk in chunks)
        for chunk in chunks:
            if chunk.score < best:
                continue
            key = chunk.fingerprint
            candidates.setdefault(key, chunk)
            covers.setdefault(key, set()).add(item_ref)
            scores[key] = max(scores.get(key, 0), chunk.score)
    remaining = set().union(*covers.values()) if covers else set()
    selected = []
    while remaining and len(selected) < maximum:
        key = max(candidates, key=lambda k: (len(covers[k] & remaining), scores[k]))
        if not covers[key] & remaining:
            break
        selected.append(candidates.pop(key))
        remaining.difference_update(covers[key])
    return selected


@dataclass(frozen=True)
class LogicalItem:
    ref: str
    rows: tuple

    @property
    def label(self):
        item = self.rows[0].item
        return f"{safe_text(item.subject_name)} · {setting_label(item)}"


def group_items(rows, *, current_targets=False):
    grouped = {}
    for row in rows:
        item = row.item
        # A missing storage path is not proof that two unnamed rows mean the same thing.
        identity = (
            row.domain,
            item.subject_id,
            item.category,
            item.scope_name,
            item.setting_key if item.setting_key is not None else row.ref,
        )
        grouped.setdefault(identity, []).append(row)
    groups = [
        group
        for group in grouped.values()
        if not current_targets or any(row.item.kind == "CURRENT" for row in group)
    ]
    return [LogicalItem(f"I{n}", tuple(group)) for n, group in enumerate(groups, 1)]


def requested_facets(plan):
    if plan.requested_parts:
        return [part.ref for part in plan.requested_parts]
    return plan.requested_facets or [
        {"PROCESS": "HOW", "REASON": "WHY", "SCENE": "SCENE"}[plan.source_need]
    ]


def request_labels(plan):
    return {**FACET_LABELS, **{p.ref: p.description for p in plan.requested_parts}}


def empty_item_answer(items, facets):
    return GroundedAnswer(
        coverage="NONE",
        claims=[],
        conflicts=[],
        item_answers=[
            ItemAnswer(item_ref=i.ref, answers=[], missing_facets=list(facets)) for i in items
        ],
    )


def answer_evidence_refs(answer):
    return {
        ref
        for statement in [
            *answer.claims,
            *answer.conflicts,
            *(a for item in answer.item_answers for a in item.answers),
        ]
        for ref in statement.evidence_refs
    }


def event_matches(status, scope):
    return scope is None or status == {"ACTUAL": "OCCURRED", "PLANNED": "PLANNED"}[scope]


def apply_event_scope(answer, scope, facets, *, event_context=None):
    """A planned or uncertain event cannot satisfy a request for an actual event.

    This verifies the declared event classification, not the model's semantic
    reading of the source. Keep classified observations to explain the gap once.
    Omitted statuses are compatible only when the recorded plan has no scope.
    """
    if scope is None and event_context is None:
        return answer
    items = []
    for item in answer.item_answers:
        observations = [
            a
            for a in item.answers
            if (scope is None or a.event_status is not None)
            and (event_context is None or a.event_match == "MATCH")
        ]
        covered = {a.facet for a in observations if event_matches(a.event_status, scope)}
        items.append(
            ItemAnswer(
                item_ref=item.item_ref,
                answers=observations,
                missing_facets=[f for f in facets if f not in covered],
            )
        )
    claims = [
        claim.model_copy(
            update={
                "answered_facets": claim.answered_facets
                if event_matches(claim.event_status, scope)
                else []
            }
        )
        for claim in answer.claims
        if (scope is None or claim.event_status is not None)
        and (event_context is None or claim.event_match == "MATCH")
    ]
    covered = {f for c in claims for f in c.answered_facets}
    missing = [] if items else [f for f in facets if f not in covered]
    conflicts = [
        conflict
        for conflict in answer.conflicts
        if event_context is None or conflict.event_match == "MATCH"
    ]
    supported = bool(claims or conflicts or any(i.answers for i in items))
    complete = (
        supported
        and not conflicts
        and not answer.unanswered_row_refs
        and not missing
        and all(not i.missing_facets for i in items)
    )
    return GroundedAnswer(
        coverage="COMPLETE" if complete else "PARTIAL" if supported else "NONE",
        claims=claims,
        conflicts=conflicts,
        unanswered_row_refs=answer.unanswered_row_refs,
        item_answers=items,
        missing_facets=missing,
    )


def validate_item_answer(answer, items, facets):
    if answer.claims or answer.unanswered_row_refs or answer.missing_facets:
        raise ValueError("Logical item answers cannot contain row-level fallback prose")
    if {i.item_ref for i in answer.item_answers} != {i.ref for i in items}:
        raise ValueError("Every logical item must occur exactly once")
    for item in answer.item_answers:
        if {a.facet for a in item.answers} | set(item.missing_facets) != set(facets):
            raise ValueError("Every requested facet needs an answer or explicit uncertainty")
    complete = bool(items) and all(not i.missing_facets for i in answer.item_answers)
    supported = any(i.answers for i in answer.item_answers) or answer.conflicts
    expected = (
        "COMPLETE" if complete and not answer.conflicts else "PARTIAL" if supported else "NONE"
    )
    if answer.coverage != expected:
        raise ValueError("Coverage must describe requested facets, not reference presence")


def merge_item_answers(previous, latest, items, facets, *, event_scope=None):
    """Keep independently supported facets; explicit conflicts invalidate older facets.

    The caller keeps all previously used E references in the next model context and
    revalidates the final union. Missing new evidence alone is not a retraction.
    """
    old = {i.item_ref: i for i in previous.item_answers}
    new = {i.item_ref: i for i in latest.item_answers}
    conflict_rows = {r for c in latest.conflicts for r in c.row_refs}
    merged = []
    for item in items:
        conflict = any(row.ref in conflict_rows for row in item.rows)
        answers = {} if conflict else {a.facet: a for a in old[item.ref].answers}
        if not conflict:
            for candidate in new[item.ref].answers:
                prior = answers.get(candidate.facet)
                # A later mention of a plan does not erase an earlier occurrence.
                if (
                    prior is None
                    or event_matches(candidate.event_status, event_scope)
                    or not event_matches(prior.event_status, event_scope)
                ):
                    answers[candidate.facet] = candidate
        merged.append(
            ItemAnswer(
                item_ref=item.ref,
                answers=[answers[f] for f in facets if f in answers],
                missing_facets=[
                    f
                    for f in facets
                    if f not in answers or not event_matches(answers[f].event_status, event_scope)
                ],
            )
        )
    # A previously observed disagreement cannot disappear merely because a later
    # search had no new matching passage; retain it until the request finishes.
    conflicts = []
    for conflict in [*previous.conflicts, *latest.conflicts]:
        if conflict not in conflicts:
            conflicts.append(conflict)
    conflicts = conflicts[:4]
    conflict_rows = {r for c in conflicts for r in c.row_refs}
    merged = [
        ItemAnswer(item_ref=result.item_ref, answers=[], missing_facets=list(facets))
        if any(row.ref in conflict_rows for row in item.rows)
        else result
        for item, result in zip(items, merged, strict=True)
    ]
    supported = any(i.answers for i in merged) or conflicts
    complete = all(not i.missing_facets for i in merged) and not conflicts
    return GroundedAnswer(
        coverage="COMPLETE" if complete else "PARTIAL" if supported else "NONE",
        claims=[],
        conflicts=conflicts,
        item_answers=merged,
    )


FACET_LABELS = {"TIME": "시점", "HOW": "경위·방법", "WHY": "이유", "SCENE": "장면"}


def render_item_answers(
    answer, items, public_numbers, *, blocked_rows=frozenset(), event_scope=None, labels=None
):
    labels = labels or FACET_LABELS
    by_ref = {i.ref: i for i in items}
    parts, missing, blocked_subjects = [], {}, []
    for result in answer.item_answers:
        item = by_ref[result.item_ref]
        if any(row.ref in blocked_rows for row in item.rows):
            name = safe_text(item.rows[0].item.subject_name)
            if name not in blocked_subjects:
                blocked_subjects.append(name)
            continue
        cited = []
        for facet in result.answers:
            numbers = tuple(sorted({public_numbers[r] for r in facet.evidence_refs}))
            prefix = (
                "발생 여부가 불확실한 기록: "
                if facet.event_status == "UNCERTAIN" and event_scope
                else ""
            )
            statement = (f"{prefix}{safe_text(facet.text)}", numbers)
            if statement not in cited:
                cited.append(statement)
        # Combine citations only when every statement has the same supporting
        # sources. Different source sets keep their statement-level association.
        if cited and len({numbers for _, numbers in cited}) == 1:
            texts, numbers = " ".join(text for text, _ in cited), cited[0][1]
            statements = [f"{texts} (근거 {', '.join(map(str, numbers))})"]
        else:
            statements = [
                f"{text} (근거 {', '.join(map(str, numbers))})" for text, numbers in cited
            ]
        if statements:
            if result.missing_facets:
                needs = "·".join(labels[f] for f in result.missing_facets)
                only_plans = event_scope == "ACTUAL" and all(
                    a.event_status == "PLANNED" for a in result.answers
                )
                if only_plans:
                    statements.append(
                        f"계획만 확인되며, 실제로 이루어진 {needs} 정보는 확인하지 못했습니다."
                    )
                else:
                    statements.append(f"{needs} 정보는 이번 자료로 확인하지 못했습니다.")
            parts.append(f"- {item.label}: {' '.join(statements)}")
        elif result.missing_facets:
            missing.setdefault(tuple(result.missing_facets), []).append(item.label)
    for facets, item_labels in missing.items():
        names = ", ".join(item_labels)
        needs = "·".join(labels[f] for f in facets)
        parts.append(f"{names}의 {needs} 정보는 이번에 확인한 자료만으로 확정하기 어렵습니다.")
    if blocked_subjects:
        parts.append(
            f"{', '.join(blocked_subjects)}은 현재 등록 자료로 질문의 조건에 해당하는지 확정하지 못해 원문 설명을 보류했습니다."
        )
    return parts
