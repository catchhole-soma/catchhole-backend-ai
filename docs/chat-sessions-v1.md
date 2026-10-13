# 일회성 작품 대화와 후속 질문 (#241)

## 제품 정책

대화는 작품 소유자가 명시적으로 종료하거나 마지막 질문 접수/답변 완료 후 2시간이 지나면 이용할 수 없다. 읽기, 새로고침, 화면 이동, 탭 닫힘은 만료 시간을 바꾸지 않는다. 실패 질문의 명시적 재시도는 새 질문 실행이므로 시간을 갱신한다. 빈 대화는 생성 후 2시간이다.

종료 트랜잭션에서 질문·최종 답변·인용 receipt를 지운다. 만료는 모든 API에서 즉시 차단하고, 본문 정리는 60초 간격 작업이 최대 100개씩 처리한다. 정리 실패는 다음 주기에 재시도한다. 앱 중단 중 지난 만료도 기동 후 정리된다. 스케줄이 지연되면 물리적 본문 삭제도 지연되지만 조회/후속 문맥에는 쓰이지 않는다.

대화 ID·멱등 키·시각·실행 상태·사용량 같은 메타데이터는 남긴다. 대화 종료가 이미 발생한 provider 비용 정산을 취소하지 않는다. 대화 종료 후 새 예약/도구 호출/결과 게시는 거절하지만 이미 예약한 호출의 늦은 정산은 받는다. 일반 종료와 작품/회원 영구 파기는 구분한다. 후자는 기존 파기 그래프에서 세션 행까지 삭제한다. 이 정책은 서버 원장·로그 보존 정책을 대체하지 않는다.

## 소유 경계와 데이터

- Java V77: `chat_sessions` 추가. 기존 `chat_questions`에 nullable `session_id`, `sequence_no`, `submitted_at` 추가. 답변은 기존 `chat_runs.result_json`, 사용량은 기존 원장을 재사용한다. 별도 메시지/답변 복사 테이블을 만들지 않는다.
- 세션 없는 기존 질문 요청은 그대로 동작한다. null `sessionId`는 request hash JSON에서 생략해 기존 멱등 요청과 호환된다.
- 작품 행 잠금 아래 권한·열림·진행 질문 여부·순번 할당을 함께 처리한다. 같은 대화에 다른 탭이 동시에 보내도 한 실행만 접수한다. 생성 요청의 UUID `requestId`, 질문의 `Idempotency-Key`는 응답 유실 시 같은 값을 재사용한다.
- 초기 운영 상한: 회원당 활성 대화 20개, 대화당 질문 200개. 화면 목록은 20개씩 페이지 이동한다. 이 페이지 크기는 모델의 Fact 조회 상한과 별개다.
- 원문 변경/삭제가 기존 `invalidateWork`에 도달하면 해당 작품의 활성 대화를 모두 종료하고 본문을 지운다. 직접 입력한 설정 자체를 삭제하지 않는다. 영향받은 대화만 남기는 정밀한 의존 추적은 후속 최적화다.

## API

모든 사용자 API는 인증·작품 소유권·활성 회원/작품 검사와 `CommonResponse`를 사용한다.

| API | 동작 |
| --- | --- |
| `POST /api/v1/works/{workId}/chat-sessions` | `{requestId: UUID}`로 멱등 생성 |
| `GET .../chat-sessions?page=0&size=20` | 열린 대화만 생성 시각/ID 오름차순 |
| `GET .../chat-sessions/{sessionId}` | 만료 시각·질문 수 조회, TTL 갱신 없음 |
| `GET .../chat-sessions/{sessionId}/messages?page=0&size=20` | 접수 순번 오름차순, 현재 실행 결과/사용량 연결 |
| `DELETE .../chat-sessions/{sessionId}` | 반복해도 안전한 종료·본문 삭제 |
| 기존 `POST .../chat-questions` | optional `sessionId` 추가 |

목록은 `PageResponse`, size 1~50. 닫힌 세션은 410 `CHAT_SESSION_CLOSED`, 진행 중 질문은 409 `CHAT_SESSION_BUSY`, 세션/질문 수 상한은 409 `CHAT_SESSION_LIMIT`. `CHAT_ENABLED=false`는 신규 생성·질문 접수를 막으며 기존 정리/정산은 계속한다.

## 문맥과 사실의 구분

Spring만 같은 세션의 앞선 완료 질문/답변에서 문맥을 만든다. 클라이언트가 과거 답변·회원 ID·작품 ID를 모델 문맥에 주입하지 않는다. 최근 질문 7개를 스캔해 최대 6개 성공 턴, 답변당 최대 1,000 UTF-16 단위, 질문+답변 합계 8,000 UTF-16 단위까지 보낸다. 이모지 surrogate 쌍을 쪼개지 않는다. 실패/무효 결과는 제외한다. 오래된 성공 턴을 찾으려고 실패 질문을 끝까지 훑지 않는다.

Python `ExecutionContext.conversation`/`conversationTruncated`는 additive 필드다. 과거 출력은 지시나 사실 근거가 아니라 지칭 대상 해석에만 쓰는 비신뢰 데이터다. 현재 질문과 이 문맥에 실제 등장한 이름/검색어만 허용하고, 실제 RDB 대상 탐색으로 재확인한다. 같은 이름의 복수 대상이나 불명확한 대명사는 추가 질문으로 남긴다. 예전 회차 제한은 새 질문에 자동 계승하지 않는다.

분류 입력이 토큰 상한을 넘으면 오래된 문맥부터 제외한다. 문맥 생략은 답변 `limitations`에 안내하며 유료 요약 호출을 추가하지 않는다. 프롬프트 버전은 QA plan/select 및 hybrid plan/answer 모두 v2다. 답변에 선택하는 R*/E*는 이번 실행이 다시 읽은 데이터만 가리킨다. 기존 RDB·직접 근거·키워드/인접·벡터 정책과 모델 호출 예산을 재사용한다.

예: “화염술의 소모 마력은?” → 현재 등록값 35. 사용자가 값을 42로 수정 → “그 마법의 소모 마력은?” → 문맥에서 화염술을 해석 → `FIND_WORLD_SETTINGS` → `WORLD_CURRENT` 재조회 → 42. 이전 35는 최신값 증거로 사용하지 않는다.

## 화면

`/chat?workId=...&sessionId=...&messagePage=...`에서 활성 대화를 복원한다. URL에는 식별자/페이지만 넣고 본문·임시 입력을 브라우저 저장소에 보관하지 않는다. React Query 캐시는 인증 세대·작품·세션별로 분리하고 이탈 시 취소/제거한다. 늦은 mutation 응답은 이전 화면을 복원하지 않는다.

서버의 저장 상태를 조회해 질문/답변을 하나의 턴으로 표시한다. FastAPI는 기존 별도 HTTP 역할이며 분석 Job 큐에 질문을 넣지 않는다. 이번 화면은 완료 상태를 2초 간격으로 확인한다. 토큰 단위 스트리밍은 제공하지 않는다. 실패한 질문은 명시적으로 재시도하고, 접수 응답이 유실된 질문은 입력을 보존한 채 같은 키로 접수를 확인한다. 한글 IME Enter는 전송하지 않는다.

근거 창은 현재 질문을 다시 확인하고 직접 근거 도구를 호출한 뒤 실행이 같은지 재확인한다. 검증된 원문 인용, 직접 입력, 과거 이력, 원문 확인 불가 상태를 구분한다. 설정 상세와 현재 원문 화면으로 연결한다. 변경/삭제로 세션을 읽을 수 없으면 본문과 열린 근거 창을 숨긴다.

## 검증과 남은 범위

- Java: H2/실제 PostgreSQL V1~V77+JPA validate에서 생성/멱등성/페이지/TTL/동시 전송/권한/문맥/정산/본문 삭제/원문 무효화/작품 파기 재시도 검증.
- AI: 소켓 차단 합성 테스트에서 잘못된 이전 답변보다 새 RDB 결과 우선, 모호성, 관측되지 않은 이름 거절, 회차 범위, 문맥 상한, 과거 E* 재사용 거절, 기존 wire 호환 검증.
- Front: mock E2E와 별도 opt-in `e2e/chat-live.spec.ts`를 구분한다. 후자는 실제 브라우저·Spring·PostgreSQL·Python QA/hybrid를 연결하고 모델 출력만 고정한다. 실제 LLM/임베딩/judge API는 호출하지 않는다.
- 실제 모델의 대명사/생략 해석 정확도, 의미상 근거 지지, 비용/지연, 세 검색 방식 비교는 #242에서 평가한다. 전체 Fact 이력 누락, 의미 경로 좁히기 및 문맥 상한 최적화는 #247. 미지원 설정집/과거 전체 상태 복원은 지원한다고 표시하지 않는다.
- 적용 순서: Java V77/API → AI v2 문맥 처리 → Front 생성 SDK/화면. 기존 세 플래그 기본 OFF를 유지하며 운영 배포·백필·활성화는 이번 작업에 포함하지 않는다.

## 로컬 연결 재현

1. 일회용 `pgvector/pgvector:0.8.2-pg16` 컨테이너에 `gh241_test` DB를 만들고 localhost 임의 포트에만 연결한다. 공유/운영 DB를 사용하지 않는다.
2. Java를 `e2e` profile, 명시적 테스트 DB URL, `spring.config.import=`(개인 .env 제외), `storage.local.root`로 기동한다. 로컬에서만 `CHAT_ENABLED=true`, `CHAT_CAPABILITY_SECRET`(32자 이상), `INTERNAL_API_KEY`, `CHAT_PYTHON_URL=http://127.0.0.1:19091`, Front origin CORS를 지정한다. 기존 세 설정 파일의 기본 OFF는 바꾸지 않는다.
3. AI checkout에서 `tests/support/prepare_chat_conversation.py --database-url postgresql://.../gh241_test --api-url http://127.0.0.1:18091 --storage-root <동일 경로> --output <저장소 밖 fixture.json>`을 실행한다. 공개된 local JWT secret으로 합성 계정만 만들며 output은 0600이다.
4. AI를 `CHAT_TEST_ONLY=true`, `CHAT_TEST_PORT=19091`, `SPRING_INTERNAL_API_KEY`, `SPRING_INTERNAL_API_BASE_URL`, `CHAT_OUTBOX_PATH`, `GH241_TEST_DATABASE_URL=postgresql+psycopg://.../gh241_test`, `PYTHONPATH=.`로 `tests/support/chat_conversation_stub_server.py`에서 기동한다. 실제 provider key는 읽지 않으며 텍스트 모델 출력만 고정한다. 장면 경로는 실제 읽기 전용 PostgreSQL 검색과 Spring 원문 검증을 사용한다.
5. Front에서 `VITE_API_BASE_URL=http://127.0.0.1:18091 CATCHHOLE_E2E_PORT=5179 CATCHHOLE_CHAT_LIVE_FIXTURE=<fixture.json> npx playwright test e2e/chat-live.spec.ts`를 실행한다. 다른 opt-in live 테스트에 이 합성 계정을 재사용하지 않는다.
6. 질문·답변 삭제와 `ai_token_usages` 정산 상태를 테스트 DB에서 대조한다. 테스트 서버/컨테이너를 종료하고 합성 storage/outbox/token fixture를 정리한다. 실제 환경 접속이나 운영 배포는 이 절차에 없다.

2026-10-13 검증: Java 전체+bootJar 1,758 통과/24 skip; Python 전체(외부 DB env 미주입) 2,440 통과/50 skip; Front 전체 524 통과/12 opt-in skip. 별도 챗봇 실제 연결 1개 통과: 35→42 최신값 재조회, 원문 장면 검색/인용 확인/현재 원문 링크, 재진입/중복 없음/종료. 세 번의 텍스트 QA에 6건의 합성 provider 사용량이 정산되고 종료 후 질문·답변·인용 본문은 0건이다. 실제 모델 정확도/비용/지연은 미측정이다. 최종 화면 스크롤 보완 후 챗봇 10개(PC/320px 명시적 재시도 포함)와 실제 연결 1개를 재검증했다. Java는 실제 회차 삭제 이벤트·회원 탈퇴를 추가해 세션 H2 15개와 PostgreSQL 15개를 마지막으로 통과했다. 전체 테스트 수에 이 부분집합을 더하지 않는다.
