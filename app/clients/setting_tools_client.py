"""Spring 소유권/현재값/근거 정책을 호출하는 클라이언트. DB/LLM/Worker lease와 독립적이다."""

from dataclasses import dataclass
from uuid import UUID

import httpx
from pydantic import ValidationError

from app.schemas.setting_tools import SettingToolQuery, SettingToolResponse


@dataclass(frozen=True)
class SettingToolContext:
    """인증한 서버가 생성한다. LLM tool arguments에서 생성하지 않는다 (#237 연결 지점)."""

    member_id: int
    work_id: UUID

    def __post_init__(self) -> None:
        if (
            type(self.member_id) is not int
            or self.member_id < 1
            or not isinstance(self.work_id, UUID)
        ):
            raise ValueError("인증된 설정 조회 범위가 필요합니다.")


class SettingToolClientError(RuntimeError):
    def __init__(self, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        # 원문, 서버 body, 인증 헤더, URL을 예외/로그로 전파하지 않는다.
        super().__init__(f"설정 조회 실패: {code}")


class SettingToolsClient:
    def __init__(
        self,
        base_url: str,
        internal_api_key: str,
        context: SettingToolContext,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = internal_api_key
        self._context = context
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=15)

    async def query(self, request: SettingToolQuery) -> SettingToolResponse:
        # model_construct 등 검증 우회를 호출 경계에서 다시 차단한다.
        request = SettingToolQuery.model_validate(request.model_dump())
        try:
            response = await self._http.post(
                f"{self._base_url}/api/internal/v1/works/{self._context.work_id}/setting-tools/queries",
                headers={
                    "X-Internal-Api-Key": self._api_key,
                    "X-Setting-Tool-Member-Id": str(self._context.member_id),
                },
                json=request.model_dump(by_alias=True, mode="json"),
                timeout=15,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise SettingToolClientError("TRANSPORT_ERROR") from None
        if response.status_code != 200:
            raise SettingToolClientError("HTTP_ERROR", response.status_code)
        try:
            envelope = response.json()
            if envelope.get("success") is not True:
                raise ValueError("실패 응답")
            result = SettingToolResponse.model_validate(envelope["data"])
            if (
                result.tool != request.tool
                or result.items.page != request.page
                or result.items.size != request.size
            ):
                raise ValueError("조회 계약 불일치")
            return result
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
            raise SettingToolClientError("CONTRACT_ERROR") from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()
