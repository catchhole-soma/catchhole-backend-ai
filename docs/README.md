# CatchHole AI Backend Docs

Python AI 서버의 실행 구조, 분석 작업 흐름, DB 스키마 초안, 외부 연동 책임을 정리하는 문서 디렉터리입니다.

전역 개발 규칙과 협업 컨벤션은 레포 루트 문서를 기준으로 관리하고, AI 서버의 설계 의도와 구현 흐름은 이 디렉터리에 둡니다.

## 문서 목록

| 문서 | 내용 |
| --- | --- |
| [Database Schema](database-schema.md) | AI 서버가 참조하는 PostgreSQL ERD 초안, 스키마 변경 후보, 첫 이슈 접근 범위 |
| [Global](global.md) | FastAPI 앱 구조, 설정, 공통 예외 응답, 환경변수, Swagger, 테스트 기준 |
| [Analysis Job](analysis-job.md) | Spring API 호출 기반 분석 작업 실행 흐름, 상태 모델, DB 접근 책임 |
| [Analysis Job Workflow](analysis-job-workflow.md) | 분석 API 호출부터 Python worker 처리까지의 Mermaid workflow |

## 작성 기준

- 문서는 현재 코드와 함께 갱신합니다.
- DB 필드, 상태 전이, API 호출 계약, Spring/Python 책임 분리처럼 구현에 영향을 주는 결정은 이유를 함께 남깁니다.
- Notion/회의에서 정리한 ERD나 워크플로우를 옮길 때는 현재 Python AI 서버 구현과 다른 부분을 명시합니다.
- Spring 서버가 사용자 인증과 소유권 검증을 담당하고, Python AI 서버는 분석 작업 처리와 DB 상태 갱신을 담당한다는 책임 경계를 유지합니다.
- Queue 전환, pgvector 검색, LLM 출력 계약처럼 아직 구현 전인 내용은 현재 결정과 보류 사항을 구분해 기록합니다.
