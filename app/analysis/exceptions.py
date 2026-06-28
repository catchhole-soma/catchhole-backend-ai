# analysis 패키지 내부에서만 사용하는 예외를 모아둔다.
# worker가 실패 사유를 구분하기 위한 목적
class LlmExtractionError(Exception):
    """LLM 응답을 설정 후보 구조로 변환하지 못했을 때 사용하는 analysis 내부 예외."""
