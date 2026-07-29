# 모델 승격 구조화 결과 계약

- **상태**: Implemented
- **날짜**: 2026-07-29
- **이슈**: #411
- **관련 계약**:
  - `docs/specs/2026-07-25-promote-model-champion-gate.md`
  - `docs/specs/2026-07-13-public-batch-execution-contract.md`

## 배경

현재 `python -m src.cli promote-model`은 정상적인 게이트 미달과 실행 오류를
모두 exit code 1로 반환한다. Airflow는 두 경우를 같은 task 실패로 처리하고,
운영자는 로그의 `[게이트 미달]`과 `[에러]` 문자열을 읽어 원인을 구분한다.

게이트 미달은 후보가 운영 기준을 통과하지 못했다는 정상적인 판정이다. MLflow
접속 실패, 지표 결손, 아티팩트 조회 실패 같은 실행 오류와 대응 방법이 다르므로
기계가 문자열을 해석하지 않고 두 결과를 구분해야 한다.

## 결정 요약

| 항목 | 결정 |
| --- | --- |
| 결과 종류 | `promoted`, `rejected`, `no_candidate`, `error` |
| 정상 종료 | `promoted`, `rejected`, `no_candidate`는 exit 0 |
| 비정상 종료 | `error`는 exit 1, 잘못된 CLI 조합은 exit 2 |
| 구조화 형식 | 버전이 명시된 JSON object `model-promotion-result-v1` |
| Airflow 전달 | 선택 인자로 지정한 파일에 JSON object를 원자적으로 기록 |
| 하위 호환 | 구조화 계약을 명시하지 않은 기존 호출은 종전 종료 코드와 텍스트 출력을 유지 |
| 보안 | 원본 예외, traceback, credential, URI userinfo, run ID를 결과에 넣지 않음 |

## 결과 의미

### `promoted`

새 후보가 모든 게이트를 통과했고 `champion` alias 이동까지 완료됐다. 최초
champion 생성과 기존 champion 대비 지표 비열화 통과를 포함한다.

### `rejected`

후보는 존재하지만 운영 정책을 통과하지 못해 alias를 변경하지 않았다. 다음은
정상 거부다.

- 후보 `val_roc_auc`가 현재 champion보다 낮음
- downsampling 후보의 calibration 아티팩트가 없음
- downsampling 후보인데 서빙 calibration 준비 가드가 꺼져 있음

거부 판정 중 MLflow나 아티팩트 저장소 접근 자체가 실패한 경우는
`rejected`가 아니라 `error`다.

### `no_candidate`

등록된 모델 버전이 없거나 최신 버전이 이미 champion이다. 할 일이 없는 정상
no-op이며 모델 이벤트 알림 대상이 아니다.

### `error`

판정을 신뢰할 수 없거나 alias 이동을 완료하지 못했다. 예시는 다음과 같다.

- MLflow 연결·권한·API 오류
- 후보 또는 champion의 필수 지표 결손
- 모델 버전과 run 연결 불일치
- calibration 아티팩트 저장소 조회 실패
- 결과 파일 기록 실패

`error`에서는 alias가 이미 변경됐을 가능성을 숨기지 않는다. 특히 외부 API
호출 결과가 불명확한 오류는 자동 재시도 전에 현재 alias를 다시 조회해야 한다.

## CLI 전환 계약

구조화 결과는 다음 두 선택 인자를 함께 지정할 때만 활성화한다.

```text
python -m src.cli promote-model \
  --model-name ctr-model \
  --champion-alias champion \
  --result-contract model-promotion-result-v1 \
  --result-path /airflow/xcom/return.json
```

- `--result-contract`의 v1 허용값은 `model-promotion-result-v1` 하나다.
- `--result-contract`와 `--result-path`는 함께만 지정할 수 있다. 일부만
  지정하거나 지원하지 않는 버전을 지정하면 판정 전에 exit 2로 거부한다.
- `--result-path`는 secret이 아니지만 운영 경로는 Airflow가 소유한다.
  application은 지정된 경로의 부모를 만들고 임시 파일 작성 후 같은
  filesystem에서 `os.replace`하여 완전한 JSON만 노출한다.
- 결과 파일 기록에 실패하면 판정 결과와 무관하게 exit 1이다.
- 구조화 모드의 stdout 마지막 줄에도 같은 payload를 JSON Lines 한 줄로
  기록한다. 사람용 상세와 traceback은 stderr로만 보낸다.
- 구조화 인자를 지정하지 않은 기존 호출은 기존 계약을 유지한다.
  `promoted`와 `no_candidate`는 exit 0, `rejected`와 `error`는 exit 1이며
  종전 한국어 텍스트를 출력한다.

구조화 모드를 opt-in으로 두는 이유는 `Dockerfile.train` 기반 Pod가 시작 시
`CODE_ARTIFACTS_BUCKET`의 최신 코드 아카이브를 받기 때문이다. 새 코드를 먼저
게시해도 기존 Airflow DAG는 구조화 인자를 보내지 않으므로 종료 코드 의미가
갑자기 바뀌지 않는다.

## JSON schema

모든 키는 고정된 JSON primitive 또는 null만 사용한다.

```json
{
  "event": "model_promotion_result",
  "contract_version": "model-promotion-result-v1",
  "outcome": "rejected",
  "model_name": "ctr-model",
  "champion_alias": "champion",
  "candidate_version": "13",
  "champion_version": "12",
  "metric_name": "val_roc_auc",
  "candidate_metric": 0.7812,
  "champion_metric": 0.7931,
  "reason_code": "metric_below_champion"
}
```

| 필드 | 타입 | 계약 |
| --- | --- | --- |
| `event` | string | 항상 `model_promotion_result` |
| `contract_version` | string | 항상 `model-promotion-result-v1` |
| `outcome` | enum | `promoted`, `rejected`, `no_candidate`, `error` |
| `model_name` | string | CLI에서 검증된 registry 모델 이름 |
| `champion_alias` | string | CLI에서 검증된 대상 alias |
| `candidate_version` | string 또는 null | 후보를 정하기 전 오류나 후보 부재면 null |
| `champion_version` | string 또는 null | 기존 champion이 없으면 null |
| `metric_name` | string | v1에서는 `val_roc_auc` |
| `candidate_metric` | number 또는 null | 조회 전 오류·후보 부재면 null |
| `champion_metric` | number 또는 null | champion 부재 또는 조회 전 오류면 null |
| `reason_code` | enum | 아래의 안정된 기계 판독 코드 |

v1 `reason_code`는 다음 값으로 제한한다.

| outcome | reason_code |
| --- | --- |
| `promoted` | `first_champion`, `metric_not_degraded` |
| `rejected` | `metric_below_champion`, `calibration_artifact_missing`, `serving_calibration_not_ready` |
| `no_candidate` | `registry_empty`, `already_champion` |
| `error` | `registry_access_failed`, `metric_missing`, `artifact_lookup_failed`, `alias_update_failed`, `result_write_failed`, `unexpected_error` |

새 사유는 소비자가 모르는 값을 안전하게 표시할 수 있어야 하며, 기존 코드의
의미를 바꾸지 않고 추가한다. Slack 문구는 `reason_code`를 자체 문구로 매핑하고
원본 예외 문자열을 사용하지 않는다.

## 애플리케이션 구조

`src/tracking/promote.py`는 판정 데이터를 보존하는 타입이 있는 결과 객체를
반환한다. 내부 흐름은 다음 순서를 지킨다.

1. 후보와 champion 버전을 조회한다.
2. 후보가 없거나 이미 champion이면 `no_candidate`를 반환한다.
3. 필수 지표와 downsampling calibration 조건을 검증한다.
4. 정책 미달이면 alias를 건드리지 않고 `rejected`를 반환한다.
5. 게이트를 모두 통과하면 alias를 이동하고 `promoted`를 반환한다.
6. 외부 시스템·데이터 계약 오류는 정상 결과로 삼키지 않고 예외로 전파한다.

CLI 어댑터는 구조화 모드에서 예외를 안전한 `error` 결과로 변환해 가능한 경우
파일과 stdout에 기록한 뒤 exit 1로 종료한다. traceback과 원본 예외는 기존
stderr 로깅 범위에만 남기며 구조화 payload에 복사하지 않는다.

## 저장소 경계

- 이 저장소는 판정과 결과 schema를 소유한다.
- Airflow는 공개 CLI만 실행하며 `src.tracking` 내부 API를 import하지 않는다.
- Airflow는 결과 파일을 XCom으로 운반하고 채널별 메시지로 렌더링한다.
- Slack webhook과 메시지 전송은 `Autoresearch-airflow` 소유다.

## 배포 순서

1. 이 계약을 구현한 코드 아카이브를 게시하되 기존 Airflow 호출은 구조화
   인자를 보내지 않아 legacy 동작을 유지한다.
2. 새 계약을 직접 실행해 네 outcome의 payload와 종료 코드를 검증한다.
3. `Autoresearch-airflow`가 구조화 인자와 XCom sidecar를 활성화한다.
4. scheduled `ctr_model_promote`에서 `promoted` 또는 `rejected` 실증과
   `no_candidate` 무알림을 확인한다.
5. 전환 안정화 뒤 legacy 호출 계약 제거는 별도 deprecation 이슈로 다룬다.

## 검증

- 판정 본체의 `promoted`, `rejected`, `no_candidate` 단위 테스트
- MLflow·지표·아티팩트·alias 오류의 `error` 분류 테스트
- 구조화 모드 세 정상 결과 exit 0, error exit 1, 인자 오류 exit 2 테스트
- legacy 모드에서 gate rejection exit 1이 유지되는 회귀 테스트
- 결과 파일 원자적 교체와 쓰기 실패 테스트
- JSON schema의 필드, 타입, enum과 원본 예외 미포함 테스트
- `uv run python -m pytest`
- `uv run --no-sync ruff check autoresearch tests tools`
- `git diff --check`

## 롤백

Airflow가 구조화 인자를 제거하고 이전 알림 callback으로 돌아가면 새
application 코드도 legacy 모드로 동작한다. 코드 아카이브까지 되돌려야 할 때는
이전 정상 revision을 다시 게시한다. 구조화 결과 파일은 상태를 저장하는 시스템
of record가 아니므로 별도 데이터 마이그레이션은 없다.

## 범위 밖

- Slack App·webhook·채널 생성
- Airflow callback과 XCom 배선
- expected-success-missing 감지
- 후보 선택 정책이나 champion 게이트 기준 변경
- Bot Token, 메시지 수정, thread 기반 incident 상태 관리
