"""Candidate execution ledger. No Gold matching, exception messages, or scoring here."""

from dataclasses import dataclass, field

from app.domain.enums import AnalysisFailureCode
from app.exceptions.failure_classification import comparison_failure_code
from evals.multi_stage_setting.contracts import (
    CandidateProcessingReason,
    CandidateProcessingRecord,
    CandidateProcessingStatus,
    EvaluationDomain,
    ExecutionFailure,
    ProcessingStage,
    ScenarioPrediction,
    Stage1Prediction,
    Stage2Prediction,
    stage2_source_candidate_ids,
)
from evals.multi_stage_setting.provider_diagnostics import provider_failure_details

_REASON = dict(zip(CandidateProcessingStatus, CandidateProcessingReason, strict=True))


@dataclass
class ProcessingTrace:
    episode_no: int | None = None
    candidates: list[Stage1Prediction] = field(default_factory=list)
    raw: list[Stage1Prediction] = field(default_factory=list)
    records: list[CandidateProcessingRecord] = field(default_factory=list)
    decisions: list[Stage2Prediction] = field(default_factory=list)
    active_ids: set[str] = field(default_factory=set)
    forwarded_ids: set[str] = field(default_factory=set)
    stage: ProcessingStage = "CHARACTER_STAGE1"

    def start(self, ids: list[str], stage: ProcessingStage, *, forwarded: bool = False) -> None:
        self.active_ids = set(ids)
        self.stage = stage
        if forwarded:
            self.forwarded_ids.update(ids)

    def record(
        self,
        candidate_id: str,
        domain: EvaluationDomain | str,
        status: CandidateProcessingStatus | str,
        *,
        failure_code: AnalysisFailureCode | None = None,
        decision: Stage2Prediction | None = None,
    ) -> None:
        status = CandidateProcessingStatus(status)
        if any(item.candidate_id == candidate_id for item in self.records):
            raise ValueError("Duplicate candidate processing result.")
        self.records.append(
            CandidateProcessingRecord(
                candidate_id=candidate_id,
                domain=domain,
                comparison_forwarded=candidate_id in self.forwarded_ids,
                status=status,
                reason_code=_REASON[status],
                stage=self.stage,
                failure_code=failure_code,
                decision_source_candidate_id=decision.source_candidate_id if decision else None,
                operation=decision.operation if decision else None,
            )
        )
        self.active_ids.discard(candidate_id)

    def complete(self, decision: Stage2Prediction) -> None:
        for source_id in stage2_source_candidate_ids(decision):
            self.record(source_id, decision.domain, "COMPARED", decision=decision)
        self.decisions.append(decision)

    def fail(self, ids: list[str], domain: EvaluationDomain | str, exc: BaseException) -> None:
        code = comparison_failure_code(exc)
        for source_id in ids:
            status = (
                "COMPARISON_FAILED" if source_id in self.forwarded_ids else "PREPARATION_FAILED"
            )
            self.record(source_id, domain, status, failure_code=code)

    def aborted(self, scenario_id: str, exc: BaseException) -> ScenarioPrediction:
        recorded = {item.candidate_id for item in self.records}
        code = comparison_failure_code(exc)
        for source in self.candidates:
            if source.candidate_id in recorded:
                continue
            if source.candidate_id in self.active_ids:
                self.fail([source.candidate_id], source.domain, exc)
            else:
                self.record(
                    source.candidate_id, source.domain, "EXECUTION_ABORTED", failure_code=code
                )
        return ScenarioPrediction(
            scenario_id=scenario_id,
            execution_failure=ExecutionFailure(
                episode_no=self.episode_no,
                stage=self.stage,
                failure_code=code,
                provider=provider_failure_details(exc),
            ),
            pipeline_status="EXECUTION_ABORTED",
            failed_stage=self.stage,
            raw_stage1=self.raw,
            stage1=self.candidates,
            stage2=self.decisions,
            processing_version=1,
            processing=self.records,
        )


def processing_outcomes(prediction: ScenarioPrediction) -> dict[str, dict]:
    """Legacy recovery uses explicit stored facts only, never missing-decision inference."""
    prediction.validate_processing_coverage()
    if prediction.processing_version == 1:
        return {
            r.candidate_id: {**r.model_dump(mode="json", by_alias=True), "recordOrigin": "RUNTIME"}
            for r in prediction.processing
        }
    decisions = {
        source_id: d for d in prediction.stage2 for source_id in stage2_source_candidate_ids(d)
    }
    outcomes = {}
    for source in prediction.stage1:
        decision = decisions.get(source.candidate_id)
        state = "RECORD_UNAVAILABLE"
        reason = "LEGACY_RECORD_MISSING"
        forwarded = None
        stage = None
        if decision is not None:
            state, reason, forwarded = "COMPARED", "DECISION_RECORDED", True
            stage = f"{source.domain}_STAGE2"
        elif source.candidate_kind == "CHARACTER_DISCOVERY":
            state, reason, forwarded = "NOT_APPLICABLE", "CHARACTER_DISCOVERY", False
        elif getattr(source, "match_status", None) == "UNRESOLVED":
            state, reason, forwarded = "WAITING_FOR_CHARACTER", "CHARACTER_UNRESOLVED", False
        elif getattr(source, "match_status", None) == "AMBIGUOUS":
            state, reason, forwarded = "AMBIGUOUS_CHARACTER", "CHARACTER_AMBIGUOUS", False
        outcomes[source.candidate_id] = {
            "candidateId": source.candidate_id,
            "domain": source.domain.value,
            "status": state,
            "reasonCode": reason,
            "comparisonForwarded": forwarded,
            "stage": stage,
            "failureCode": None,
            "decisionSourceCandidateId": decision.source_candidate_id if decision else None,
            "operation": decision.operation.value if decision else None,
            "recordOrigin": "LEGACY_CONFIRMED" if state != "RECORD_UNAVAILABLE" else "UNAVAILABLE",
        }
    return outcomes
