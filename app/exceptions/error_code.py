from enum import StrEnum

from starlette import status


class ErrorCode(StrEnum):
    ANALYSIS_JOB_NOT_FOUND = "ANALYSIS_JOB_NOT_FOUND"
    EPISODE_NOT_FOUND = "EPISODE_NOT_FOUND"
    INVALID_REQUEST = "INVALID_REQUEST"
    INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"


ERROR_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.ANALYSIS_JOB_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.EPISODE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.INVALID_REQUEST: status.HTTP_400_BAD_REQUEST,
    ErrorCode.INTERNAL_SERVER_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


ERROR_MESSAGE_MAP: dict[ErrorCode, str] = {
    ErrorCode.ANALYSIS_JOB_NOT_FOUND: "분석 잡을 찾을 수 없습니다.",
    ErrorCode.EPISODE_NOT_FOUND: "회차를 찾을 수 없습니다.",
    ErrorCode.INVALID_REQUEST: "요청 값이 올바르지 않습니다.",
    ErrorCode.INTERNAL_SERVER_ERROR: "서버 내부 오류가 발생했습니다.",
}
