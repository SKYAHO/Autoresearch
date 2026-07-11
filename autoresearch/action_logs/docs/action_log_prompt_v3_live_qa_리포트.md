# Action Log prompt v3 Live QA 리포트

- 측정일: 2026-07-12
- 대상 브랜치: `perf/111-action-log-prompt-token-reduction`
- 모델: `mistralai/mistral-nemo` via OpenRouter
- prompt version: `action_log_ctr_v3`

## 목적

v2 compact prompt가 실제 데이터에서 malformed JSON과 불완전한 index 집합을
반환한 문제를 재현하고, complete JSON skeleton 및 1회 schema 교정 재시도를
추가한 v3가 실제 OpenRouter 응답과 최종 EventLog 계약을 만족하는지 확인한다.

## 입력

- YouTube raw partition:
  `gs://ar-infra-501607-autoresearch-dev-raw-data/data_lake/youtube_trending_kr/dt=2026-07-10/part-0.parquet`
- 파티션 200행 중 앞 20행을 후보 pool로 사용.
- Virtual user: `vu_0001`~`vu_0005` 5명.
- 유저당 후보 20건, `chunk_size=0`, `max_concurrency=1`.
- QA 출력은 운영 GCS에 쓰지 않고 `/tmp/action_log_liveqa_v3_20x5/`에 격리.
- API key는 Windows User 환경변수에서 프로세스 메모리로만 주입했으며 출력,
  report, manifest에 기록하지 않았다.

## v2 실패 재현

v2로 동일 raw 20건 × user 3명을 호출했을 때 2건 성공, 1건 실패했다.

- 실패 유형: `invalid_json`
- 오류: `Expecting ',' delimiter: line 1 column 165`
- 응답은 홀수 index 10개만 반환하고 닫는 `]`를 하나 더 출력했다.
- pipeline은 실패 응답을 통과시키지 않고 quarantine했으며, 정상 2명의
  impression 40건만 최종 EventLog에 반영했다.

## v3 변경

- prompt의 추상 `...` 예시를 제거했다.
- 후보 수에 맞는 완전한 valid JSON skeleton을 동적으로 제공한다.
- row 수, index와 row 순서를 고정하고 두 판단값만 교체하도록 지시한다.
- `required_indexes`와 `expected_count`를 명시한다.
- `invalid_json`/`schema_fail`이면 OpenRouter에서만 교정 prompt로 최대 1회
  재생성한다. 재실패 시 기존 quarantine 계약을 유지한다.

## Live QA 결과

### 응답 계약

| 항목 | 결과 |
| --- | ---: |
| 최초 OpenRouter 호출 | 5 |
| 유효 JSON | 5/5 |
| top-level key가 `j` 하나 | 5/5 |
| row 수 20 | 5/5 |
| 각 row 길이 3 | 5/5 |
| index 집합 `0..19` | 5/5 |
| schema 교정 재시도 | 0 |
| quarantine | 0 |

### 판단값 분포

| 항목 | click propensity | watch fraction |
| --- | ---: | ---: |
| 판단 수 | 100 | 100 |
| non-zero | 82 | 82 |
| 고유값 수 | 10 | 10 |
| 범위 | 0.0~0.6 | 0.0~0.8 |
| 평균 | 0.1554 | 0.3475 |

판단값이 여러 값으로 분포하므로 모델이 skeleton의 `0.0` placeholder를 그대로
복사한 결과가 아니다.

### 최종 EventLog

| 항목 | 결과 |
| --- | ---: |
| impression | 100 |
| click | 5 |
| view | 5 |
| 총 row | 110 |
| CTR | 5% |
| `invalid_json` | 0 |
| `schema_fail` | 0 |
| `api_error` | 0 |

- Parquet 12컬럼 계약을 만족했다.
- `prompt_version=action_log_ctr_v3`와
  `llm_model=mistralai/mistral-nemo`가 전 행에 기록됐다.
- `watch_time_sec`는 view 이벤트에서만 non-null이었다.

## 판정과 제한

- v3 prompt는 이번 5-call 표본에서 v2의 malformed/incomplete 응답을 재현하지
  않았고, draft 및 EventLog schema를 모두 통과했다.
- 1회 schema 교정 재시도는 단위 테스트에서 `invalid_json`, `schema_fail`, 최종
  재실패 quarantine을 검증했다. 이번 Live QA에서는 최초 5콜이 모두 성공해 실제
  교정 호출은 발생하지 않았다.
- 작은 모델의 출력은 확률적이므로 5콜은 무오류를 보장하지 않는다. 첫 운영
  배치에서 initial failure, schema retry success/failure, quarantine 비율을 계속
  관측해야 한다.

## 확대 Live QA: Virtual user 100명

5명 QA 이후 동일한 GCS raw 영상 20건을 virtual user parquet의 앞 100행에
적용해 확대 검증했다. OpenRouter 동시성은 운영 안정화 기준에 맞춰 2로 제한했다.

### 호출 및 응답 계약

| 항목 | 결과 |
| --- | ---: |
| Virtual user | 100 |
| 유저당 후보 | 20 |
| 최초 OpenRouter 호출 | 100 |
| 유효 JSON | 100/100 |
| top-level key가 `j` 하나 | 100/100 |
| row 수 20 | 100/100 |
| 각 row 길이 3 | 100/100 |
| index 집합 `0..19` | 100/100 |
| schema 교정 재시도 | 0 |
| quarantine | 0 |

### 판단값 전수 검사

| 항목 | click propensity | watch fraction |
| --- | ---: | ---: |
| 판단 수 | 2,000 | 2,000 |
| non-zero | 1,663 | 1,699 |
| 고유값 수 | 29 | 26 |
| 범위 | 0.0~1.0 | 0.0~1.0 |
| 평균 | 0.1671 | 0.3886 |

2,000개 판단값이 충분히 분산되어 있어 모델이 skeleton placeholder를 복사한
결과가 아님을 확인했다.

### 최종 EventLog 전수 검사

| 항목 | 결과 |
| --- | ---: |
| impression | 2,000 |
| click | 100 |
| view | 100 |
| like | 44 |
| 총 row | 2,244 |
| CTR | 5% |
| 처리된 user | 100 |
| 유저당 impression | 20 |

- 2,244개 `event_id`가 모두 고유했다.
- click과 view의 `(user_id, video_id)` 집합이 일치했다.
- like 세션은 모두 click 세션의 부분집합이었다.
- 각 클릭 세션의 timestamp가 `impression < click < view < like` 순서를
  만족했다.
- `watch_time_sec`는 view에서만 non-null이었다.
- 전 행이 `prompt_version=action_log_ctr_v3`,
  `llm_model=mistralai/mistral-nemo`를 가졌다.

### 확대 QA 판정

v3는 이번 100-call 표본에서 최초 응답만으로 JSON 및 index 계약을 100% 준수했다.
schema retry 경로는 발생하지 않았으며 단위 테스트로 별도 검증된 상태다. 운영에서는
모델 응답의 확률적 특성을 고려해 retry 및 quarantine 지표를 계속 관측한다.
