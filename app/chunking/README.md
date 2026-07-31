# chunking

회차 분리, 원문 보존 청킹, 근거 위치 계산을 담당하는 패키지입니다.

청킹은 Python AI 서버가 담당합니다. Spring 서버는 업로드 파일과 분석 작업을 생성하고, Python AI 서버는 S3 원문을 읽어 분석 가능한 청크와 근거 위치 정보를 만듭니다.

## 현재 구현

- `chunk_splitter.py`
  - S3 회차 원문을 변경하지 않고 문단 단위로 읽습니다.
  - 문단을 묶어 기본 2,500자 안팎의 청크를 만듭니다.
  - 한 청크가 3,000자를 넘지 않게 문단 또는 긴 단일 문단을 나눕니다.
  - 각 청크에 원문 위치 정보를 함께 담습니다.

## 청킹 기준

현재 청킹은 AI나 외부 라이브러리를 사용하지 않는 규칙 기반 청킹입니다.

- 기본 목표 길이: 2,500자
- 최대 길이: 3,000자
- 최소 누적 길이: 1,000자
- 기준 단위: 문단
- 긴 문단 처리: 한 문단 내부에서 3,000자 기준으로 분리

웹소설 원고는 대사와 서술 흐름이 문단 단위로 이어지는 경우가 많기 때문에, 문장 단위로 너무 잘게 자르기보다 문단 경계를 우선 보존합니다.

3,000자는 청크 전체의 상한입니다. 다음 문단을 더하면 상한을 넘는 경우 현재 청크를 먼저 확정합니다. 마지막 청크나 전체가 짧은 회차는 최소 누적 길이보다 짧을 수 있습니다.

## 규칙 기반으로 시작하는 이유

초기 MVP에서는 semantic chunking이나 외부 text splitter보다 재현 가능한 규칙 기반 방식을 우선 사용합니다.

- 같은 원문을 넣으면 항상 같은 청크가 나와야 테스트와 디버깅이 쉽습니다.
- 설정 후보의 원문 근거를 화면에 보여줘야 하므로 offset 계산을 직접 통제해야 합니다.
- 웹소설 원고는 문단 단위로 장면, 대사, 서술 흐름이 이어지는 경우가 많아 문단 기준이 자연스럽습니다.
- 외부 라이브러리를 쓰면 빠르게 시작할 수 있지만, 원문 offset 보정과 문단 보존 정책을 별도로 맞춰야 합니다.

따라서 현재 구현은 `S3 회차 원문 -> 문단 추출 -> 길이 기준 청크 생성 -> 원문 offset 저장` 흐름을 직접 관리합니다. 추후 검색 품질이나 LLM 입력 품질 문제가 확인되면 token 기반 청킹, overlap, semantic chunking을 검토할 수 있습니다.

## 위치 정보

청크는 나중에 설정 후보의 원문 근거를 보여주기 위해 위치 정보를 포함합니다.

- `chunk_index`: 회차 안에서 몇 번째 청크인지
- `start_offset`: 회차 원문 기준 청크 시작 위치
- `end_offset`: 회차 원문 기준 청크 끝 위치
- `paragraph_start_index`: 청크에 포함된 첫 문단 번호
- `paragraph_end_index`: 청크에 포함된 마지막 문단 번호

`chunk_text`는 문단을 다시 이어 붙인 문자열이 아니라, 원문에서 `start_offset:end_offset`으로 그대로 잘라낸 문자열입니다. 그래야 이후 LLM이 반환한 `evidence_spans[].quote` 위치를 검증하거나 보정할 때 원문 위치가 틀어지지 않습니다.

## 청킹 결과 구조

청킹 함수는 DB를 직접 알지 않는 `EpisodeChunkDraft`를 반환합니다. 이후 mapper 또는 service 계층에서 `episode_id`를 붙여 `episode_chunks` 저장용 구조로 변환합니다.

아래 JSON은 API 응답 규격이 아니라, 저장 전 청킹 결과가 어떤 필드를 갖는지 설명하기 위한 예시입니다. 현재 흐름에서 Python이 청크 목록을 Spring에 응답으로 반환하지는 않습니다.

```json
{
  "episode_id": "00000000-0000-0000-0000-000000000000",
  "chunk_index": 0,
  "chunk_text": "첫 번째 문단입니다.\n두 번째 문단입니다.",
  "start_offset": 0,
  "end_offset": 22,
  "paragraph_start_index": 0,
  "paragraph_end_index": 1,
  "metadata_json": null
}
```

## DB 저장 필드 매핑

| 청킹 결과 | DB 컬럼 | 설명 |
| --- | --- | --- |
| 저장 시 주입 | `episode_chunks.episode_id` | 어떤 회차에서 생성된 청크인지 나타냅니다. |
| `EpisodeChunkDraft.chunk_index` | `episode_chunks.chunk_index` | 회차 안에서의 청크 순서입니다. |
| `EpisodeChunkDraft.chunk_text` | `episode_chunks.chunk_text` | LLM 입력과 근거 검색에 사용할 원문 조각입니다. |
| `EpisodeChunkDraft.start_offset` | `episode_chunks.start_offset` | 회차 원문 기준 시작 위치입니다. |
| `EpisodeChunkDraft.end_offset` | `episode_chunks.end_offset` | 회차 원문 기준 끝 위치입니다. |
| `EpisodeChunkDraft.paragraph_start_index` | `episode_chunks.paragraph_start_index` | 청크에 포함된 첫 문단 번호입니다. |
| `EpisodeChunkDraft.paragraph_end_index` | `episode_chunks.paragraph_end_index` | 청크에 포함된 마지막 문단 번호입니다. |
| mapper/service에서 선택 입력 | `episode_chunks.metadata_json` | 전처리 태그, 업로드 방식 같은 부가 정보를 선택적으로 저장합니다. 기본값은 `null`입니다. |

`embedding`은 청킹 자체의 결과가 아니라, 저장된 청크를 입력으로 `EpisodeChunkEmbeddingService`가 생성하는 후속 값입니다. 현재 Flyway V1의 `vector(1536)` 컬럼과 HNSW cosine 인덱스, Python 모델 매핑, 신규 청크 임베딩 저장, 범용 Top-K 검색과 실제 PostgreSQL 통합 테스트까지 구현되어 있습니다. 실제 OpenAI API와 샘플 원고를 사용한 의미 검색 품질 검증은 NVM-143에서 query·검색 범위 정책을 정한 뒤 진행합니다.

## LLM 근거 위치 처리 기준

LLM은 한 청크 안에서 여러 설정 후보를 추출할 수 있습니다. 현재 저장 구조에서는 각 후보의 `evidence_spans[].quote`, `start_offset`, `end_offset`으로 원문 근거를 표현합니다.

다만 LLM이 반환한 숫자 offset은 그대로 신뢰하지 않습니다. LLM은 원문 문구는 비교적 잘 복사해도, 문자 단위 위치 계산은 틀릴 수 있기 때문입니다.

현재 구현은 다음 순서를 기본으로 둡니다.

1. Python이 `episode_chunks.chunk_text`를 LLM에 전달합니다.
2. LLM은 각 설정 후보마다 원문에서 그대로 복사한 `evidence_spans[].quote`를 반환합니다.
3. LLM이 반환한 offset은 참고하지 않습니다.
4. Python이 `chunk_text` 안에서 quote를 다시 검색합니다.
5. exact match에 실패하면 줄바꿈/연속 공백을 공백 하나로 정규화해 다시 검색합니다.
6. 검색된 chunk 내부 위치를 `episode_chunks.start_offset`과 더해 회차 전체 offset으로 보정합니다.
7. quote를 찾지 못하면 후보는 저장하되 `start_offset`, `end_offset`은 `null`로 둡니다.

즉, 청크는 LLM이 설정을 이해하기 위한 문맥 단위이고, 실제 화면에 표시할 근거 위치는 `evidence_spans[].quote`를 기반으로 Python이 다시 검증해 계산합니다.

근거 위치 보정 구현은 `app/analysis/evidence_span_resolver.py`에 있습니다. 저장되는 offset은 chunk 내부 기준이 아니라 회차 전체 원문 기준이며, `end_offset`은 Python slice처럼 exclusive 값입니다.

## Offset 기준과 화면 하이라이트

`episode_chunks.start_offset`, `episode_chunks.end_offset`,
`evidence_spans[].start_offset`, `evidence_spans[].end_offset`은 모두
`Episode.content_s3_key`로 읽은 회차 원문 기준입니다.

Python은 BOM, CRLF, 탭, 연속 공백, 특수 공백을 제거하거나 치환하지 않습니다.
청크 본문도 새로 조립하지 않고 `source[start_offset:end_offset]` slice를 그대로 저장합니다.
따라서 Java가 같은 S3 원문을 그대로 응답하면 프론트에서 근거 범위를 검증해
하이라이트할 수 있습니다. offset은 Python 문자열 slice와 같은 Unicode code point
기준이므로, 브라우저는 UTF-16 인덱스로 변환한 뒤 quote 일치 여부를 확인합니다.

`evidence_span_resolver.py`의 공백 정규화 검색은 LLM이 반환한 quote를 청크 안에서
찾기 위한 국소적인 fallback입니다. 원문 자체를 변경하지 않으며, 최종 offset은 항상
원본 회차 문자열의 위치로 환산됩니다.

분석 뒤 원고 파일이 교체될 수 있으므로 각 `setting_candidates` 행에는 분석 당시의
`source_content_s3_key`도 함께 저장합니다. 근거 조회는 Episode의 최신 key가 아니라
후보에 고정된 key를 사용합니다.

## 기본 청크 크기

- 목표 길이: 약 2,500자
- 최대 길이: 3,000자
- 최소 누적 길이: 1,000자
- 문단 경계를 우선하며 마지막 청크와 짧은 회차는 1,000자보다 짧을 수 있습니다.
- 3,000자를 넘는 단일 문단만 원문 내부에서 3,000자 단위로 나눕니다.

기존 1,000자 청크보다 한 장면의 문맥을 더 넓게 제공하면서도, 지나치게 큰 단일
LLM 입력과 근거 탐색 범위를 피하기 위한 기준입니다.

## 후속 논의

- quote를 찾지 못한 설정 후보를 어떻게 처리할지 결정해야 합니다.
  - 현재는 후보를 저장하되 `start_offset`, `end_offset`을 `null`로 둡니다.
  - 대안은 후보 제외, LLM 재시도, 낮은 confidence 처리입니다.
- 프론트에서 offset을 어떻게 사용할지 결정해야 합니다.
  - 원문 상세 화면에서 `start_offset:end_offset` 범위를 하이라이트할 수 있습니다.
  - 사용자에게는 문자 위치보다 문단 번호, 주변 문맥, quote를 함께 보여주는 편이 더 자연스러울 수 있습니다.
  - 이를 위해 `paragraph_start_index`, `paragraph_end_index` 또는 quote 주변 context를 API 응답에 포함할지 검토합니다.
- 재시도와 디버그 관측성을 보강할지 결정해야 합니다.
  - 현재 debug JSON만으로는 LLM 재시도 여부를 확인할 수 없습니다.
  - chunk별 attempt count, 실패 사유, 모델명, token usage, quote match 실패 개수를 debug 출력 또는 JSON에 남길지 검토합니다.

## 후속 작업

- `episode_splitter.py`: 한 파일에 여러 회차가 들어온 경우 회차 단위로 분리
- `upload_files.storage_url` 기준 원본 파일 로드는 필요한 시점에 별도 검토
