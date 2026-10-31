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
