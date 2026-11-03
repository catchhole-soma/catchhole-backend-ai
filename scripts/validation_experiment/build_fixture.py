"""Raw read-only export (`baseline-raw.json`, see export_baseline.sql) -> experiment `fixture.json`.

Mirrors the server capture (`ValidationBaselineService.capture` + `normalize`) closely enough for the judge
input, which only reads compact current rows (`compact_setting_row`) and the planner, which only reads names and
aliases. Characters keep `CharacterSnapshotAccessor.read` slot values; world settings keep their properties;
world history rows keep the exported fields. Everything dropped or assumed is listed in `approximations`.
"""

import json
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256

from scripts.validation_experiment.common import HarnessError, file_sha256, manuscript_path

FIXTURE_VERSION = 1
ENVELOPE = "__catchhole_snapshot"
GROUPS = (("PROFILE", "profileJson"), ("STAT", "statsJson"), ("SKILL", "skillsJson"), ("ITEM", "itemsJson"))
APPROXIMATIONS = [
    ("historyFacts, supportingSources, provenance, provenanceByPath and identityEvidence are omitted "
     "(compact_setting_row drops them; the judge reads current values only)"),
    "world history rows have no evidenceSpans, comparison or narrativeTime (never sent to the judge)",
    "evidence availability checks and sanitizing are not applied",
    "subject (S) order follows the export order when created_at ties",
    "explicitAuthorCorrections is empty (corrections are report-scoped and were purged with the report)",
]


def _rows(raw, key):
    value = raw.get(key)
    return value if isinstance(value, list) else []


def _fact_value(raw_value):
    """`CharacterSnapshotAccessor.toFactValue`: `value` (text or JSON), else a textual `name`, else the scalar."""
    if raw_value is None:
        return None
    primary = raw_value.get("value") if isinstance(raw_value, dict) else raw_value
    if primary is not None:
        if isinstance(primary, (dict, list)):
            return json.dumps(primary, ensure_ascii=False, separators=(",", ":"))
        return str(primary).lower() if isinstance(primary, bool) else str(primary)
    name = raw_value.get("name") if isinstance(raw_value, dict) else None
    return name if isinstance(name, str) else None


def _envelope(stored):
    if isinstance(stored, dict) and len(stored) == 1 and isinstance(stored.get(ENVELOPE), dict):
        envelope = stored[ENVELOPE]
        if (envelope.get("format") == "character-snapshot-entry" and envelope.get("version") == 1
                and "factValue" in envelope and "valueJson" in envelope):
            return envelope
    return None


def character_slots(row, sources):
    """`TYPE:key` -> `{factType, factKey, factValue}` sorted, plus the source episode of each chosen value."""
    slots, origins = {}, {}

    def put(fact_type, key, stored, *, persisted_value=None):
        envelope = _envelope(stored)
        origin = None
        if persisted_value is not None:
            value = persisted_value
        elif envelope is not None:
            value = None if envelope["factValue"] is None else str(envelope["factValue"])
        else:
            facts = sources.get((fact_type, key), [])
            if facts:
                last = max(facts, key=lambda fact: fact.get("sourceOrder", 0))
                value, origin = last.get("factValue"), last.get("sourceEpisodeNo")
            else:
                value = _fact_value(stored)
        if origin is None:
            facts = sources.get((fact_type, key), [])
            origin = max(facts, key=lambda fact: fact.get("sourceOrder", 0)).get("sourceEpisodeNo") if facts else None
        slots[f"{fact_type}:{key}"] = {"factType": fact_type, "factKey": key, "factValue": value}
        origins[f"{fact_type}:{key}"] = origin

    for fact_type, column, key in (("AGE", "currentAge", "age"), ("LEVEL", "currentLevel", "level")):
        if row.get(column) is not None:
            put(fact_type, key, {"value": row[column]}, persisted_value=str(row[column]))
    for fact_type, column in GROUPS:
        group = row.get(column)
        if isinstance(group, dict):
            for key, stored in group.items():
                put(fact_type, key, stored)
    statuses = row.get("statusesJson")
    if isinstance(statuses, dict):
        for key, stored in statuses.items():
            put("TIME" if key.startswith("time.") else "STATUS", key, stored)
    order = sorted(slots)
    return {key: slots[key] for key in order}, {key: origins[key] for key in order}


def _protected_paths(raw):
    paths, skipped = [], 0
    for element in raw or []:
        value = element
        if isinstance(element, str):
            try:
                value = json.loads(element)
            except ValueError:
                skipped += 1
                continue
        if isinstance(value, list) and len(value) == 2:
            paths.append({"scopeName": value[0], "settingName": value[1]})
        elif isinstance(value, dict) and "settingName" in value:
            paths.append({"scopeName": value.get("scopeName"), "settingName": value["settingName"]})
        else:
            skipped += 1
    return paths, skipped


def convert(raw, *, target_no, target_file, target_hash, exclude_target_or_later=False):
    """Return `(fixture, warnings)`; `warnings` counts rows sourced from the target episode or later."""
    if raw.get("exportVersion") != 1 or raw.get("targetEpisodeNo") != target_no:
        raise HarnessError("Unexpected export version or target episode number")
    warnings = Counter()
    snapshot_sources = {}
    for row in _rows(raw, "snapshotSources"):
        snapshot_sources.setdefault(row["characterId"], {}).setdefault((row["factType"], row["factKey"]), []).append(row)
    discoveries = {}
    for row in _rows(raw, "discoveries"):
        discoveries.setdefault(row["matchedCharacterId"], []).append(row)

    entries = []
    for row in _rows(raw, "characters"):
        name = row["name"]
        found = discoveries.get(row["id"], [])
        aliases = []
        for discovery in found:
            alias = (discovery.get("entityName") or "").strip()
            if alias and alias != name and alias not in aliases:
                aliases.append(alias)
        slots, origins = character_slots(row, snapshot_sources.get(row["id"], {}))
        later = [key for key, episode in origins.items() if episode is not None and episode >= target_no]
        warnings["character_slots_from_target_or_later"] += len(later)
        known = [episode for episode in [*(d.get("sourceEpisodeNo") for d in found), *origins.values()]
                 if episode is not None]
        first_seen_later = bool(known) and min(known) >= target_no
        warnings["characters_first_seen_target_or_later"] += first_seen_later
        if exclude_target_or_later:
            if first_seen_later:
                continue
            slots = {key: value for key, value in slots.items() if key not in later}
        protected = row.get("manuallyEditedSlots")
        entries.append({"domain": "characters", "subjectRef": f"character:{row['id']}", "state": {
            "name": name, "aliases": aliases, "protectedSlots": protected if isinstance(protected, list) else [],
            "snapshotVersion": row.get("snapshotVersion"), "currentNarrativeTime": row.get("currentNarrativeTime"),
            "historyFacts": [], "absences": {}, "slots": slots}})
    skipped_paths = 0
    for row in _rows(raw, "worldSettings"):
        earliest, latest = row.get("earliestSourceEpisodeNo"), row.get("latestSourceEpisodeNo")
        if earliest is not None and earliest >= target_no:
            warnings["world_settings_first_seen_target_or_later"] += 1
            if exclude_target_or_later:
                continue
        elif latest is not None and latest >= target_no:
            warnings["world_settings_with_target_or_later_sources_kept"] += 1
        paths, skipped = _protected_paths(row.get("manuallyEditedPaths"))
        skipped_paths += skipped
        entries.append({"domain": "worldSettings", "subjectRef": f"world:{row['id']}", "state": {
            "category": row.get("category"), "subjectName": row.get("subjectName"),
            "normalizedSubjectName": row.get("normalizedSubjectName"), "version": row.get("version"),
            "propertiesJson": row.get("propertiesJson") or {}, "protectedPaths": paths,
            "latestSourceEpisodeNo": latest, "provisionalSubjectKey": None}})
    history_keys = ("candidateId", "targetWorldSettingId", "category", "subjectName", "scopeName", "settingName",
                    "value", "operation", "historyOnly", "effectiveFromEpisodeNo", "temporalScope")
    for row in _rows(raw, "worldHistory"):
        entries.append({"domain": "worldHistory",
                        "state": {**{key: row.get(key) for key in history_keys}, "sourceAvailability": "AVAILABLE"}})
    if skipped_paths:
        warnings["protected_paths_unparsed"] = skipped_paths

    episodes = sorted((row for row in _rows(raw, "episodes")
                       if row.get("status") != "ARCHIVED" and row["episodeNo"] < target_no),
                      key=lambda row: row["episodeNo"])
    warnings = +warnings  # drop zero counters
    counts = Counter(entry["domain"] for entry in entries)
    fixture = {
        "fixtureVersion": FIXTURE_VERSION,
        "exportedAt": raw.get("exportedAt") or datetime.now(UTC).isoformat(),
        "work": {"workId": raw["workId"], "memberId": 1},
        "target": {"episodeNo": target_no, "file": target_file, "contentHash": target_hash},
        "episodes": [{"episodeId": row["id"], "episodeNo": row["episodeNo"], "contentHash": row["contentHash"],
                      "contentS3Key": row["contentS3Key"], "contentS3Version": row.get("contentS3Version")}
                     for row in episodes],
        "baselineEntries": entries,
        "counts": {domain: counts.get(domain, 0) for domain in ("characters", "worldSettings", "worldHistory")},
        "excludedTargetOrLater": exclude_target_or_later,
        "warnings": dict(warnings),
        "approximations": APPROXIMATIONS,
    }
    return fixture, warnings


def fixture_hash(fixture):
    return sha256(json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build(raw, manuscripts, *, target_no, target_file=None, exclude_target_or_later=False, source_git=None):
    path = manuscript_path(manuscripts, target_no, target_file)
    fixture, warnings = convert(raw, target_no=target_no, target_file=unicodedata.normalize("NFC", path.name),
                                target_hash=file_sha256(path),
                                exclude_target_or_later=exclude_target_or_later)
    fixture["sourceGit"] = source_git or {}
    mismatched = []
    for episode in fixture["episodes"]:
        try:
            local = file_sha256(manuscript_path(manuscripts, episode["episodeNo"]))
        except HarnessError:
            local = None
        if local != episode["contentHash"]:
            mismatched.append(episode["episodeNo"])
    fixture["localManuscriptHashMismatch"] = mismatched
    return fixture, warnings
