"""Python parity of the server's validation projection, plus the compact row the JUDGE model reads.

The Worker in an execution job reads SUBJECTS/SETTINGS from Java; a job-less callable (tests, the local
experiment fixture client) builds the same rows here from raw baseline entries. Shared vectors in
`docs/contracts/validation-execution-v1.json` check both sides against each other.
"""

import re
from copy import deepcopy

from app.validation.contracts import IncompleteInspection

# Domains whose rows are current values; `worldHistory` rows (one per confirmed change) never reach a judge.
CURRENT_DOMAINS = frozenset({"characters", "worldSettings"})

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
_SERVER_KEYS = frozenset({"actualCharacterId", "actualWorldSettingId", "provisionalSubjectKey", "subjectRef",
                          "sourceKey", "sourceVersion", "sourceHash"})


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
    """`{entry_ref, subject_ref, domain, name, aliases, state}` (+ `author_correction` when the row carries it).

    The only setting form a JUDGE model reads: current values without history, evidence, provenance or versions.
    """
    result = {key: deepcopy(row[key]) for key in _ROW_FIELDS if key in row}
    result["state"] = compact_state(row.get("state"))
    if "author_correction" in row:
        result["author_correction"] = deepcopy(row["author_correction"])
    return result


def model_state(value, sources=()):
    """Preserve values, history, conditions and times; exclude server IDs/storage metadata (server parity)."""
    if isinstance(value, dict):
        result = {key: model_state(item, sources) for key, item in value.items()
                  if not re.search(r"(?:Id|Ids|Hash|S3Key|S3Version|Token)$", key) and key not in _SERVER_KEYS}
        source_id = (value.get("sourceEpisodeId") or value.get("episodeId")
                     or value.get("sourceSettingBookRevisionId") or value.get("revisionId"))
        source_hash = value.get("sourceHash") or value.get("contentHash")
        source_version = value.get("sourceVersion", value.get("contentS3Version"))
        for source in sources:
            if (source_id in {source.metadata.get("episodeId"), source.metadata.get("revisionId")}
                    and source_id is not None and source_hash == source.content_hash
                    and (source_version is None or source_version == source.source_version)
                    and value.get("availability") != "UNAVAILABLE"
                    and value.get("sourceAvailability") != "UNAVAILABLE"):
                result["source_ref"] = source.ref
                break
        return result
    if isinstance(value, list):
        return [model_state(item, sources) for item in value]
    return value


def model_settings(entries, sources=()):
    """Server `toValidationSettings` parity: one row per baseline entry, S refs in first-seen subject order."""
    subjects, result = {}, []
    for row in entries:
        if not isinstance(row, dict) or not isinstance(row.get("state"), dict):
            raise IncompleteInspection("INVALID_BASELINE_CONTRACT")
        state = row["state"]
        identity = row.get("subjectRef") or "world:" + str(state.get("targetWorldSettingId") or row["entryRef"])
        subjects.setdefault(identity, f"S{len(subjects) + 1}")
        result.append({
            "entry_ref": row["entryRef"],
            "subject_ref": subjects[identity],
            "domain": row["domain"],
            "name": state.get("name") or state.get("subjectName"),
            "aliases": state.get("aliases", []),
            "state": model_state(state, sources),
            "author_correction": state.get("explicitAuthorCorrection"),
        })
    return result


def subject_catalogue(settings):
    """SUBJECTS parity (GH349): one row per subject in S-number order; the first entry's domain/name and the
    ordered union of every entry's aliases."""
    rows = {}
    for row in settings:
        subject = rows.setdefault(row["subject_ref"], {"subject_ref": row["subject_ref"], "domain": row["domain"],
                                                      "name": row["name"], "aliases": []})
        for alias in row.get("aliases") or []:
            if alias not in subject["aliases"]:
                subject["aliases"].append(alias)
    return sorted(rows.values(), key=lambda row: subject_order(row["subject_ref"]))


def current_settings(settings, subject_refs):
    """SETTINGS parity: rows of the requested subjects in current domains only, in B-number order."""
    wanted = set(subject_refs)
    rows = [row for row in settings if row["subject_ref"] in wanted and row["domain"] in CURRENT_DOMAINS]
    return sorted(rows, key=lambda row: entry_order(row["entry_ref"]))


def source_catalogue(sources):
    """Server `toValidationSourceCatalogue` parity: short refs plus reading-order metadata, no storage IDs."""
    return [dict(source_ref=source.ref, source_kind=source.kind,
                 **{key: source.metadata[key] for key in ("episodeNo", "revisionNo", "referenceTimeKind",
                                                          "referenceEpisodeNo") if key in source.metadata})
            for source in sources]


def setting_value_leaves(row):
    """The stored setting values a basis quote may cite (Java parity, shared contract `basisQuotes`).

    `characters`: every string `state.slots.<key>.factValue`; `worldSettings`: every string leaf under
    `state.propertiesJson` (dict values and list items, recursively; the value itself if it is a string).
    Keys, names, aliases and any other domain are never quotable.
    """
    state = row.get("state") if isinstance(row, dict) else None
    if not isinstance(state, dict):
        return []
    if row.get("domain") == "characters":
        slots = state.get("slots")
        return [slot["factValue"] for slot in slots.values()
                if isinstance(slot, dict) and isinstance(slot.get("factValue"), str)] if isinstance(slots, dict) else []
    if row.get("domain") != "worldSettings":
        return []
    leaves, pending = [], [state.get("propertiesJson")]
    while pending:
        value = pending.pop(0)
        if isinstance(value, str):
            leaves.append(value)
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return leaves


def bundle_settings(rows):
    """GH349 Amendment 2: published bundle settings are refs only; the Backend checks the ref set."""
    return [{"entry_ref": row["entry_ref"], "subject_ref": row["subject_ref"]} for row in rows]


def _numbered(prefix, ref):
    match = re.fullmatch(prefix + r"([1-9][0-9]*)", ref) if isinstance(ref, str) else None
    return (int(match.group(1)), ref) if match else (float("inf"), str(ref))


def subject_order(ref):
    """Server S-refs sort by their number (S2 before S10)."""
    return _numbered("S", ref)


def entry_order(ref):
    """Server B-refs sort by their number (B2 before B10)."""
    return _numbered("B", ref)
