# 원문 검색 준비 v1 — GH239

## 완료 범위와 다음 연결

현재 회차의 S3 원문을 재업로드·설정 재추출 없이 검색용 청크로 만들고,
필요한 벡터만 제한된 운영 실행으로 생성한다. #258 이후 운영 자동화는 AI 저장소의 `docs/chat-runtime-operations.md`를 따른다. 아래 `status → dry-run → execute → status`는 단건 CLI 점검/수동 복구 경로다.
Spring의 새 운영 백필 접수에는 `CHAT_OPERATIONS_ENABLED=true`와 별도 32자 이상 실행 secret이 필요하다.
공개 `CHAT_ENABLED`는 false로 유지할 수 있고 이 단계의 `CHAT_PYTHON_URL`은 필요하지 않다.
내부 키로 `GET /api/internal/v1/chat-runs/operations/readiness`를 호출해 CommonResponse `data`의
`operationsEnabled=true`, `capabilityConfigured=true`를 확인한다. HTTP 200만으로 판단하지 않는다.
이 GET은 DB·S3·provider를 호출하거나 운영 실행을 생성하지 않으므로 실제 저장소 준비 검증은 기존 status/dry-run이 맡는다.
기존 #238의 RDB 질의응답과 직접 근거 조회는 이 준비 상태에 의존하지 않는다.
질문 벡터·키워드 검색·랭킹·주변 문단 확장·답변 합성은 #240이다. 이 변경만 배포해서
원문 질문에 답하는 것은 아니다. 실제 운영 전체 백필과 검색 품질 측정은 아직 하지 않았다.

## 코드·출처 조사 결과

- 기존 `episode_chunks`는 설정 추출용으로 목표 6,000자/최대 7,000자/최소 1,000자, 겹침 없음이다.
  재분석은 이를 삭제하고 새 UUID를 만든다. 후보의 `source_chunk_id`가 연결되므로 검색 정책으로 바꾸지 않는다.
- `episodes`에는 현재 `content_s3_key`, nullable `content_s3_version`, `content_hash`가 있다.
  원문 저장마다 고유 key를 만들며 UTF-8 SHA-256을 저장한다. S3 version이 없더라도 고유 key와 hash로 확인한다.
- 수정·교체·삭제는 작품 잠금 안에서 원문 상태와 `episode_source_purge_requests`를 갱신한다.
  S3 삭제가 지연되면 청크가 남을 수 있다. 요청 행이 존재하는 동안 검색 준비와 조회를 모두 거절한다.
- 설정집의 편집 텍스트는 존재하지만 회차와 같은 hash/version 계약이 없다. #221도 구현 완료를 전제하지 않는다.
  v1은 `CURRENT_EPISODE`만 지원하며 `SETTING_BOOK`, `PAST_EPISODE_VERSION`은 미지원이다.
  설정집 수는 작품 단위로 별도 집계한다. 과거 버전의 총수는 알 수 없어 null이다.
- 회차 번호는 출처의 현재 분류/검색 범위이며 소설 속 사건 시점이나 특정 시점 세계 상태를 뜻하지 않는다.

## 저장·유효성 계약

Java Flyway V76이 `retrieval_sources`, `retrieval_chunks`, `retrieval_current_chunks` view를 만든다.
Python `app/retrieval/models.py`는 공유 스키마의 매핑이다. Java의 `domain/retrieval/entity`는
원문 생명주기용 메타데이터만 매핑한다. vector(1536)은 기존 분석 청크처럼 Python/pgvector가 소유한다.
운영 Python에서 `create_all`은 호출하지 않는다.

| 단위 | 저장값과 역할 |
| --- | --- |
| source | episode/work ID, 실제 key/version/hash, chunk policy, 전체 청크 수, 준비 시각 |
| chunk | 별도 UUID/순서, 원문 그대로의 slice, Unicode code point 시작·끝, UTF-8 hash, 실제 token count |
| embedding | vector(1536), 실제 모델/버전, 생성 시각, MISSING/FAILED/READY, 고정 failure code |
| 시도 | 청크별 호출 직전 누적 attempt_count와 마지막 operational run ID; 실제 호출별 원장은 ai_token_usages |

정책은 `cl100k-800-100-boundary-v1`: `cl100k_base`로 최대 800토큰,
최대 약 100토큰 겹침, 문단/문장 경계를 우선한다. 경계가 너무 이르면 크기를 유지하고,
한 문장이 큰 경우 원문 문자 경계에서 잘라 **매번 실제 slice를 다시 토큰화**한다.
토큰 바이트를 decode해 원문을 재조립하지 않으므로 한글/emoji가 깨지지 않는다.
공백/CRLF는 보존하며 공백만으로 된 조각은 임베딩하지 않는다. 경계 때문에 실제 길이/겹침은 가변이다.
토크나이저 캐시는 Docker 이미지에 포함하며 API 호출 없이 센다.

동일 source/policy/내용이면 기존 청크 ID와 완료 벡터를 유지한다. 원문/정책/청크 내용이 달라지면
작품 잠금 아래 검색 청크만 교체한다. 기존 분석 청크나 후보/현재 설정을 건드리지 않는다.
같은 원문의 재분석은 검색 벡터 재생성 사유가 아니다.

`retrieval_current_chunks`는 현재 source key/hash/version 일치, 회원·작품 ACTIVE,
회차 비보관, 파기 요청 없음, READY+vector 존재를 모두 요구한다.
**소비자는 policy/model/version과 서버에서 확정한 work/회차 범위를 추가로 적용해야 한다.**
`RetrievalStore.valid_chunks()`는 이 필터와 chunk hash 확인을 적용한 최대 100개 읽기 계약이다.
LLM이나 외부 사용자에게 직접 노출한 DB 조회 API가 아니다. #240은 모든 검색/이웃 조회/최종 인용 경로에서
동일 조건을 적용하고 답변 전달 전 재검증한다. 예전 `EpisodeChunkVectorSearchService`를
그대로 챗봇에 붙이면 이 유효성 계약을 충족하지 않는다.

## 준비 상태와 dry-run

`status`는 DB만 읽으며 source별 상태/대상 수/READY·MISSING·FAILED·MODEL_VERSION_MISMATCH·CHUNK_MISMATCH 수를 반환한다.
원문/정책이 바뀐 저장물은 `SOURCE_POLICY_MISMATCH`, 미준비는 `UNPREPARED`이며 새 대상 수는 null이다.
`PURGE_PENDING`, `SOURCE_UNIDENTIFIED`, `UNAVAILABLE`을 빈 결과로 바꾸지 않는다.
보관 tombstone은 일반 페이지에서 제외하고 수를 따로 집계한다. 명시적으로 그 episode ID를 지정하면 ARCHIVED다.
`hasMoreSources`가 true면 작품 전체를 확인한 것이 아니다. `supportedScopeReady`는 선택한 지원 범위만 의미한다.

`dry-run`은 지정 범위의 S3 원문을 제한적으로 읽고 hash를 확인한 다음 실제 token 수/청크 수를 계산한다.
DB를 쓰거나 embedding client/사용량 예약을 생성하지 않는다. S3 읽기 자체는 실제 저장소 접근이다.
원문 확인 실패는 `SOURCE_UNREADABLE`/`SOURCE_CHANGED`와 null 입력량으로 남기며 전체 비용도 null이다.
단가를 입력하지 않아도 비용은 null이다. `knownInputTokens`만으로 미확인 항목을 0원으로 계산하지 않는다.
`estimatedCostUsd`는 선택된 source 페이지의 **미생성 청크 전체·첫 시도·입력 토큰 비용 추정**이다.
재시도/DB·S3 비용은 별도이며 execution cap보다 대상이 크면 `limited=true`다.

## 실행 상한·재개·사용량

| 상한 | 기본 / 허용 최대 |
| --- | --- |
| source 페이지 | 20 / 100, 한 작품과 명시한 회차 범위 |
| 처리 청크 | 100 / 1,000, 성공·실패 포함 고유 대상 수 |
| 누적 입력량 | 100,000 / 1,000,000토큰, 재시도와 사용량 미확인 시도도 예산에 포함 |
| batch | 8 / 32 |
| provider 동시성 | 1, 같은 작품의 겹친 운영 실행은 PostgreSQL advisory lock으로 거절 |
| 추가 재시도 | 1 / 2, 일시 provider 오류만; 인증/계약/DB/원장 오류는 중단 |
| 시간 | 900 / 3,600초, 새 작업/호출 허용 한도와 진행 중 provider timeout; 동기 저장소/DB 취소의 hard deadline은 아님 |
| 원문 읽기 | 회차당 2MB 및 250,000 code point |
| 금액 | `--usd-per-million`과 `--max-cost-usd`를 함께 지정하면 입력량 기반 추정액으로 호출 전 제한 |

provider 호출 전에 source와 chunk를 다시 검증하고, 원장 예약 뒤 호출 직전에도 검증한다.
호출이 시작되지 않았다면 RELEASE다. 성공 응답을 저장할 때 같은 작품 row 잠금을 잡고 다시 비교한다.
원문 변경·삭제·청크 교체라면 성공으로 저장하지 않는다. 외부 호출 중에는 DB row transaction을 유지하지 않는다.
운영 실행의 advisory lock은 별도 연결 1개를 유지하며 DB 작업까지 **최대 연결 2개**다.

실제 호출마다 새 operational run/call ID를 사용하며 Spring의 actor/work/policy/예약량 계약을 재사용한다.
정책은 `RETRIEVAL_INDEX_SERVICE_PAID_V1`, 목적은 `OPERATIONAL_BACKFILL`이다.
**기존 회차와 신규 원문 검색 준비 모두 사용자 계정 예약·차감 없음.** 실제 provider 입력량과 성공/실패는 원장에 남긴다.
요청 최대 예약은 기존 UTF-8 byte 기반 보수적 상한이다. 모델 응답에 usage가 없으면 UNKNOWN이며 0이 아니다.
청크의 attempt_count는 호출 직전 기록이므로 프로세스 종료 경계에서 실제 HTTP 전송 여부가 불명확할 수 있다.
CLI의 providerAttempts는 그 프로세스에서 실제 delegate를 호출한 횟수다.

SQLite outbox는 계량/실행 보고만 저장하고 원문·벡터는 저장하지 않는다. 실행 시작 시 미전송 보고를 먼저 보낸다.
아직 남으면 새 임베딩을 시작하지 않는다. 같은 outbox 파일은 OS file lock으로 동시 사용을 막는다.
보고 성공/폐기/기존 TTL 만료 시 행을 삭제한다. 생성 OFF에서도 `recover-reports`는 사용할 수 있다.
기본 사용량 복구 유효기간은 기존 실행 계약의 deadline+24시간이다. 볼륨을 보존한다.

성공 벡터는 PostgreSQL에 있어 프로세스가 바뀌어도 같은 범위 재실행 시 건너뛴다.
DB 저장 실패/강제 종료 뒤 미완료 벡터는 다시 생성 대상이다. **provider 응답 후 DB 저장 전 종료 시
외부 API 호출은 중복될 수 있다.** DB 멱등성과 provider exactly-once를 혼동하지 않는다.
새 운영 명령은 새 비용 상한을 받는다. 같은 명령을 무한 재시작하는 스케줄은 이 구현에 없다.

## 신규 원고·수정·삭제 연결

1. 기존 분석 Worker에서 `RETRIEVAL_PREPARATION_ENABLED=true`이면 청크 stage(재시작 포함)에
   현재 원문의 검색 청크를 준비한다. 실패하면 분석을 계속하며 준비 누락은 운영 실행으로 복구한다.
2. 이 단계에는 provider 호출/사용자 과금이 없다. 분석 완료를 기다리지 않고 업로드된 원문을
   `prepare`/`execute`로 준비하는 것도 가능하다.
3. 운영 실행기의 `EMBEDDING_GENERATION_ENABLED=true`에서 준비된 MISSING/FAILED/불일치 벡터를 생성한다.
   신규 데이터도 같은 실행 경로를 사용한다. **분석 Worker가 검색 임베딩을 자동 호출하는 것은 아니다.**
   상시 소비 스케줄은 실제 배포 시 운영 예산과 실행 범위에 맞춰 별도로 정한다.
4. 기존 분석 Worker의 `EMBEDDING_GENERATION_ENABLED`는 이전 6,000자 청크 임베딩/분석 원장 경로를 유지한다.
   검색용 생성기를 켜려고 분석 Worker의 이 값을 함께 켜지 않는다.
5. 수정/삭제/교체 중에는 view와 저장 검증에서 즉시 제외한다. Spring의 기존 회차/작품 purge가
   새 검색 텍스트·벡터·manifest까지 물리 삭제한다. 정리가 끝난 새 원문은 다음 prepare/execute 대상이다.

## 운영 명령과 배포 순서

Java V76/파기 코드 → AI #238 기반과 이 변경 순으로 배포한다. 현재 생성/준비 기본값은 모두 OFF다.
검색 실행기는 기존 분석 5개/해소 2개/챗봇 HTTP 프로세스와 별도의 one-shot 프로세스다.
#258 자동 배포가 별도 systemd timer를 설치하며 기본 command는 `app.retrieval.backfill run`이다. 유료 생성 기본 OFF와 명시적 전체 예산을 유지하고 포트를 열지 않는다. 기존 단건 CLI는 command override로 사용할 수 있다.

운영 전 실제 work ID/회차 범위와 단가를 확인한다. 예시 ID는 교체해야 한다.
환경변수는 프로세스에 명시적으로 주입하며 CLI는 로컬 `.env`를 읽지 않는다.
S3 IAM Role/기존 설정, Spring 내부 API key와 CHAT_OPERATIONS_ENABLED/실행 서명 설정이 필요하다.
단가 예시 0.02 USD/1M은 구현 시점의 text-embedding-3-small 일반 입력 단가이며 실행 전에 확인한다.
출처: https://developers.openai.com/api/docs/models/text-embedding-3-small

```bash
python -m app.retrieval.cli status --work-id 00000000-0000-0000-0000-000000000001 --episode-from 1 --episode-to 20
python -m app.retrieval.cli dry-run --work-id 00000000-0000-0000-0000-000000000001 --episode-from 1 --episode-to 20 --usd-per-million 0.02
EMBEDDING_GENERATION_ENABLED=true python -m app.retrieval.cli execute --work-id 00000000-0000-0000-0000-000000000001 --episode-from 1 --episode-to 20 --actor operator-name --max-chunks 100 --max-input-tokens 100000 --batch-size 8 --max-retries 1 --usd-per-million 0.02 --max-cost-usd 0.01 --outbox /var/lib/catchhole-chat/retrieval.sqlite
python -m app.retrieval.cli recover-reports --outbox /var/lib/catchhole-chat/retrieval.sqlite
```

Compose에서는 `--env-file deploy/retrieval.env -f deploy/compose.retrieval.prod.yml run --rm retrieval-index`
뒤에 위 `python -m ...` 명령을 붙인다. 실제 env는 커밋하지 않는다. 생성 flag는 이 Compose에만
`RETRIEVAL_EMBEDDING_GENERATION_ENABLED=true`로 전달한다.

기존 애플리케이션 27개 + 챗봇 검색 2개 + 백필 2개 = 정상 동시 운영 31개다.
배포 preflight 연결 1개까지 32개이며 관리/다른 앱의 여유는 별도다. 기존의 분석 Worker 1개 drain 계산은 챗봇 pool을 제외했으므로 동시 운영 계획에 사용하지 않는다.
실제 RDS 여유를 확인해 명시한 전체 연결 예산 안에서만 배포하며 워커 수/pool을 자동 변경하지 않는다.

CHUNK_LIMIT/BUDGET_EXHAUSTED/TIME_LIMIT이면 같은 범위로 재실행한다. 해당 범위가 준비되었는지 확인한 뒤
다음 회차 범위로 이동한다. `nextEpisodeFrom`은 source 페이지 이동 안내이며 **미완료 청크를 건너뛰라는 뜻이 아니다.**
400개 회차를 하나의 무제한 요청으로 실행하지 않는다. SIGINT/종료 시 미완료 대상은 다음 실행에서 다시 판정한다.
복구 시 생성 OFF → 미전송 보고 복구 → status/dry-run → 필요한 범위만 다시 ON 순서를 사용한다.
AI만 롤백하면 새 테이블은 남겨두고 두 flag를 OFF로 한다. 이미 적용된 Flyway를 되돌리거나
파기 코드만 제거하지 않는다. 민감한 파생 원문 삭제 경로를 계속 유지해야 한다.

## 검증과 한계

합성 원고/가짜 provider로 토큰·원문 offset·중단/재개·예산·usage·실패 분기·OFF를 검증한다.
Python PostgreSQL 테스트는 이름이 `gh239_test`인 명시적 localhost DB의 매번 새 schema에서 Java 전체 SQL을 적용한다.
Java Testcontainers는 빈 pgvector PostgreSQL에 실제 Flyway 전체 적용 후 JPA validate, 원문 변경/파기와 사용자 비과금을 검증한다.
기존 Spring↔Python 채팅 HTTP 흐름도 회귀 검증한다. 운영 DB/개인 원고/유료 임베딩·LLM·judge는 사용하지 않는다.

800/100은 초기 정책이며 검색 정확도 최적값으로 검증된 수치가 아니다. #240에서 질문/정답 근거에 대한 검색·인용 품질을
측정해야 한다. 내부 합성 응답으로 실제 embedding 모델의 검색 품질을 측정했다고 보고하지 않는다.
실행 결과·정확한 검증 커밋과 수치는 AI 로직 v0008 상세 기록에 둔다.
