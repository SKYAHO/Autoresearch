# 피처 스토어 prod/dev 환경 분리 — 설계 결정

- 이슈: SKYAHO/Autoresearch#399
- 상태: 구현 중
- 관련: #346(feast apply GKE Job), #358/#359(offline 조립·서빙 정합), [EPIC] #299

## 배경

피처 스토어는 지금까지 환경 개념이 단일했다. Registry(GCS)·Offline(BigQuery)·
Online(Redis)·Staging(GCS)이 하나의 좌표로 고정돼 있어 `feast apply`/materialize가
곧바로 프로덕션에 반영된다.

오토리서치 에이전트(자율 실험)는 가설을 세워 피처를 새로 정의·apply 하고
`get_historical_features`(BigQuery PIT)로 학습셋을 조립·평가하는 것을 수시로
자동 실행한다. 이 실험이 프로덕션에 섞이면 실서빙 일일 폐루프가 오염된다.
16회차 회의(2026-07-28)에서 "오토리서치 피처는 prod가 아니라 dev로 들어가야
한다", "피처 스토어에 prod/dev 개념을 만들어야 한다 — GCS 경로만 분리하면 된다"로
방향이 정해졌다.

## 핵심 관찰 — dev는 오프라인 전용이다

에이전트의 실험 루프는 전부 **오프라인**이다: apply(정의 등록) → BigQuery PIT
학습셋 조립 → 학습 → champion 대비 metric 평가. **온라인 서빙(Redis)은 prod만의
책임**이다. 실제로 학습 조립 경로(`build_offline_feature_store`, #358)는 이미
Redis 대신 sqlite 더미 online store를 쓴다. dev 실험이 승격 기준(metric)을 넘으면
정의·모델을 prod로 승격(main 머지 → prod apply → prod materialize → 서빙 A/B 또는
전량 교체)한다.

따라서 dev에 필요한 분리는 **registry + offline 두 좌표뿐**이고, 둘 다 이미 env
주입이다.

| 저장소 | 주입 변수 | dev 분리 방법 |
| --- | --- | --- |
| Registry | `GCS_REGISTRY_PATH` | 배포가 dev 경로 주입 (코드 변경 0) |
| Offline | `BQ_DATASET` | 배포가 dev dataset 주입 (코드 변경 0) |
| Staging | `GCS_STAGING_LOCATION` | 배포가 dev 경로 주입 (코드 변경 0) |
| Online(Redis) | — | **dev는 사용 안 함** (서빙·materialize는 prod만) |

## 유일한 함정 — apply가 Redis에 접속한다

dev가 Redis를 "쓰지" 않아도, `feast apply` 자체가 `full_scan_for_deletion: true`일
때 삭제된 FeatureView의 고아 키를 정리하려고 online store(Redis)에 **접속**한다
(#346이 apply를 VPC 안 GKE Job으로 옮긴 이유). 그래서 dev registry로 apply하면서
prod `feature_store.yaml`을 그대로 쓰면 apply가 **prod Redis를 스캔·삭제**할 수 있다.

리포 spec(2026-07-27-feast-apply-gke-job.md S4)에 명시돼 있듯, `full_scan_for_deletion:
false`면 apply는 Redis에 접속하지 않는다. 이것이 dev를 Redis-free로 만드는 열쇠다.

## 결정

### D1. 환경 셀렉터 `AUTORESEARCH_ENV` (기본 prod)

`feature_repo/env.py`를 신설해 단일 해석기를 둔다.

- `resolve_environment()` — `AUTORESEARCH_ENV`(prod|dev) 검증, 미설정·공백은 `prod`.
- `online_full_scan_for_deletion()` — 배포가 `FEAST_ONLINE_FULL_SCAN_FOR_DELETION`을
  명시하면 그 값이 최종 권한, 없으면 환경에서 파생(prod → True, dev → False).
- `ensure_online_store_env()` — `feature_store.yaml`의
  `full_scan_for_deletion: ${FEAST_ONLINE_FULL_SCAN_FOR_DELETION}` 치환이 성립하도록
  `setdefault`로 채운다("true"/"false").

`AUTORESEARCH_ENV` 미설정 시 prod 경로는 full_scan=true로 기존과 100% 동일하다.

### D2. Feast `project`는 prod/dev 공통

registry 경로(`GCS_REGISTRY_PATH`)가 이미 환경을 물리적으로 가르므로 `project`
네임스페이스를 나눌 필요가 없다. `feature_store.yaml`과
`build_offline_feature_store`의 `project`는 `autoresearch_feature_store`로 유지한다.
(초안에서 project를 환경 파생으로 바꾸려 했으나, Redis 키 충돌 우려는 dev가 Redis를
쓰지 않음으로써 근본적으로 사라지므로 되돌렸다.)

### D3. `full_scan_for_deletion`을 환경 파생으로

`feature_store.yaml`의 값을 `${FEAST_ONLINE_FULL_SCAN_FOR_DELETION}`으로 바꾼다.
치환 성립 보증은 project와 동일한 패턴이다.

- **Python 경로**(materialize, 서빙 리더, 검증 스크립트): `bootstrap.load_feature_store`가
  `FeatureStore(repo_path=...)` 직전에 `ensure_online_store_env()`를 호출한다.
- **bare `feast` CLI 경로**(feast-apply Job): Job env로 값을 직접 주입한다.

### D4. feast-apply 워크플로우에 환경 선택 배선

`push`(main)는 항상 `prod`. `workflow_dispatch`에 `environment`(prod|dev, 기본 prod)
입력을 추가한다. 렌더 스텝이 `AUTORESEARCH_ENV`에서
`FEAST_ONLINE_FULL_SCAN_FOR_DELETION`을 env.py와 동일 규칙으로 파생(prod=true,
dev=false)해 Job manifest에 주입한다. dev apply는 full_scan=false라 Redis에 접속하지
않는다.

## 범위 밖(후속)

- **dev registry/BQ/staging 좌표 주입**: 이 PR은 셀렉터·online 접촉 제어를 제공한다.
  dev로 실제 apply하려면 `GCS_REGISTRY_PATH`·`BQ_DATASET`·`GCS_STAGING_LOCATION`의
  dev 값이 필요하며, GitHub Environments 또는 dev-scoped repo vars로 주입한다 —
  `Autoresearch-infra` 후속 이슈.
- **dev BQ dataset·GCS prefix 프로비저닝**: `Autoresearch-infra` 후속 이슈.
- **승격(promotion) 파이프라인**: dev metric 통과 → prod 반영·A/B는 별도 트랙.

## 하위 호환성 정책(함께 못 박음)

16회차에서 서빙 중 피처의 조인 키 컬럼명을 in-place로 바꾸자 서빙 서버가 다운됐다.
환경 분리와 함께 다음을 운영 정책으로 명문화한다.

- 서빙 중인 피처는 **in-place 업데이트·삭제 금지**, **append-only**만 허용.
- 컬럼명·조인 키 변경은 **새 피처를 별도 정의**해 갈아끼우고 기존 것은 유지.
- 이 규칙은 운영 문서와 Claude 리뷰 액션에 함께 적어 에이전트가 막게 한다.

dev를 오프라인 실험장으로 두면 이런 파괴적 변경을 prod 밖에서 먼저 검증할 수 있다.

## 검증

- `feature_repo/env.py` 단위 테스트: prod 기본값, dev 파생(full_scan false), 명시
  `FEAST_ONLINE_FULL_SCAN_FOR_DELETION` 우선, 잘못된 값 실패, `setdefault` 비파괴.
- prod 회귀: `AUTORESEARCH_ENV` 미설정 시 full_scan=true, project 정본 문자열 유지.
