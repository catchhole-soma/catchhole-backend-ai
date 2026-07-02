import httpx

from app.core.config import Settings, get_settings
from app.llm.responses import LlmTextResponse


# "OpenAI Responses API 호출만" 담당하는 client
class OpenAIResponsesClient:
    def __init__(
        self,
        api_key: str, #OpenAi 키
        model: str, # 기본 모델명
        responses_api_url: str, # OpenAI Responses API 주소
        http_client: httpx.Client | None = None, #실제 HTTP 요청 도구
    ) -> None:
        self.api_key = api_key
        # 기본 모델명, 호출할 때 model을 따로 넘기면 그 값이 우선
        self.model = model
        # 기본값은 https://api.openai.com/v1/responses, 테스트에서는 fake URL을 넣을 수 있음
        self.responses_api_url = responses_api_url
        # 실제 HTTP 요청을 보내는 도구, 테스트에서는 MockTransport가 들어간 client를 주입
        self.http_client = http_client or httpx.Client(timeout=60)

    # .env에서 읽은 설정값으로 client를 만드는 생성 보조 함수
    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "OpenAIResponsesClient":
        settings = settings or get_settings()
        return cls(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            responses_api_url=settings.openai_responses_api_url,
        )

    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
    ) -> LlmTextResponse:
        if not self.api_key:
            raise ValueError("LLM_API_KEY is required.")

        # 실제 OpenAi API 요청을 보내는 부분
        # system_prompt는 역할/규칙, user_prompt는 실제 청크 입력을 담음
        response = self.http_client.post(
            self.responses_api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                # 호출별 model이 있으면 그걸 쓰고, 없으면 Settings의 기본 모델을 쓴다.
                "model": model or self.model,
                "input": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": system_prompt}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_prompt}],
                    },
                ],
                "max_output_tokens": max_output_tokens,
            },
        )
        # 4xx/5xx 응답이면 httpx.HTTPStatusError를 발생
        response.raise_for_status()
        # OpenAI 응답 JSON을 dict로 바꾼 뒤, 필요한 text와 token usage만 내부 schema로 정리
        payload = response.json()
        usage = payload.get("usage") or {}
        return LlmTextResponse(
            text=self._extract_output_text(payload),
            input_token_count=usage.get("input_tokens"),
            output_token_count=usage.get("output_tokens"),
            raw_response=payload,
        )

    def _extract_output_text(self, payload: dict) -> str:
        # Responses API가 output_text를 바로 주는 경우 먼저 사용
        if payload.get("output_text"):
            return payload["output_text"]

        # 일부 응답 형태는 output[].content[] 안에 output_text가 들어올 수 있어서 fallback으로 처리
        output_texts: list[str] = []
        for output_item in payload.get("output", []):
            for content in output_item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    output_texts.append(content["text"])

        return "\n".join(output_texts).strip()
