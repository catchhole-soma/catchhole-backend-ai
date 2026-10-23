# 캐릭터 관계 관찰 실험

[AI #93](https://github.com/catchhole-soma/catchhole-backend-ai/issues/93)은 기존 캐릭터
추출의 품질을 보존하면서 관계도에 쓸 근거를 같은 호출에서 얻을 수 있는지 검증한다.
운영 Worker는 기본값 OFF다. 이 단계에는 관계 DB 저장·확정·UI 공개가 없다.

## 추출 경계

`CharacterSettingExtractor(relationship_observations=True)`일 때만 기존 system 지시 뒤에
관계 관찰 지시를 추가하고 strict 응답 schema에 `relationship_observations`를 추가한다.
기존 `candidates`, `time_context`, 이름·상태 문맥, 모델, 출력 상한, 재시도 정책은 유지한다.
일반 Worker에는 환경변수로 이 실험을 켜는 경로를 만들지 않았다.

현재 실험은 `relationship-observations:v2`다. v1의 실제 결과와 조건은
[v0020 상세](ai-logic-versions/details/v0020.md)에 보존한다. v2는 관계 종류·방향·확실성·시점
분류를 제거하고 두 인물의 원문 지칭·nullable 이름, 짧은 요약, 연속 원문 인용 1~3개만 받는다.
한 청크당 최대 12개 상한은 유지한다. `source`/`target`의 순서는 관계 방향이 아니다.
가족 호칭/계보만 말하는 내용은 기존 팩트에 유지하고 추가 배열에 중복 추출하지 않는다.
가족인 두 사람의 실제 도움·다툼 같은 행동은 남길 수 있다. 이 제외는 프롬프트 지시이며
코드가 가족 의미를 완벽히 판별한다는 보장은 아니다.
이름·소속·동시 등장만으로 관계를 만들지 않고, 동행·도움을 친구/동맹으로 승격하지 않는다.
현재 청크에 없는 과거의 친분·반복 횟수를 만들지 않는다.
등록되지 않은 이름이나 이름을 모르는 인물도 관찰에 남길 수 있지만 실제 캐릭터에 연결하지 않는다.

요약에는 원문의 과거·가정·부정·비유·일방적 인식을 보존하도록 요청하지만 별도 enum으로
분류하거나 코드로 보정하지 않는다. 원문 위치 검증이 이런 의미의 보존을 보장하지 않으므로
실제 출력 검수가 필요하다. v1 자료를 v2로 재해석하지 않으며 파일 버전으로 구분한다.
현재 출력만으로 확정된 친구 간선이나 방향 있는 관계를 생성할 수 없다.

코드는 인용의 유일한 원문 위치, 두 지칭의 근거 내 존재, 자기 연결·형식·완전 중복을
검증한다. 유효하지 않은 관찰만 고정 사유와 함께 제외하고, 그 이유로 LLM을 다시 부르지 않는다.
위치 검증은 문장의 진실·이름 해소·관계 분류의 정확성을 증명하지 않는다.
전체 JSON 파손·provider 실패·출력 절단은 여전히 기존 후보까지 영향을 줄 수 있다.
같은 호출이므로 모델의 주의 분산, 토큰 증가, 시간·재시도 증가도 평가해야 한다.

## 재현

기존 다단계 FIXED 평가 명령에 아래 옵션을 더한다. 다른 모드나 WORLD 포함은 실행 전에 거절한다.
관찰 파일은 새 파일이어야 하며 0600 권한으로 생성한다. 원문 인용이 있으므로 비공개로 보관한다.

```bash
python -m evals.multi_stage_setting.runtime_cli \
  --gold private/gold.json --source-root private/sources \
  --mode FIXED --domains CHARACTER --episodes 1,2,3,4 \
  --character-setting-schemas private/character-setting-schemas.json \
  --analysis-model gpt-5.6-sol --subject-resolution-model gpt-5.6-terra \
  --comparison-model gpt-5.6-sol \
  --relationship-observations-output private/variant-observations.json \
  --output private/variant-predictions.json
```

공통 `LLM_REASONING_EFFORT=medium`, `store=false`를 유지한다. 기본 버전은 추가 옵션 없이
동일 입력·모델·상한으로 실행한다. `promptVersions.characterRelationshipObservations`에
실험 버전을 남기며 기존 fact 채점·최종 상태에는 관찰을 넣지 않는다. 별도 파일에는 성공한
청크별 episodeNo, sourceChunkId, sourceSha256, 원시 관찰·검증된 관찰과 제외 사유를 쓴다.
원시 관찰은 신뢰하지 않는 진단 데이터이며 설정·관계 상태에 반영하지 않는다. 실패 청크를 성공으로
채우지 않는다. 실행 성공 여부는 predictions의 실패 기록과 함께 확인한다.

## 판정

- 바바리안 1~4화의 기존 FINAL Gold, 동일 원문·스키마·시작 상태와 같은 채점기를 고정한다.
- 기존 후보 P/R/F1·값 정확도, 2차 결정과 실제 도달 수, 최종 상태·오류·미판정을 회차별로 본다.
- 주요 인물 누락·주체 오연결·수치·보유 능력·사망/회복 누락은 전체 평균으로 상쇄하지 않는다.
- 상호작용 근거의 완결성·불필요한 관찰·요약의 주체/시점/부정 보존을 별도로 검토한다. 친구 정답이 없는 초반 회차만으로
  문맥상 친구 판별이나 작품 전체 관계도 품질을 검증했다고 보고하지 않는다.
- 정상 호출 수뿐 아니라 절단·검증 재시도, 실제 토큰과 지연을 비교한다. 평가 judge 비용은 분리한다.
- 차이가 있으면 같은 조건의 반복 실행으로 변동을 확인한다. 한 번의 평균 점수로 비열등성을
  보장하지 않는다. 회귀가 남으면 기본값을 켜지 않고 결과와 다음 조건을 기록한다.

실제 실행 수치와 입력/코드 SHA는 AI 로직 버전 기록에 보존한다. 정답을 고쳐 점수를 맞추거나
실험에 유리한 실행만 선택하지 않는다. 운영 적용에는 관찰 영속화·멱등성·실제 인물 연결·검토
및 방향/시점별 관계 병합의 별도 계약이 필요하다.
