"""Conservative request-local chronology; Spring remains the write authority."""

from dataclasses import dataclass

from app.domain.enums import (
    AnalysisSourceKind,
    CharacterFactTemporalScope,
    SettingBookReferenceTimeKind,
)
from app.schemas.analysis_context import AnalysisStateProvenance
from app.schemas.analysis_source import WorkerAnalysisSource


@dataclass(frozen=True)
class NarrativePoint:
    """WORK_START is a distinct anchor, never a source episode numbered zero."""

    kind: SettingBookReferenceTimeKind
    episode_no: int | None = None

    def is_strictly_after(self, other):
        if self.kind == SettingBookReferenceTimeKind.WORK_START:
            return False
        if other.kind == SettingBookReferenceTimeKind.WORK_START:
            return True
        return self.episode_no > other.episode_no


def _book_point(kind, number, effective_from_episode_no=None):
    if kind == SettingBookReferenceTimeKind.WORK_START:
        return NarrativePoint(kind)
    if kind == SettingBookReferenceTimeKind.EPISODE:
        return NarrativePoint(kind, number)
    if effective_from_episode_no is not None:
        return NarrativePoint(SettingBookReferenceTimeKind.EPISODE, effective_from_episode_no)
    return None


def _source_point(source, effective_from_episode_no=None):
    if source is None:
        return None
    if source.source_kind == AnalysisSourceKind.SETTING_BOOK:
        return _book_point(source.reference_time_kind, source.reference_episode_no, effective_from_episode_no)
    if source.episode_no is not None:
        return NarrativePoint(SettingBookReferenceTimeKind.EPISODE, source.episode_no)
    return None


def _current_point(provenance):
    if provenance is None:
        return None
    if provenance.source_kind == AnalysisSourceKind.SETTING_BOOK:
        return _book_point(provenance.source_reference_time_kind, provenance.source_reference_episode_no,
                           provenance.effective_from_episode_no)
    if provenance.source_episode_no is not None:
        return NarrativePoint(SettingBookReferenceTimeKind.EPISODE, provenance.source_episode_no)
    return None


def _current_points(provenance):
    if provenance is None or not provenance.current_source_times:
        return [_current_point(provenance)]
    points = []
    for current in provenance.current_source_times:
        if current.temporal_scope != CharacterFactTemporalScope.PRESENT:
            points.append(None)
        elif current.source_kind == AnalysisSourceKind.SETTING_BOOK:
            points.append(_book_point(current.reference_time_kind, current.reference_episode_no,
                                      current.effective_from_episode_no))
        elif current.source_kind == AnalysisSourceKind.EPISODE:
            points.append(NarrativePoint(SettingBookReferenceTimeKind.EPISODE, current.source_episode_no))
        else:
            points.append(None)
    return points


def has_book_current(provenance):
    return provenance is not None and (provenance.source_kind == AnalysisSourceKind.SETTING_BOOK
        or any(item.source_kind == AnalysisSourceKind.SETTING_BOOK for item in provenance.current_source_times))


def validate_current_change(
    *, source: WorkerAnalysisSource | None, current: AnalysisStateProvenance | None = None,
    temporal_scope=CharacterFactTemporalScope.PRESENT, effective_from_episode_no=None,
    changing_existing=False, operation=None,
):
    """Reject a concrete projection that the server must hold for human review.

    Unknown metadata is not reconstructed from IDs, upload order or reviewSource.
    Automatic callers may supply their validated operation: a destination MERGE
    preserves existing meaning, and a transition may lack an optional BOOK time.
    The default remains conservative. This validates supplied per-slot metadata;
    Spring rechecks persisted
    sources, deletion markers, dependencies and target versions before applying.
    """
    incoming_book = source is not None and source.source_kind == AnalysisSourceKind.SETTING_BOOK
    current_book = has_book_current(current)
    if incoming_book:
        if temporal_scope != CharacterFactTemporalScope.PRESENT:
            raise ValueError("Book reference time requires review before changing current state.")
        if effective_from_episode_no is not None and (
            source.reference_time_kind == SettingBookReferenceTimeKind.WORK_START
            or (source.reference_time_kind == SettingBookReferenceTimeKind.EPISODE
                and effective_from_episode_no > source.reference_episode_no)
        ):
            raise ValueError("Book reference time requires review before changing current state.")
        if changing_existing and current is not None and current.current_value_protected is True:
            raise ValueError("Book cannot change protected current state.")
    if changing_existing and (incoming_book or current_book):
        if temporal_scope != CharacterFactTemporalScope.PRESENT:
            raise ValueError("Book reference time requires review before changing current state.")
        incoming_point = _source_point(source, effective_from_episode_no)
        automatic_transition = operation in {"UPDATE", "REMOVE", "MERGE"}
        optional_incoming = (automatic_transition and incoming_book
            and source.reference_time_kind == SettingBookReferenceTimeKind.UNSPECIFIED)
        if incoming_point is None and not optional_incoming:
            raise ValueError("Book reference time requires review before changing current state.")
        optional_current = (
            [item.source_kind == AnalysisSourceKind.SETTING_BOOK
             and item.reference_time_kind == SettingBookReferenceTimeKind.UNSPECIFIED
             and item.temporal_scope == CharacterFactTemporalScope.PRESENT
             for item in current.current_source_times]
            if current is not None and current.current_source_times else
            [current is not None and current.source_kind == AnalysisSourceKind.SETTING_BOOK
             and current.source_reference_time_kind == SettingBookReferenceTimeKind.UNSPECIFIED]
        )
        for point, optional in zip(_current_points(current), optional_current, strict=True):
            if point is None:
                if automatic_transition and optional:
                    continue
                raise ValueError("Book reference time requires review before changing current state.")
            if (incoming_point is not None and operation != "MERGE"
                    and not incoming_point.is_strictly_after(point)):
                raise ValueError("Book reference time requires review before changing current state.")
