"""Pure stage projections over frozen references. Selection never changes stored input."""
from dataclasses import dataclass
from uuid import UUID

from app.analysis.character_name_resolver import (
    is_usable_subject_resolution_name,
    normalize_character_name,
)
from app.schemas.analysis_context import WorkerAnalysisReference


@dataclass(frozen=True)
class PendingSubjectScope:
    domain: str
    actual_subject_ids: tuple[UUID, ...] = ()
    provisional_subject_keys: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    world_category: str | None = None


def names_overlap(left, right):
    """Use the ordered character resolver's existing normalized overlap rule."""
    if not is_usable_subject_resolution_name(left) or not is_usable_subject_resolution_name(right):
        return False
    left, right = normalize_character_name(left), normalize_character_name(right)
    return left == right or (min(len(left), len(right)) >= 2 and (left in right or right in left))


def _matches(reference, scope):
    if reference.domain != scope.domain:
        return False
    selection = reference.selection
    if selection is not None:
        if (scope.world_category and selection.world_category
                and scope.world_category != selection.world_category):
            return False
        actual = set(selection.actual_subject_ids)
        provisional = set(selection.provisional_subject_keys)
        if actual.intersection(scope.actual_subject_ids) or provisional.intersection(scope.provisional_subject_keys):
            return True
        # A known contradictory link overrides a coincident display name. A
        # different identity kind can be a promoted provisional, so only compare
        # identities when both sides carry that same kind of linkage.
        if (actual and scope.actual_subject_ids) or (provisional and scope.provisional_subject_keys):
            return False
    names = (reference.subject_name, selection.raw_subject_mention if selection else None)
    return any(names_overlap(name, spelling) for name in names for spelling in scope.names)


def select_pending_references(
    references: tuple[WorkerAnalysisReference, ...], scopes: tuple[PendingSubjectScope, ...],
) -> tuple[WorkerAnalysisReference, ...]:
    """Return original objects, once, in frozen order; no fact/scope truncation."""
    return tuple(reference for reference in references if any(_matches(reference, scope) for scope in scopes))


def character_scopes(candidates, subjects=()):
    scopes = []
    for candidate in candidates:
        names = tuple(name for name in (getattr(candidate, 'entity_name', None),
                      getattr(candidate, 'raw_entity_mention', None)) if name)
        scopes.append(PendingSubjectScope('characters', names=names))
        for subject in subjects:
            spellings = (subject.name, *subject.aliases)
            if any(names_overlap(name, spelling) for name in names for spelling in spellings):
                scopes.append(PendingSubjectScope('characters',
                    actual_subject_ids=((subject.actual_character_id,) if subject.actual_character_id else ()),
                    provisional_subject_keys=((subject.provisional_subject_key,) if subject.provisional_subject_key else ()),
                    names=spellings))
    return tuple(scopes)


def world_scopes(candidates, subjects=()):
    scopes = []
    for candidate in candidates:
        category = candidate.category
        names = (candidate.subject_name,)
        scopes.append(PendingSubjectScope('worldSettings', names=names, world_category=category))
        for subject_category, subject in subjects:
            spellings = (subject.subject_name, *getattr(subject, 'aliases', ()))
            if category == subject_category and any(names_overlap(name, spelling) for name in names for spelling in spellings):
                scopes.append(PendingSubjectScope('worldSettings',
                    actual_subject_ids=((subject.world_setting_id,) if subject.world_setting_id else ()),
                    provisional_subject_keys=((subject.provisional_subject_key,) if subject.provisional_subject_key else ()),
                    names=spellings, world_category=category))
    return tuple(scopes)


def character_comparison_scope(name, candidates, advisory=None, provisional_key=None):
    actual = (advisory or {}).get('characterId')
    try:
        actual_ids = (UUID(str(actual)),) if actual else ()
    except (ValueError, TypeError):
        actual_ids = ()
    provisional_key = provisional_key or (advisory or {}).get('provisionalSubjectKey')
    names = [name, *((advisory or {}).get('selectionNames') or ())]
    for candidate in candidates:
        names.extend(getattr(candidate, field, None) for field in ('entity_name', 'raw_entity_mention'))
    return PendingSubjectScope('characters', actual_subject_ids=actual_ids,
        provisional_subject_keys=(provisional_key,) if provisional_key else (),
        names=tuple(value for value in names if value))


def world_comparison_scopes(category, candidates, targets):
    # Targets have already been resolved by Spring; every target is relevant.
    return tuple(PendingSubjectScope('worldSettings',
        actual_subject_ids=((target.world_setting_id,) if target.world_setting_id else ()),
        provisional_subject_keys=((target.provisional_subject_key,) if target.provisional_subject_key else ()),
        names=(target.subject_name, *getattr(target, 'aliases', ()),
               *(candidate.subject_name for candidate in candidates)), world_category=category)
        for target in targets) or tuple(PendingSubjectScope('worldSettings',
            names=(candidate.subject_name,), world_category=category) for candidate in candidates)


def _schema_status_key(name, hints):
    """Resolve exact, alias, then trailing wildcard, matching the frozen schema."""
    if not name:
        return None
    name = name.strip()
    tiers = [[], [], []]
    for hint in hints:
        key = hint.schema_key.strip()
        namespace = key.rpartition('.')[0]
        if key == name:
            tiers[0].append((hint, key))
        if any(alias and '.' not in alias and name in (alias, f'{namespace}.{alias}' if namespace else alias)
               for alias in (value.strip() for value in hint.aliases)):
            tiers[1].append((hint, key))
        pattern = hint.attribute_pattern or ''
        if pattern.endswith('.*') and pattern.count('*') == 1 and name.startswith(pattern[:-1]) and len(name) > len(pattern)-1:
            tiers[2].append((hint, name))
    for matches in tiers:
        if matches:
            if len(matches) != 1:
                return None
            hint, key = matches[0]
            # Older schema payloads carry a canonical namespace, not factType.
            fact_type = hint.canonical_fact_type or (hint.attribute_pattern or hint.schema_key).partition('.')[0].upper()
            return key if fact_type == 'STATUS' else None
    return None


def episode_character_hints(references, schema_hints):
    names, statuses = [], []
    seen_linked_statuses = set()
    for reference in references:
        if reference.domain != 'characters':
            continue
        if reference.subject_name and reference.subject_name not in names:
            names.append(reference.subject_name)
        selection = reference.selection
        key = (selection.canonical_fact_key or reference.setting_name) if selection else reference.setting_name
        if selection is not None and selection.fact_type is not None:
            if selection.fact_type != 'STATUS':
                continue
        else:
            key = _schema_status_key(key, schema_hints)
            if key is None:
                continue
        value_json = selection.value_json if selection else None
        active = value_json.get('active') if isinstance(value_json, dict) else None
        if not isinstance(active, bool):
            active = None
        identity = ((selection.actual_subject_ids, selection.provisional_subject_keys) if selection else ((), ()))
        if any(identity):
            fingerprint = (identity, reference.subject_name, key, reference.value, active)
            if fingerprint in seen_linked_statuses:
                continue
            seen_linked_statuses.add(fingerprint)
        statuses.append({'character_name': reference.subject_name, 'fact_key': key,
            'fact_value': reference.value, 'active': active,
            'confirmation_status': 'UNRESOLVED', 'applied_to_current_state': False})
    return {'names': names, 'statuses': statuses}
