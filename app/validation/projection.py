"""GH349 amendment: the compact catalogue row COVERAGE/JUDGE models read.

Only the model prompt uses it. Catalogue reads, BASELINE lookups the model asks for, receipts and Worker decision
checks keep the full server projection; published/stored evidence bundles carry entry/subject refs only (Amendment 2).
"""

from copy import deepcopy

# Evidence, provenance, history collections and version counters: never in the compact model row.
DROPPED_STATE_KEYS = frozenset({
    "historyFacts", "supportingSources", "supportingSource", "provenance", "provenanceByPath", "identityEvidence",
    "removedSnapshotEntries", "valueJson", "evidenceSpans", "comparison", "explicitAuthorCorrections",
    "snapshotVersion", "currentSupportingEvidenceVersion", "version",
})
# Nested state values that stay: time points, absences and protection markers (evidence keys inside removed).
KEPT_STATE_STRUCTURES = frozenset({"currentNarrativeTime", "narrativeTime", "absences", "protectedSlots", "protectedPaths"})
# World-setting values are author data keyed by setting names, so they are copied verbatim.
VERBATIM_STATE_KEYS = frozenset({"propertiesJson"})
SLOT_FIELDS = ("factKey", "factType", "factValue", "temporalScope", "effectiveFromEpisodeNo")
# History facts a model-requested BASELINE read delivers: the timeline of values without evidence or provenance.
HISTORY_FACT_FIELDS = ("factType", "factKey", "factValue", "operation", "effectiveFromEpisodeNo", "temporalScope")
MAX_EXPANDED_HISTORY_FACTS = 300
_ROW_FIELDS = ("entry_ref", "subject_ref", "domain", "name", "aliases")


def _scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def _without_evidence(value):
    if isinstance(value, dict):
        return {key: _without_evidence(item) for key, item in value.items() if key not in DROPPED_STATE_KEYS}
    if isinstance(value, list):
        return [_without_evidence(item) for item in value]
    return value


def compact_state(state):
    """Scalars, lists of scalars, kept structures and `slots` reduced to their value fields; nothing else."""
    if not isinstance(state, dict):
        return deepcopy(state)
    result = {}
    for key, value in state.items():
        if key in DROPPED_STATE_KEYS:
            continue
        if key == "slots" and isinstance(value, dict):
            result[key] = {slot_key: {name: slot[name] for name in SLOT_FIELDS if name in slot and _scalar(slot[name])}
                           for slot_key, slot in value.items() if isinstance(slot, dict)}
        elif (key in VERBATIM_STATE_KEYS or _scalar(value)
              or isinstance(value, list) and all(_scalar(item) for item in value)):
            result[key] = deepcopy(value)
        elif key in KEPT_STATE_STRUCTURES:
            result[key] = _without_evidence(value)
    return result


def compact_setting_row(row):
    """`{entry_ref, subject_ref, domain, name, aliases, state}` (+ `author_correction` when the row carries it)."""
    result = {key: deepcopy(row[key]) for key in _ROW_FIELDS if key in row}
    result["state"] = compact_state(row.get("state"))
    if "author_correction" in row:
        result["author_correction"] = deepcopy(row["author_correction"])
    return result


def expanded_setting_row(row, max_history=MAX_EXPANDED_HISTORY_FACTS):
    """The compact row plus `state.historyFacts` reduced to scalar fact fields (newest `max_history` kept).

    A BASELINE lookup the model asks for used to deliver the full canonical row; a production character entry
    carried 209 KB of history with quotes and provenance, which alone exceeds the per-call input cap. The judge
    needs the value timeline and where each value came from, not the evidence text behind it: slots keep the
    scalar fields of their provenance (source alias, kind, episode number), history facts keep
    HISTORY_FACT_FIELDS plus the supporting source alias, and older facts beyond `max_history` are dropped with
    `historyTruncated` = number dropped.
    """
    result = compact_setting_row(row)
    state = row.get("state")
    if not isinstance(state, dict):
        return result
    slots = state.get("slots")
    if isinstance(slots, dict):
        for slot_key, slot in slots.items():
            origin = _scalar_fields(slot.get("provenance")) if isinstance(slot, dict) else {}
            if origin and slot_key in result["state"].get("slots", {}):
                result["state"]["slots"][slot_key]["provenance"] = origin
    history = state.get("historyFacts")
    if isinstance(history, list):
        facts = []
        for fact in history:
            if not isinstance(fact, dict):
                continue
            item = {name: fact[name] for name in HISTORY_FACT_FIELDS if name in fact and _scalar(fact[name])}
            origin = fact.get("supportingSource")
            if isinstance(origin, dict) and _scalar(origin.get("source_ref")) and origin.get("source_ref") is not None:
                item["source_ref"] = origin["source_ref"]
            facts.append(item)
        dropped = max(0, len(facts) - max_history)
        result["state"]["historyFacts"] = facts[dropped:]
        if dropped:
            result["state"]["historyTruncated"] = dropped
    return result


def _scalar_fields(value):
    """Scalar members of a provenance object (source alias, kind, episode number); never quotes or text."""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items()
            if _scalar(item) and item is not None and key not in {"quote", "text"}}
