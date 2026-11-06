"""Explicit process settings; never load analysis .env or provider defaults."""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HybridSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHAT_RETRIEVAL_", env_file=None, extra="ignore")
    enabled: bool = False
    database_url: str = Field(default="", repr=False)
    embedding_version: str = Field(default="v1", min_length=1, max_length=50)
    # 2026-10-30 #319: 비통과 사례의 필요 근거 54개 중 26개가 모델 입력에 들어가지 못해
    # 후보·근거·라운드 예산을 넓혔다. 입력 32,000토큰·모델 호출 6회 상한은 유지한다.
    keyword_top_k: int = Field(default=16, ge=1, le=50)
    vector_top_k: int = Field(default=16, ge=1, le=50)
    neighbor_radius: int = Field(default=1, ge=0, le=2)
    # PARTIAL 초안이 인용한 장면의 앞뒤를 2라운드에서 더 넓게 읽는 반경. 회차는 맞췄지만 세부가 빠진 경우용.
    focus_radius: int = Field(default=3, ge=1, le=3)
    # 회차 순서 검색 결과 중 일반 keyword 후보 앞에 둘 최대 개수. 초반 언급이 관련 후보를 모두 밀어내지 않게 한다.
    ordered_top_k: int = Field(default=4, ge=1, le=12)
    # 아래 상한(le)은 인용 등록·결과 계약(한 번에 60개)에 맞춰 넓혔다. 기본값은 그대로다.
    max_neighbors: int = Field(default=8, ge=1, le=60)
    max_evidence: int = Field(default=12, ge=1, le=60)
    per_episode: int = Field(default=4, ge=1, le=60)
    evidence_tokens: int = Field(default=9000, ge=200, le=60000)
    # 3라운드는 앞 라운드가 PARTIAL/NONE이고 남은 시간이 충분할 때만 실행한다.
    max_rounds: int = Field(default=3, ge=1, le=3)
    # 원문 탐색(agent)은 단계마다 모델·임베딩을 더 부르므로 상한(le)만 넓혔다. 기본값은 그대로이며,
    # 탐색을 켤 때 함께 올릴 값은 docs/chat-hybrid-retrieval-v1.md의 표를 따른다.
    max_model_calls: int = Field(default=6, ge=1, le=40)
    max_query_embeddings: int = Field(default=2, ge=0, le=64)

    # --- 모델 주도 원문 탐색(#319). false면 아래 설정은 쓰이지 않고 기존 고정 라운드 그대로다. ---
    # 모델이 읽은 원문을 보고 다음 검색어·통째로 읽을 회차·검색 정렬을 고르는 단계를 반복한다.
    agent: bool = False
    # 탐색 단계(모델에게 다음 행동을 묻는 호출) 최대 횟수. 첫 답이 PARTIAL일 때의 재탐색도 이 안에서 쓴다.
    agent_max_steps: int = Field(default=4, ge=1, le=8)
    # 한 단계에서 실행할 검색어·통째로 읽을 회차 수 상한.
    agent_max_searches: int = Field(default=4, ge=1, le=6)
    agent_max_read_episodes: int = Field(default=5, ge=0, le=5)
    # 읽은 원문 묶음 전체 예산(cl100k 토큰, 조각별 합). 답변·사실 추출 입력에 그대로 들어간다.
    agent_pool_tokens: int = Field(default=80000, ge=2000, le=150000)
    # 한 단계에서 검색 결과·이웃 조각으로 묶음에 더할 수 있는 토큰. 회차 통째 읽기는 묶음 전체 예산만 따른다.
    agent_step_tokens: int = Field(default=14000, ge=800, le=60000)
    # 검색 결과·이웃 조각이 묶음에서 차지할 수 있는 최대 비율. 나머지는 회차 통째 읽기 몫으로 남겨
    # 검색 조각이 묶음을 다 채워 핵심 회차를 통째로 읽지 못하는 일을 막는다.
    agent_search_share: float = Field(default=0.6, ge=0.1, le=1.0)
    # 검색어 하나당 키워드·벡터 각각에서 새로 가져올 조각 수.
    agent_keyword_top_k: int = Field(default=6, ge=1, le=30)
    agent_vector_top_k: int = Field(default=4, ge=1, le=30)
    # 탐색 호출의 출력 상한(추론 토큰 포함)과, 답변 호출 몫으로 남겨 둘 시간(초).
    agent_step_output_tokens: int = Field(default=3000, ge=256, le=8000)
    agent_answer_reserve_seconds: float = Field(default=75, ge=5, le=240)
    # 탐색에 쓸 시간 상한(초). 지나면 새 탐색 단계를 시작하지 않고 답변으로 넘어간다. 0이면 제한 없음.
    agent_explore_seconds: float = Field(default=45, ge=0, le=240)
    # 답변 시도 최대 횟수. 첫 답이 PARTIAL/NONE이면 빠진 부분을 겨냥해 다시 탐색한 뒤 답하고, 인용 검증에
    # 실패해 답을 버린 경우(자료 부족이 아님)는 같은 자료로 다시 답한다. 모두 이 횟수 안에서만 한다.
    agent_max_answers: int = Field(default=3, ge=1, le=3)
    # 항목별 질문(perItem)도 탐색으로 처리한다. false면 그 질문은 기존 항목별 고정 라운드 그대로다.
    agent_items: bool = True
    # 계획이 질문에 적힌 회차(예: 'N화에서')로 출처 범위를 좁혔을 때, 탐색은 서버가 이 질문에 허용한
    # 원래 범위 전체에서 하고 좁힌 범위는 먼저 읽을 곳으로만 쓴다. 등록 이력 조회의 회차 필터는 그대로다.
    agent_relax_plan_range: bool = True
    # 계획이 등록 이력만으로 답하기로 했고(rdbMode=HISTORY 계열, sourceNeed=NONE) 등록 답이 나왔어도
    # 원문 탐색으로 보완한다. 원문 답이 나오지 않으면 등록 답을 그대로 돌려준다.
    agent_verify_registered: bool = True
    # --- 답변을 '사실 추출 → 답 작성' 두 단계로 나눈다(탐색의 일반 답변에만 적용). ---
    # false면 읽은 원문 묶음을 한 번의 답변 호출에 넣는다. 항목별(perItem) 답변은 이 값과 무관하게 한 단계다.
    agent_extract: bool = True
    # 추출 한 묶음에 넣을 원문 분량(cl100k 토큰)과 병렬로 돌릴 묶음 수 상한. 묶음은 회차 순서대로 채운다.
    agent_extract_group_tokens: int = Field(default=10000, ge=1000, le=60000)
    agent_extract_max_groups: int = Field(default=6, ge=1, le=12)
    # 추출 호출의 출력 상한(추론 토큰 포함).
    agent_extract_output_tokens: int = Field(default=8000, ge=512, le=16000)

    @model_validator(mode="after")
    def compatible(self):
        if self.per_episode > self.max_evidence:
            raise ValueError("Per-episode limit exceeds evidence cap")
        if not self.embedding_version.strip():
            raise ValueError("Embedding version is required")
        return self
