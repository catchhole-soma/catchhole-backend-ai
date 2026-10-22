# 설정·직접 근거 조회 계약 v1 (GH236)

Spring이 소유권과 설정의 의미를 결정하고 Python은 이 읽기 전용 계약을 호출한다. SQL·S3 key·사용자 ID를 모델 도구 인자로 받지 않는다. 이 계약은 챗봇과 향후 오류 리포트의 공통 조회 기반이며 자연어 해석, 의미 검색, 충돌 판정은 포함하지 않는다.

## 지원 자료와 기준

| 자료 | v1 기준 | 주의 |
| --- | --- | --- |
| 인물 탐색 | 활성 인물의 현재 이름 부분 일치, 선택한 실제 설정 경로의 현재 scalar 값 정확 일치 | 이름과 설정 조건은 AND. 별칭·동명이인 자동 해소 없음 |
| 설정 경로 탐색 | 실제 현재 snapshot/속성과 허용된 확정 이력에서 유형·저장 key·scope만 조회 | 값·근거 없이 동적 항목 선택에 사용. 미확정 후보와 schema의 미사용 key는 포함하지 않음 |
| 인물 현재값 | `WorkCharacter` snapshot + `CharacterSnapshotSource` | 최신 Fact로 재구성하지 않음. 기존 `CharacterSnapshotAccessor`와 표시명 resolver 재사용 |
| 인물 이력 | 확정 후보의 Fact와 후보 없는 수동/레거시 Fact | 현재 기여 여부와 확정 application mode를 구분. 모든 편집 이벤트의 감사 로그는 아님 |
| 세계관 현재값 | `WorldSetting` 실제 properties/version | `category + subjectId + scopeName + settingKey` 전체 경로 사용 |
| 세계관 근거·이력 | 연결된 확정 후보 | 기존 `WorldSettingMapper`의 root→scope 이동/현재 근거 판정 재사용. 전체 편집 로그가 아님 |
| 직접 원문 | 허용된 Fact/후보에 연결된 실제 본문과 인용 | 후보 quote만 믿지 않고 현재 원문·분석 manifest·hash·offset 확인 |
| 업로드 설정집 | **미지원** | 모든 응답의 `limitations`에 `SETTING_BOOK_UNSUPPORTED`. 확정 RDB 설정과 동일시하지 않음 |
| 특정 회차 당시 전체 상태 | **미지원** | 이력의 출처 회차 필터와 구별. `HISTORY_IS_NOT_A_STATE_SNAPSHOT` 표시 |

인물 AGE/LEVEL/PROFILE/STAT/SKILL/ITEM/활성 STATUS의 현재값은 기존 snapshot 해석을 그대로 사용한다. REMOVE 후에는 slot이 없으므로 과거 Fact가 남아 있어도 현재 응답에 재등장하지 않는다. MERGE는 복수 source를 유지하며 UPDATE는 교체된 현재 source만 사용한다. 늦게 확정한 `HISTORY_ONLY`는 최신 생성 기록이어도 현재값이 아니다.

세계관 `historyOnly`는 현재 근거에서 제외한다. 현재 속성이 수동 보호 경로라면 `USER_INPUT`을 반환하고 과거 후보를 현재값의 근거로 붙이지 않는다. MERGE의 근거는 최신 후보에서 이전 ADD/UPDATE까지 연결하며 동일 병합 결정의 복수 원본을 보존한다. 이후 UPDATE는 앞선 근거 연결을 끊는다. 세계관에는 REMOVE 이벤트 저장 모델이 없으며 삭제한 속성의 후보 이력만 남을 수 있다.

`origin=CONFIRMED_ANALYSIS`는 분석 후보가 확정된 기록이라는 뜻이지 인용이 이용 가능하거나 설정값 전체를 논리적으로 입증했다는 뜻이 아니다. 실제 인용 검증 결과는 `evidence.status`로 판단한다. 인물 수동 Fact는 기존 DB에 명시적인 origin 구분이 없어 후보 없는 과거 데이터와 구별할 수 없다. 출처가 없다는 이유만으로 수동이라고 추정하지 않고 `UNKNOWN`으로 반환한다.

### 인물 출처 상세 (`provenance`, OPT-33)

인물 CURRENT/CURRENT_COLLECTION/HISTORY/EVIDENCE의 선택적 `provenance`는 기존 `origin`을 재정의하지 않고 원자료, 내용 편집 여부, 후보의 보존된 확정 정보를 분리한다. 현재 설정값은 계속 snapshot이 기준이다. 원문과 값이 다르다는 이유만으로 현재값을 무효화하거나 작품 속 변화가 있었다고 추론하지 않는다. 이번 범위는 인물 Fact이며 세계관과 FIELD/탐색 응답에는 이 객체를 제공하지 않는다. 서버 구버전, 연결된 Fact 없는 현재값, OUT_OF_RANGE 근거에서도 생략한다. 생략은 미상이며 미편집·출처 없음의 증명이 아니다.

```json
{"provenance":{"sources":[{"reference":{"tool":"CHARACTER_EVIDENCE","targetId":"00000000-0000-0000-0000-000000000001"},"sourceKind":"SETTING_BOOK","contentEdited":false,"applicationAction":"USER_CONFIRM","recordedAt":"2026-01-02T03:04:05"}],"sourcesTruncated":false}}
```

- `sources`는 기존 `sources` 참조와 같은 순서·20개 상한을 사용한다. CURRENT의 MERGE는 출처별 정보를 유지하며, 하나의 출처로 병합값 전체를 대표하지 않는다. HISTORY/EVIDENCE는 해당 Fact 하나다. 잘림은 두 목록의 `sourcesTruncated`에 함께 표시한다.
- `sourceKind=EPISODE|SETTING_BOOK|USER_INPUT|UNKNOWN`은 원자료 종류다. 저장된 후보/Job의 sourceKind, 또는 연결된 회차로 판별한다. `SETTING_BOOK`이어도 설정집 원문 인용 지원을 뜻하지 않으며 기존 `SETTING_BOOK_UNSUPPORTED`는 유지한다. 인물의 후보·Job·회차 없는 Fact는 직접 입력과 레거시를 구별할 명시적 표지가 없어 `UNKNOWN`이다. `USER_INPUT`은 명시적으로 확인 가능한 직접 입력 출처를 위한 계약 값이며 이번 인물 mapper는 출처 부재나 수동 보호 표지만으로 이를 만들지 않는다.
- `contentEdited=true|false|null`은 후보의 `user_content_modified`를 그대로 제공한다. `true`는 내용 편집 표지가 존재한다는 뜻이며 원문과 최종값의 실제 차이·편집한 필드·편집 시점까지 증명하지 않는다. legacy null, 후보 없는 Fact는 null이다. `user_modified`, 수동 보호 slot, 검토 선택만으로 true 또는 false를 추정하지 않는다.
- `applicationAction=AUTOMATIC_CONFIRM|USER_CONFIRM|UNKNOWN`은 **후보에 현재 보존된 확정 메타데이터**다. `reviewed_automatically=true`이면 자동 확정, 그 외 `reviewed_application_mode`가 보존되어 있으면 사용자 확정으로 표시한다. 나머지는 미상이다. false만으로 사용자 확정을 추정하지 않으며 원문 파기에서 지워지는 raw 비교 JSON을 사용하지 않는다. 설정집 출처와 USER_CONFIRM을 함께 보고도 구체적인 `applyToCurrent` 클릭이나 내용 편집을 단정할 수 없다.
- `recordedAt`은 **Fact 생성 시각**이며 nullable ISO local datetime(서버 설정 시간대, offset 없음)이다. candidate/character `updatedAt`, 사용자 편집 시각, 현재 slot 마지막 반영 시각, 작품 속 사건 시각으로 설명하지 않는다. 정확한 `appliedAt`이나 완전한 편집 이력은 기존 저장 모델에 없어 이번 응답에 추가하거나 역산하지 않는다.
- 일반 API는 확정 후보의 내용/검토 선택을 다시 편집하지 못하게 한다. 이 정보는 별도 불변 이벤트 snapshot이 아니라 현재 보존된 후보의 메타데이터이므로 과거에 존재하지 않던 편집 이벤트를 복원하는 용도로 사용하지 않는다. 원문 파기는 인용·raw 비교 JSON을 지우되 위 확정 메타데이터와 Fact 생성 시각은 보존한다.
- 이 메타데이터는 인용 receipt를 발급하지 않는다. 기존 AVAILABLE 근거 검증, 소유권·출처 범위, snapshotVersion/readToken, 도구 상한과 실행 정산을 유지한다. 구조 정보와 현재 설정을 읽는 기능이며 내용 수정·재분석·원문 검색을 추가하지 않는다.

배포 시 Python이 `provenance` 생략과 신규 객체를 모두 읽도록 먼저 갱신한 뒤 Java의 새 응답을 제공한다. 신규 객체가 있으면 sources/sourcesTruncated와 각 항목의 reference/sourceKind/contentEdited/applicationAction/recordedAt을 검증한다. nullable 필드를 생략과 혼동하지 않는다. `src/test/resources/contracts/setting-tools-v1.json`은 새 응답, 기존 FIELD/COLLECTION fixture는 생략 호환성을 검증한다. 생성 SDK 소비자도 새 선택적 필드를 수용하는지 확인한다.

## API와 인증 경계

- 사용자: `POST /api/v1/works/{workId}/setting-tools/queries`, 기존 JWT. 회원은 `MemberPrincipal`에서 얻는다.
- 내부: `POST /api/internal/v1/works/{workId}/setting-tools/queries`, 기존 `X-Internal-Api-Key`와 신뢰 서버가 고정한 `X-Setting-Tool-Member-Id`.
- 두 adapter는 동일한 `SettingToolService`를 호출하고 활성 회원, 작품 소유권, 작품 ACTIVE, 인물 ACTIVE를 확인한다.
- 내부 헤더는 **서비스 간 신뢰 경계**다. API key를 소유한 서버는 회원을 지정할 권한이 있다. 인터넷/LLM에서 받은 회원 헤더를 그대로 전달하는 gateway로 사용하지 않는다.
- #237에서 인증된 챗봇 실행의 회원·작품을 Python `SettingToolContext`로 결합한다. 이번 클라이언트는 실행 ID/권한 토큰의 발급·검증이나 사용자 요청 수신 runtime을 만들지 않는다. 기존 Worker lease/AnalysisJob을 챗봇 실행처럼 위조하지 않는다.
- Fact→인물/후보/회차/Job, 세계관 후보→대상/회차/Job 관계의 작품·회차를 검사한다. provisional 후보는 제외한다. 청크는 Python 소유 `episode_chunks`에서 실제 존재와 회차 소속만 읽어 검사한다. 청크가 이미 없어졌다면 ID를 꾸며 반환하지 않는다.
- 다른 회원/작품 ID, 숨겨진 인물, 미확정 후보, 잘못 연결된 source는 조회 가능한 대상으로 취급하지 않는다. 기존 작품 소유권 정책과 동일하게 404를 반환한다.

공개 OpenAPI `/v3/api-docs`에는 `querySettingTools`, `queryInternalSettingTools`, `SettingToolQuery`, `SettingToolResponseV1` 및 중첩 `SettingTool*V1` schema가 등록된다. 성공/실패 envelope는 `CommonResponse`, 페이지는 `PageResponse`를 사용한다. 새 DB 테이블·migration·환경변수는 없다.

## 입력

필수: `tool`, `size`(1~50). `page`는 0~10000이며 생략 시 Java 0. Python client 기본은 page 0, size 20이다. 모델 도구 인자에 회원·작품·원문 저장소 key를 넣지 않는다.

| tool | targetId | 허용 선택 인자 |
| --- | --- | --- |
| `FIND_CHARACTERS` | 사용하지 않음 | query(최대 100자), factType + settingKey + valueEquals(함께 사용), page, size |
| `CHARACTER_FIELDS` | 생략 시 작품의 활성 인물 전체, 있으면 해당 활성 인물 | factType, episodeFrom/To(대상 인물이 있을 때만), page, size |
| `WORLD_FIELDS` | 생략 시 작품의 세계관 현재 경로, 있으면 해당 세계관 | worldCategory(대상 없을 때만), episodeFrom/To(대상 있을 때만), page, size |
| `FIND_WORLD_SETTINGS` | 사용하지 않음 | query(주체명, 최대 100자), worldCategory, page, size |
| `CHARACTER_CURRENT` | 활성 인물 UUID | factType, settingKey(정확한 저장 key), page, size |
| `CHARACTER_HISTORY` | 활성 인물 UUID | factType, settingKey, episodeFrom, episodeTo, page, size |
| `WORLD_CURRENT` | 세계관 UUID | settingKey, scopeName, page, size |
| `CHARACTER_CURRENT_COLLECTION` | 사용하지 않음 | factType, settingKey, readToken, page, size |
| `WORLD_CURRENT_COLLECTION` | 사용하지 않음 | worldCategory, settingKey, scopeName, readToken, page, size |
| `WORLD_HISTORY` | 세계관 UUID | settingKey, scopeName, episodeFrom, episodeTo, page, size |
| `CHARACTER_EVIDENCE` | Fact UUID | episodeFrom, episodeTo, size; page는 0 |
| `WORLD_EVIDENCE` | 확정 세계관 후보 UUID | episodeFrom, episodeTo, size; page는 0 |

`settingKey` 최대 150자, `scopeName` 최대 100자. `scopeName`은 `settingKey`와 함께만 사용하며 null은 루트 경로다. key를 생략하면 모든 경로를 페이지 조회한다. 경로 선택은 저장된 정확한 이름으로 한다. “상위 주문”→“고급 마법” 같은 의미 해석은 #238 책임이다.

회차는 1 이상이며 양끝을 포함한다. `episodeFrom > episodeTo`, 현재값+회차 범위, 탐색+targetId 등 허용하지 않는 조합은 400이다. 범위는 분석 manifest의 **출처 회차**에 적용하고 레거시는 연결 회차 번호를 사용한다. 사건 발생 시점(`effectiveFromEpisodeNo`)·검토 시각과 같다고 해석하지 않는다. `query`의 `%`, `_`, `!`는 SQL wildcard가 아니라 문자로 검색한다.

## 실제 설정 경로 선택과 인물 조건 검색 (#247)

질문에서 해석한 인물 설정 유형(`PROFILE`, `AGE`, `LEVEL`, `STAT`, `SKILL`, `ITEM`, `STATUS`, `TIME`)은 `factType`으로 전달한다. 현재값·이력에서 `factType`과 `settingKey`를 모두 쓰면 AND로 적용하고 **필터 이후** 페이지를 나눈다. 전체 설정 앞쪽 일부를 읽어 모델이 뒤쪽 항목의 존재를 추측하는 방식으로 사용하지 않는다. 특정 유형 전체를 묻는 경우 유형 필터만, 개별 동적 항목을 묻는 경우 실제 경로 탐색 후 저장 key를 사용한다.

- `CHARACTER_FIELDS`에 대상 인물이 있으면 현재 snapshot과 허용된 확정/수동/레거시 이력의 `(factType, settingKey)`를 중복 제거한다. 현재에서 삭제되거나 `HISTORY_ONLY`인 항목도 이력 경로로 찾을 수 있다. 회차 범위가 있으면 해당 범위의 이력만 제공한다.
- 대상 인물이 없는 `CHARACTER_FIELDS`는 **작품의 활성 인물 현재 경로만** 합친다. 이 경우 FIELD의 `subjectId`는 인물 ID가 아닌 서버가 인증한 **작품 ID**, `subjectName`은 `작품의 인물 설정`, `settingVersion`은 null이다. 인물 조건 검색의 경로 후보이며 다른 조회의 인물 targetId로 사용할 수 없다. 회차 범위는 거절한다.
- `WORLD_FIELDS`는 대상 세계관의 실제 현재 속성과 허용된 확정 이력의 `(scopeName, settingKey)`를 합친다. 동명 속성도 scope가 다르면 별개이며 회차 범위가 있으면 해당 이력만 반환한다. 대상이 없으면 작품의 현재 경로만 `(category, scopeName, settingKey)`별로 합치며 `worldCategory`로 분류를 제한할 수 있다. 이때 `subjectId=workId`, `subjectName=작품의 세계관 설정`, `settingVersion=null`이고 회차 범위는 거절한다.
- `FIELD`의 값·JSON·직접 근거·현재 기여 판단은 null/빈 목록이다. 메타데이터만으로 현재값이나 원문 근거를 주장할 수 없다. 후속 모델은 서버가 제공한 경로 후보의 참조를 선택하고, 오케스트레이터가 그 후보의 저장 key/type/scope를 재사용한다. 임의 key 생성은 모델 계약에서 허용하지 않는다.

`FIND_CHARACTERS` 조건은 `factType`, `settingKey`, `valueEquals` 세 필드를 함께 요구하며 `valueEquals`는 최대 200자다. 이름 `query`가 있으면 이름과 조건을 모두 만족해야 한다. `CharacterSnapshotAccessor`로 현재 snapshot을 해석하고 scalar 또는 object의 `value`/`name` 대표값을 앞뒤 공백 제거·Unicode NFC·대소문자 무시로 정확히 비교한다. 서술문의 부분 일치, 유사어 확장, 다중 조건, 숫자 범위 비교는 제공하지 않는다. 저장된 대표값이 없으면 설명용 factValue나 오래된 Fact로 대신 판단하지 않는다. 수동 수정·삭제·보관·타 작품 범위를 기존 snapshot/권한 정책과 같이 적용한다.

현재 조건 검색은 snapshot 포맷 해석을 SQL에서 중복 구현하지 않기 위해 작품의 활성 인물을 전체 조회한 뒤 필터와 결과 pagination을 적용한다. 따라서 조건 결과의 `totalElements`는 정확한 일치 수이고, 임의 scan 상한 때문에 뒤쪽 인물이 누락되지는 않는다. **DB 조회·JSON 해석 비용과 메모리는 작품의 활성 인물 수에 비례한다.** 작품 전체 경로 탐색도 같은 비용 특성이 있다. 전용 검색 인덱스·SQL 최적화와 운영 부하 측정은 미실시이며, 대규모 작품에 대해 검증된 처리량으로 설명하지 않는다. 읽은 인물들의 활성 상태·snapshot version은 응답 전에 묶음 재확인한다.

개별 경로 탐색도 `items.hasNext`가 true이면 다음 페이지를 읽어야 한다. 필터된 결과를 모두 읽었는지와 LLM 문맥에 모두 넣었는지는 별개이며, 호출자가 경로/결과 페이지를 중단한 경우 부분 확인 상태를 보존해야 한다. 원문·설정집·특정 시점 전체 상태의 지원 범위는 이 확장으로 달라지지 않는다.

## 작품 범위 현재 설정과 집계 기반 (#247)

`CHARACTER_CURRENT_COLLECTION`과 `WORLD_CURRENT_COLLECTION`은 인증된 작품의 현재 설정 행을 함께 읽는다. 회원·작품·대상 ID나 원문 회차를 입력으로 받지 않는다. 인물은 `ACTIVE` 등록 인물, 세계관은 현재 등록된 대상이 모집단이다. `ACTIVE`는 보관 여부이며 생존 여부를 뜻하지 않는다. 분석 후보·과거 Fact·원문상의 모든 등장 개체를 모집단에 합치지 않는다.

- 인물의 `factType`·실제 `settingKey`, 세계관의 `worldCategory`·실제 `settingKey/scopeName`을 **행 pagination 전에** 적용한다. scope가 null인 key 조회는 루트 경로만 뜻한다.
- 응답 행은 기존 `CURRENT`와 같은 mapper·현재 snapshot 해석·직접 근거 검증 경로를 사용한다. 표시값과 `structuredValueJson`의 `active`·설명 등 저장 속성을 보존하고, 이를 바탕으로 사망·부활·종족 같은 의미를 Java가 추정하지 않는다.
- 기존 2,000 code point 값 상한과 근거 상한을 유지한다. 구조화 값이 너무 크면 생략하고 `valueTruncated/partial/VALUE_LIMIT`로 표시한다. 잘린 값의 의미를 확인한 것으로 간주해서는 안 된다.
- `items.totalElements`는 필터에 맞는 **설정 행 수**다. 인물 수나 중복을 제거한 값의 종류 수와 다르다. 정렬은 대상 UUID의 문자열 오름차순, 이어서 인물 type/key 또는 세계관 scope(null 먼저)/key 순서다.

COLLECTION에만 응답의 `collection` 객체가 추가된다. 기존 도구 응답에서는 이 필드 자체를 생략하여 이전 wire fixture를 유지한다.

| collection 필드 | 의미 |
| --- | --- |
| `population` | `ACTIVE_REGISTERED_CHARACTERS` 또는 `REGISTERED_WORLD_SETTINGS` |
| `subjectCount` | 해당 작품의 모집단 대상 수. 세계관은 요청한 worldCategory가 있으면 그 분류만 포함 |
| `subjectsWithValues` | 필터에 맞는 현재 설정 행이 한 개 이상 있는 대상 수. 값이 비어 있거나 의미를 판단할 수 없는 대상도 포함될 수 있음 |
| `recordCount` | 필터 후 전체 현재 설정 행 수. items.totalElements와 동일 |
| `readToken` | 같은 모집단·필터를 계속 읽는지 확인하는 64자리 소문자 SHA-256 변경 감지값 |

`subjectCount - subjectsWithValues`는 해당 필터의 설정이 없는 대상 수다. 누락을 조건 불일치나 생존·종족 부재 등의 사실로 바꾸지 않는다. 모든 페이지를 읽었는지, 모든 값이 온전한지, 값만으로 질문의 의미를 판정할 수 있는지는 각각 별개다. 이름별 정보 부족 목록은 이 metadata에 포함하지 않는다.

첫 페이지는 `readToken` 없이 요청한다. 다음 페이지는 첫 응답의 `readToken`이 필수이며, 누락/형식 오류는 400이다. page 0에 같은 token을 보내는 최종 재확인도 허용하며 페이지 크기는 달라도 된다. 다른 작품·도구·필터의 token 또는 조회 이후 달라진 token은 409 `CONFLICT`다. 읽은 페이지를 최신 결과와 섞지 말고 처음부터 재조회하거나 변경으로 완전성을 확인할 수 없었다고 처리한다.

token은 작품·도구·필터, 대상 집합·이름·snapshot/world version, 세계관 `manuallyEditedPaths`, 인물 표시 schema 상태를 묶는다. 세계관 값을 그대로 수동 저장해 version이 유지되더라도 수동 출처·현재 근거 연결이 바뀌면 이전 token을 거절한다. 요청 안에서도 처음과 응답 직전의 scalar projection을 비교하여 인물 추가·보관, 세계관 분류 이동, 설정 변경과 표시 schema 변경을 감지한다. 빈 모집단에서도 작품 ACTIVE를 재확인한다. token은 권한 증명이나 보관된 과거 snapshot이 아니며 매 요청에서 기존 인증·소유권을 검사한다. 완료 후 새로운 변경을 고정하지 않으며, 원문 인용의 최종 검증은 기존 근거/receipt 경계를 그대로 사용한다.

현재 구현은 기존 JSON snapshot 해석을 재사용하기 위해 매 페이지에서 해당 모집단의 snapshot을 읽고 필터·pagination을 적용한다. 실제 응답 행의 근거만 대상으로 복원하고 S3는 읽지 않는다. 비용과 메모리는 모집단과 현재 설정 수에 비례하며, 동일 자료의 재조회·최종 token 확인에도 읽기 비용이 발생한다. 새 영속 snapshot·캐시·전용 JSON 검색 인덱스는 만들지 않았고 대규모 처리량을 측정하지 않았다.

목록·COUNT는 기존 `FIND_CHARACTERS`/`FIND_WORLD_SETTINGS`를 재사용할 수 있다. 이름 조건을 생략하면 전체 모집단이며, `FIND_WORLD_SETTINGS.worldCategory`는 이름 조건과 AND로 적용되어 필터된 정확한 전체 수를 반환한다. 기존 FIND 페이지에는 collection token이 없으므로 서로 다른 시점의 페이지를 동일 snapshot이라고 보장하지 않는다. COUNT만 필요한 경우 전체 행을 LLM에 전달할 필요가 없다.

DISTINCT나 의미 조건 판정은 이 계약 위에서 수행한다. 실제 경로를 먼저 선택하고 완전하게 읽은 현재 값의 대표 scalar를 중복 제거한 수와, 모델이 상태 설명을 읽고 판정한 대상 수를 구분한다. 원문에 등장한 종족 전체, 등록 세계관 종족 수, 현재 인물 설정에서 확인된 종족 종류 수를 서로 대신 답하지 않는다. 임의 SQL·데이터 변경·과거 상태 복원은 추가하지 않는다.

```json
{"tool":"CHARACTER_CURRENT_COLLECTION","factType":"STATUS","page":0,"size":50}
```

후속 요청은 같은 필터에 첫 응답의 `collection.readToken`을 넣어 page를 증가시킨다. 조회를 마친 뒤 page 0, size 1과 같은 token으로 다시 확인할 수 있다.

## 응답 필드와 불완전성

응답 data의 `schemaVersion=1`, `tool`, `items`, `partial`, `limitations`는 항상 반환한다. Item은 kind별 공통 record이며 사용하지 않는 필드도 null로 직렬화한다.

| 필드 | 의미 / null 허용 |
| --- | --- |
| kind | `CHARACTER`, `WORLD_SETTING`, `FIELD`, `CURRENT`, `HISTORY`, `EVIDENCE` |
| id | 탐색 대상/Fact/세계관 후보 ID. 현재 slot은 독립 row ID가 없어 null |
| subjectId, subjectName | 실제 인물/세계관 대상의 ID와 현재 표시명 |
| category | 인물 FactType 또는 세계관 category; 인물 탐색은 null |
| scopeName, settingKey, displayName | 경로와 표시명. scope null은 루트, 대상 탐색은 key null |
| value, structuredValueJson | 표시값과 구조화 값의 **JSON 문자열**. 각각 null 가능. Jackson2 JsonNode를 API에 직접 노출하지 않음 |
| valueTruncated | 표시값 절단 또는 구조화 JSON 생략 여부 |
| contributesToCurrent | 현재 slot이면 true. 이력은 현재 source 연결 여부. 판정 범위가 잘리면 null 가능. 대상 탐색은 null |
| operation, historyOnly | 실제 보존된 적용 정보만 반환. 레거시/수동에서 알 수 없으면 null |
| settingVersion | 현재 snapshot/세계관 version 또는 후보가 반영된 세계관 version. 없으면 null |
| effectiveFromEpisodeNo | Fact의 적용 시점. 원문 출처 회차와 다름. 나머지는 null |
| sourceEpisodeId, sourceEpisodeNo | 이력/근거의 출처 식별 정보. 현재값의 복수 근거는 sources로 조회. 없으면 null |
| origin | `USER_INPUT`, `CONFIRMED_ANALYSIS`, `UNKNOWN` |
| sources | 직접 근거를 여는 `{tool, targetId}` 목록. 출처 접근 가능 여부를 보장하지 않음 |
| sourcesTruncated | 현재 근거 참조가 상한/scan 범위 때문에 불완전함 |
| evidence | 직접 근거 조회에만 제공. 나머지는 null |
| provenance | 인물 Fact별 원자료·내용 편집·보존된 확정 정보. 미지원/미상 현재값/범위 밖에는 필드 생략 |

`items`는 content/page/size/totalElements/totalPages/hasNext다. 빈 목록은 정상 성공이며 “알 수 없는 오류”와 같지 않다. 페이지의 전체 개수는 해당 필터의 항목 수다. `hasNext=true`이면 후속 페이지를 읽어야 한다. `partial`은 페이지 외에 값/근거가 잘렸거나 이용 불가 상태인 경우를 표시한다. 정상 페이지네이션만으로 partial을 true로 만들지 않는다. 세계관 scan 상한은 현재 근거의 완전성에만 영향을 주며 목록 총개수를 추정값으로 바꾸지 않는다.

## 원문 상태와 인용

| evidence.status | 의미 |
| --- | --- |
| AVAILABLE | 분석 manifest와 현재 원문 정보·hash가 일치하고 반환 인용이 실제 본문과 일치 |
| LEGACY_UNVERIFIED | manifest가 불완전한 레거시. 현재 본문에 실제 있는 인용만 반환하지만 분석 당시 동일 원문 여부는 보장하지 않음 |
| NO_SOURCE | 원문 연결 또는 인용이 없음. 설정값 자체의 무효화를 의미하지 않음 |
| SOURCE_MISSING | 원문 key/객체가 없음 |
| SOURCE_PURGED | 회차 보관·파기 요청·작품 파기 상태. 인용 없음 |
| SOURCE_CHANGED | 원문 key/version/hash/회차 변경 또는 실제 저장 본문 hash 불일치. 인용 없음 |
| STORAGE_UNAVAILABLE | 일시적인 저장소 읽기 실패. 인용 없음; 실패 상세/저장소 key는 숨김 |
| OUT_OF_RANGE | 직접 참조의 출처 회차가 요청 범위 밖. 인용·설정값·현재 기여 판단을 생략해 범위 밖 Fact가 답변 근거로 섞이지 않게 함 |
| INVALID_QUOTE | quote/offset 일부가 실제 원문과 불일치. 검증에 통과한 인용만 남고 partial=true |

evidence는 candidateId/episodeId/sourceEpisodeNo/sourceChunkId/sourceHash/sourceVersion/matchesCurrentSource/quotes/truncated를 제공한다. 실제 저장된 ID/manifest만 반환한다. 세계관 후보에는 chunk ID가 없어 null이며, 인물 청크도 존재·소속을 확인한 경우만 반환한다. 검증 불가 원문 일치 여부는 null이다. hash/version은 검증용 내부 메타데이터이고 사용자 출처 표시에는 회차와 인용을 사용한다. S3 key와 인증값은 어느 응답에도 없다.

offset은 **전체 회차 본문 기준 Unicode code point**, 0-based `[startOffset, endOffset)`다. 이모지 등 UTF-16 길이를 섞지 않는다. offset 없는 레거시 인용은 유일한 정확 일치 위치만 채우고, 동일 문구가 여러 곳이면 위치를 null로 둔다. 저장된 offset이 틀리면 다른 위치로 임의 보정하지 않는다. quote는 원문 데이터이며 향후 LLM에서 지시문으로 해석하지 않아야 한다.

본문을 읽기 전과 후에 DB의 원문 상태를 확인한다. 두 번째 확인은 managed Entity 대신 scalar projection으로 수행해 1차 캐시가 삭제·교체를 가리지 않게 한다. 원문이 변경된 경우 현재 원문의 같은 위치에 과거 quote를 붙이거나 삭제된 예전 S3 객체를 fallback으로 읽지 않는다. API 응답 이후 이루어진 삭제까지 이미 받은 답변에서 회수하는 책임은 #241 대화/근거 재조회 정책에 있다.

현재값·이력·근거 응답을 조립한 뒤 인물 snapshot/world version과 활성 상태를 scalar 조회로 재확인하며, 세계관은 `manuallyEditedPaths`와 허용된 확정 후보의 근거 상태도 함께 비교한다. 값과 version이 같아도 새 후보 확정·이력 전용 전환·범위 이동으로 근거가 달라지면 변경으로 처리한다. 읽는 도중 설정이 변경되면 값과 근거를 섞어 반환하지 않고 409 CONFLICT로 재조회를 요구한다. 이 검사는 응답 직전까지의 일관성을 확인하며 응답 이후 상태를 고정하는 잠금이나 과거 snapshot 저장은 아니다.

세계관 COLLECTION token에도 같은 근거 상태를 포함한다. 기존 후보 권한·확정·회차 연결 필터와 작품/분류 범위를 재사용하며 후보 식별자·감사 시각·이력 전용/적용 버전·범위 이동 상태를 scalar로 읽는다. 후보 값·인용 본문을 추가로 로드하지 않지만 허용된 확정 후보 수에 비례하는 조회 비용은 추가된다. 변경 확인은 근거 표시 상한인 500개로 자르지 않으며, COLLECTION은 대상마다 조회하지 않고 작품/분류 단위로 한 번 읽는다.

## 정렬과 상한

- 인물 탐색: 소문자 이름, UUID. 세계관 탐색: category, 소문자 주체명, UUID.
- 인물 현재 slot/경로: FactType 이름, settingKey. 세계관 현재 속성/경로: scope(null 먼저), settingKey.
- 인물 이력: 출처 회차(null 마지막), 생성 시각, UUID. 세계관 이력: 출처 회차, 검토 시각, 생성 시각, UUID.
- 현재 인물 provenance: 선택한 페이지의 slot마다 마지막 21개만 DB 조회. 마지막 Fact의 레거시 표시값을 유지하고 최신 순 20개를 반환한다. 잘리면 `SOURCE_REFERENCE_LIMIT`.
- 현재 세계관 provenance: 검토 시각/생성 시각/UUID 역순으로 501행을 읽어 500행 cap 여부를 판단. cap이면 `WORLD_SOURCE_SCAN_LIMIT`, partial=true. 근거 연결 목록도 20개 상한이다. 알 수 없는 현재 기여 여부를 false로 단정하지 않는다.
- 표시값 최대 2,000 code point. 초과하면 잘라 반환하고 `VALUE_LIMIT`. 구조화 JSON은 중간에서 자르지 않고 생략한다.
- 직접 인용 최대 5개, 각 1,000 code point. 잘리면 evidence.truncated=true와 partial=true. 인용을 자를 때 끝 offset도 함께 조정한다.
- 이력·이름 탐색은 DB pagination, to-one fetch join, 현재 provenance는 slot별 제한 bulk 조회를 사용한다. 현재값 조건 탐색은 위 전체 snapshot 조회 후 필터/pagination 규칙을 사용한다. 목록에서 S3를 읽지 않는다. RDB 현재 snapshot JSON 한 건을 읽는 비용은 기존 모델과 같다.

실패 HTTP: 400 입력/조합 오류, 401 인증/활성 회원 실패, 404 접근할 수 없는 대상, 409 파기 중 작품/조회 중 상태 변경. 예상하지 못한 서버/DB 오류는 기존 500 정책이다. 원문 저장소 장애는 유효한 설정값을 살려 200 + STORAGE_UNAVAILABLE로 반환한다.

## 호출 예시

사용자 JWT로 대상의 현재 설정 경로를 조회한다.

```http
POST /api/v1/works/{workId}/setting-tools/queries
Authorization: Bearer <access-token>
Content-Type: application/json

{"tool":"WORLD_CURRENT","targetId":"<world-setting-uuid>","settingKey":"소모 마력","scopeName":"고급 마법","page":0,"size":20}
```

Python 신뢰 서버에서 인증된 실행 범위를 고정한다. key는 기존 서버 설정에서 읽고 도구 schema에 넣지 않는다.

```python
from app.clients.setting_tools_client import SettingToolContext, SettingToolsClient
from app.schemas.setting_tools import SettingTool, SettingToolQuery

client = SettingToolsClient(
    base_url=settings.spring_internal_api_base_url,
    internal_api_key=settings.spring_internal_api_key,
    context=SettingToolContext(member_id=authenticated_member_id, work_id=authorized_work_id),
)
try:
    result = await client.query(SettingToolQuery(
        tool=SettingTool.CHARACTER_CURRENT, target_id=resolved_character_id,
    ))
finally:
    await client.aclose()
```

클라이언트는 15초 timeout, redirect 금지, 자동 재시도 없음이다. HTTP/transport/계약 실패는 `SettingToolClientError`로 구분하고 원고/키/서버 응답 body를 예외에 넣지 않는다. Pydantic의 `SettingToolQuery.model_json_schema(by_alias=True)`와 응답 schema를 후속 tool adapter에서 사용할 수 있다. 모델 입력의 extra 필드와 허용하지 않는 조합은 검증 실패다.

## 검증과 후속 연결

- Java `SettingToolIntegrationTest`: 실제 H2/JPA, JWT/내부 인증, MockMvc wire contract, 현재/이력/source 정책과 저장소 실패. S3만 mock한다. 청크 테이블의 소속 projection은 기존 Flyway 테이블을 모사한다.
- 공통 fixture: Java `src/test/resources/contracts/setting-tools-v1.json`, Python `tests/fixtures/setting-tools-v1.json`. Java 실제 HTTP 응답의 UUID를 고정값으로 정규화해 비교하고 Python은 같은 JSON을 손실 없이 검증한다. 계약 변경 시 두 파일을 함께 갱신한다.
- Python `tests/test_setting_tools_client.py`: 실제 HTTP 요청 경로/헤더/도구 인자, schema 상한, 실패/부분 결과/계약 변경 처리. DB/LLM 호출 없음.
- #237: 요청·실행 ID, 인증된 범위 전달, 중복 실행 제어, 취소·timeout, 토큰 예약/정산, Spring↔Python 실행 경계.
- #238: 자연어 질문을 대상·정확한 경로·현재/이력 의도로 해소하고 위 도구를 호출해 근거 있는 단일 답변 생성. 애매한 후보는 임의 선택하지 않음.
- #239/#240: 임베딩 준비·원문 의미 검색과 이 근거 응답 결합. RDB에 답이 있으면 불필요한 원문 검색을 생략할 수 있음.
- #241/#242: 대화·근거 표시/삭제 정책, 평가·비용 관측. 현재 PR은 사용자 챗봇 UI나 유료 LLM 호출을 추가하지 않는다.

## 채팅 실행 연결 (#237)

실행 인증·토큰 원장·FastAPI 경로는 [채팅 실행 v1](chat-execution-v1.md)을 따른다. 채팅에서는 실행 capability/runner에 고정된 `/api/internal/v1/chat-runs/{id}/tools`를 사용한다. 자연어 도구 선택은 #238이다.

## 2026-10-14 FIELD 계약 fixture

추가 FIELD/조건 조회 계약은 Java `src/test/resources/contracts/setting-tool-fields-v1.json`과 Python `tests/fixtures/setting-tool-fields-v1.json`을 함께 검증한다. [질문 해석부터 답변까지의 연결](chat-query-quality-v1.md)을 참고한다.

COLLECTION 계약 fixture는 `src/test/resources/contracts/setting-tool-collection-v1.json`이다. token과 대상 UUID는 실제 HTTP 응답에서 고정값으로 정규화하여 비교한다. `SettingToolPostgresIntegrationTest`는 전용 Testcontainers DB에 기존 wire fixture용 JPA schema를 생성하며, 실제 Flyway·실행별 인증 어댑터·요청 중 다른 트랜잭션의 모집단 변경은 `ChatExecutionPostgresIntegrationTest`에서 함께 검증한다.

실행별 인증 어댑터는 factType/valueEquals를 보존한다. 대상 FIELD에 출처 회차가 하나라도 명시되면 실행 범위보다 넓힐 수 없고 생략한 반대쪽 경계는 실행 범위로 채운다. 회차 없는 FIELD는 현재 조회를 위한 catalog 의미를 유지하며 회차 필터를 자동 주입하지 않는다.
