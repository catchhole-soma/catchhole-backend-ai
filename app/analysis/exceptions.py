# analysis 패키지 내부에서만 사용하는 예외를 모아둔다.
# worker가 실패 사유를 구분하기 위한 목적
class LlmExtractionError(Exception):
    """LLM 응답을 설정 후보 구조로 변환하지 못했을 때 사용하는 analysis 내부 예외."""


class ComparisonValidationError(LlmExtractionError):
    """LLM 비교 응답을 도메인 결정으로 검증하지 못한 경우다."""


class OrderedInputContextError(ComparisonValidationError):
    """The frozen input/identity contract changed; never a candidate-local failure."""


class OrderedAnalysisIncompleteError(Exception):
    """Stop an ordered run after a reported batch failure without losing its code."""

    def __init__(self, failure_code):
        super().__init__("Ordered analysis stopped at an incomplete comparison batch.")
        self.failure_code = failure_code
