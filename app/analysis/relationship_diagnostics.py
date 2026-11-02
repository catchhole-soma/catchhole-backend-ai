"""Bounded relationship failure metadata, never model output or manuscript text."""

from app.schemas.worker import WorkerFailureDiagnostics


def annotate_relationship_failure(error, *, stage, reason, checks=(), excerpts=()):
    error.failure_diagnostics = WorkerFailureDiagnostics(
        origin="WORKER", stage=stage, reasonCode=reason, checks=list(checks), excerpts=list(excerpts),
    )
    return error


class RelationshipDecisionValidationError(ValueError):
    """A fixed validation rule code; message is a constant authored in code."""

    def __init__(self, reason, message, checks=(), excerpts=()):
        super().__init__(message)
        self.reason_code = reason
        self.checks = tuple(checks)
        self.excerpts = tuple(excerpts)
