# CatchHole AI Docs

Python AI Worker의 문서 위치와 작성 기준을 정리합니다.

프로젝트 전체 ERD, 사용자-facing API, Spring 도메인 정책은 `catchhole-backend-java/docs/`를 기준으로 관리합니다. 이 저장소의 `docs/`는 Python Worker 내부 실행 흐름처럼 Python 코드만 보고는 이해하기 어려운 cross-package 흐름을 설명합니다.

## 문서 목록

| 문서 | 내용 |
| --- | --- |
| [AI Worker Workflow](ai-worker-workflow.md) | Spring claim 이후 Python Worker가 청킹, LLM 추출, 후보 저장, 완료/실패 보고를 수행하는 흐름 |
| [FastAPI Role Review](fastapi-role-review.md) | FastAPI를 health check 수준으로 유지할지, Worker-only 구조로 제거할지 검토하는 기준 |

## Java Docs와의 책임 구분

| 구분 | 위치 | 예시 |
| --- | --- | --- |
| 프로젝트 전체 기준 | `catchhole-backend-java/docs/` | ERD, 업로드 전체 흐름, 분석 job 생성 API, 사용자-facing API, 도메인 정책 |
| Python Worker 전체 흐름 | `catchhole-backend-ai/docs/` | claim 이후 Python 내부 처리 순서, 여러 패키지를 가로지르는 Worker 다이어그램 |
| Python 패키지별 세부 책임 | `catchhole-backend-ai/app/*/README.md` | chunking 기준, LLM 호출 책임, repository 책임, storage 접근 방식 |

## 작성 기준

- Java 문서에 이미 있는 ERD, API, 도메인 정책을 Python 문서에 다시 복사하지 않습니다.
- Python 문서는 Java 문서의 "Python Worker" 구간을 실제 코드 기준으로 풀어 설명합니다.
- 여러 패키지를 가로지르는 Mermaid 흐름도는 `docs/`에 둡니다.
- 단일 패키지 내부 동작은 해당 패키지의 `README.md`에 둡니다.
- 후속 논의/TODO는 Python Worker 내부 구현에 영향을 주는 경우에만 이 저장소에 남깁니다.
- Java와 Python의 계약이 바뀌는 내용은 Java docs를 기준으로 갱신하고, Python 문서에서는 해당 계약을 소비하는 방식만 설명합니다.
- Python 내부 실행 방식처럼 repo 구조에 영향을 주는 미확정 정책은 이 디렉터리에 검토 문서로 남깁니다.

## 문서 추가 판단 기준

새 문서를 만들기 전에 다음 순서로 판단합니다.

1. 프로젝트 전체 정책인가?
   - 그렇다면 Java repo `docs/`에 작성합니다.
2. Python Worker의 여러 패키지를 함께 이해해야 하는 흐름인가?
   - 그렇다면 이 디렉터리 `docs/`에 작성합니다.
3. 특정 Python 패키지 내부 구현 기준인가?
   - 그렇다면 `app/{package}/README.md`에 작성합니다.
4. 코드 주석으로 충분한 구현 세부 사항인가?
   - 그렇다면 별도 문서 대신 코드 주변에 짧은 주석을 둡니다.
