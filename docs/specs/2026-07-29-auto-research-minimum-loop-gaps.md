# Auto Research 최소 흐름의 제약 — 에이전트가 자율 실험을 못 하는 지점 (2026-07-29)

> 상태: 실측 확정. 후속 트래킹 이슈 #403.
> 근거: 이슈 #396 (Auto Research 최소 흐름 1회 완주, 2026-07-29).
> 이 문서는 **왜 막히는지에 대한 실측 근거**이고, 작업 순서는 #403과 하위 이슈가 가진다.

## 목적

Auto Research 에이전트가 "가설 이슈를 읽고 스스로 실험한다"를 실제로 1회 수행했을 때
**실행이 불가능해 우회한 지점**을 근거와 함께 고정한다. 하위 이슈(#404 #405 #406 #407, #399)가
배경을 각자 반복 설명하지 않고 이 문서를 참조한다.

여기 적힌 것은 전부 **실제로 부딪힌 것**이다. 추측으로 넣은 항목은 없다.

## 전제

- 배포·개발 환경은 **리눅스**로 가정한다.
- 최소 흐름 정의(16차 회의): 이슈(가설) → 에이전트가 읽음 → 실험 수행 → 과정·결과 기록 →
  이슈 종료.
- 완주에 쓴 가설과 결과는 §부록에 있다. 지표는 이 문서의 논점이 아니다 — **막힌 지점이 산출물**이다.

---

## ① 정규 데이터 조립 경로를 에이전트가 실행할 수 없다 (#404)

`python -m src.cli build-features`는 #359 C2 이후 `_assemble_via_feast` 단일 경로이며 실행에
BigQuery `training_entity` spine 조회 권한, `GCS_REGISTRY_PATH`, `GCS_STAGING_LOCATION`,
그리고 feast 패키지(dev 그룹과 의존성 충돌로 **격리 그룹**)가 모두 필요하다.

**실측(2026-07-29 로컬):** GCP ADC 없음, feast 패키지 dev venv 미설치.

**결과:** 조립을 건너뛰고 2026-07-28에 생성된 CSV 스냅샷(1,775,808행)을 재사용했다. 가설 이슈가
대상 기간을 지정해도 에이전트가 따를 수 없다 — **모든 실험이 낡은 스냅샷에 고정된다.**

⑥(champion 공정 비교)이 여기에 종속된다. 그래서 별도 이슈로 분리하지 않고 #404의 완료 조건에
넣었다.

## ② 피처 1개 추가가 prod 모델 계약을 깬다 (#405)

`MODEL_FEATURE_COLUMNS`(`src/features/model_contract.py`)는 학습·서빙·Feast가 공유하는 정본이고
**순서까지 고정**돼 있다(ONNX 입력이 이름 없는 텐서라 순서가 틀리면 조용히 오예측). 설계 자체는
옳다. 문제는 **실험용 예외 통로가 없다**는 것이다.

- `src/pipeline/train.py` — `feature_columns = list(MODEL_FEATURE_COLUMNS)` 하드코딩, 인자로
  바꿀 수 없다.
- `src/pipeline/evaluate.py` — `require_model_feature_columns()`가 계약과 정확히 일치하지
  않으면 예외로 중단한다.

즉 실험 피처 1개를 넣는 유일한 방법이 **prod 계약 파일 수정**이다.

**실측:** `views_per_day` 1개를 계약에 추가한 상태와 하지 않은 상태로 전체 스위트를 각각 실행해
사전 실패와 분리했다.

| | 실패 | 통과 |
| --- | --- | --- |
| 계약 수정 전 | 42 | 787 |
| 계약 수정 후 | 85 | 743 |

신규 실패 43건 — 학습·시뮬레이션·일일추천·서빙이 한꺼번에 깨진다.

| 파일 | 신규 실패 |
| --- | --- |
| `tests/test_simulate_policy_round.py` | 16 |
| `tests/test_daily_recommendations.py` | 11 |
| `tests/test_pipeline_train.py` | 10 |
| `tests/test_serving_onnx.py` | 3 |
| `tests/test_model_feature_contract.py` | 2 |
| `tests/test_serving_online_features.py` | 1 |

계약 참조 파일은 28개(소스 12 · 테스트 12 · 문서 4). `tests/test_model_feature_contract.py`는
21개 튜플과 `== 21`, 오류 메시지의 `"zero-based position 20"`까지 하드코딩되어 있다.

**주의:** 위 42는 이 변경과 무관한 사전 실패이며, 그 정체는 §부록 B에 있다(저장소 문제 아님).

## ③ 파생 피처를 정규 파이프라인에 올릴 경로가 없다 (#399로 대체)

검증된 파생 피처를 실제로 반영하려면 Feast **ODFV 정의 → `feast apply` → FeatureService
`ctr_training_v1` 갱신 → 학습 DAG 재배포**가 필요하다. `feature_repo/feature_definitions.py`는
"FeatureService는 `MODEL_FEATURE_COLUMNS`와 이름·개수 1:1"을 전제로 하고, feast 그룹 통합
테스트도 21피처 전량을 단언한다.

로컬 에이전트에게 `feast apply` 권한도, 인접 저장소(`Autoresearch-airflow`)의 DAG 등록 경로도
없다. #396에서는 학습 CSV에 컬럼을 사후 주입하는 우회로로만 진행했다.

16차 회의 할당으로 #399(피처 스토어 prod/dev 분리)가 이미 발행돼 있어 **중복 발행하지 않고
그 이슈로 대체**한다.

## ④ 실험 추적 백엔드가 없고, 우회하면 prod 이름을 오염시킨다 (#406)

`.env`의 `MLFLOW_TRACKING_URI`가 빈 값이고 `train.py` 기본값은 `http://localhost:5000`(로컬에
서버 없음). #396은 `file:./mlruns`로 우회했다.

**부작용(실측):** `train.py` Step 9의 `register_model`이 `config.yaml`의
`registry.model_name`을 그대로 쓰므로, 로컬 스토어에 **`ctr-model` v1 / v2가 등록**됐다.
prod registry의 `ctr-model`과 **이름은 같고 실체는 다른** 모델이 생기는 구조다. 트래킹 URI를
잘못 지정하면 실험 run이 prod 네임스페이스(승격 게이트가 보는 이름)에 그대로 들어간다.

## ⑤ 결과의 유의성을 판정할 수 없다 (#407)

파이프라인이 단일 시드 1회 학습만 지원한다. #396의 주 지표 차이는 **+0.0019**로 성공 기준
**+0.002**에 0.0001 못 미쳐 기각됐지만, 그 차이가 시드 노이즈 범위 안인지 밖인지는
**측정하지 않았다**. 즉 판정이 시드 하나에 의존한다.

Auto Research 이슈 템플릿(#397 / PR #398)은 이 이슈를 예상해 기본 시드를 3개(42, 43, 44)로
두었으므로, 현재는 **템플릿이 요구하는 것과 실제 실행이 어긋난 상태**다.

## ⑥ champion과 공정 비교가 불가능하다 (#404 완료 조건으로 흡수)

champion(`ctr-model@champion`)은 다른 기간·다른 데이터 스냅샷으로 학습됐을 수 있어 변경 1개의
효과를 분리할 수 없다. #396은 비교 대상을 champion 대신 **동일 조건 baseline 재학습**으로
대체했다. 근본 해결은 champion의 학습 기간·조립 조건 재현이며, 이는 ①에 종속된다.

---

## 우선순위

① → ② → ④ → ⑤ (③은 #399, ⑥은 ①에 종속).

①이 막혀 있으면 모든 실험이 낡은 스냅샷에 고정되고 ⑥도 불가능하다. ②는 실험을 돌릴 때마다
운영 테스트가 대량으로 깨지는 상태를 끊는다.

---

## 부록 A — 완주에 쓴 가설과 결과

가설(#396): `views_per_day = view_count / (days_since_upload + 1)`. LightGBM은 축 정렬 분할만
하므로 두 피처의 비율을 스스로 만들지 못한다 — 원본 2개가 입력에 있어도 비율은 별도 신호가 된다.

조건: `data/processed/ds_feast.csv` 1,775,808행 / 양성률 2.00% / test 0.2 → 나머지에서 val 0.2 /
`random_state=42` / LightGBM n_estimators 200 · learning_rate 0.05 · num_leaves 31 ·
scale_pos_weight auto(49.0) · sampling_rate 1.0. **변경은 피처 1개뿐.**

| 지표 | baseline (21피처) | +views_per_day (22피처) | Δ |
| --- | --- | --- | --- |
| test ROC-AUC (주 지표) | 0.7446 | 0.7465 | **+0.0019** |
| Val ROC-AUC | 0.7480 | 0.7508 | +0.0028 |
| PR-AUC | 0.0881 | 0.0886 | +0.0005 |
| LogLoss | 0.5023 | 0.5018 | −0.0005 |
| Brier | 0.1689 | 0.1687 | −0.0002 |

**판정: 기각**(임계 +0.002 미달). 다만 feature importance(split 횟수)에서 `views_per_day`는
22개 중 12위(54)로 `view_count`(10위, 54)와 동급이고 `days_since_upload`(19위, 11)보다 위다 —
모델이 비율 피처를 실제로 쓰며 분모 원본을 부분 대체한다. **방향은 가설과 일치하나 크기가
임계에 못 미친다.** 단일 시드라 노이즈 여부는 미측정(⑤).

재현: `scripts/empirical_test/add_views_per_day.py`로 학습 CSV에 컬럼을 주입한 뒤
`MODEL_FEATURE_COLUMNS` 끝에 `views_per_day`를 추가하고 `train-model` / `evaluate-model`을
`--data-path`만 바꿔 각각 실행한다(브랜치 `exp/396-views-per-day`).

## 부록 B — 사전 실패 테스트 42건은 저장소 문제가 아니다

②의 회귀 표에 나오는 "계약 수정 전 42건"의 정체다. main의 **Python CI는 success**이며, 42건은
전부 **로컬(Windows) 개발 환경** 때문이다.

| 실패 | 건수 | 원인 |
| --- | --- | --- |
| `tests/test_pr_report_archive_*` | 26 | `node` 미설치 — 테스트가 `node -e`로 `.github/pr-report/archive.js`를 호출 |
| `tests/test_action_logs_daily.py` | 16 | Windows `MAX_PATH` 260자 초과 — 실패 경로가 291자(fingerprint 64자 + tmp suffix) |

리눅스에서는 둘 다 재현되지 않으므로 별도 이슈를 만들지 않는다. Windows에서 로컬 검증하려면
Node.js 설치와 긴 경로 허용이 필요하다.

## 부록 C — 로컬 Windows 인코딩

`train.py` / `evaluate.py`의 `load_config`가 `open(path, "r")`로 열어 Windows 기본 코덱(cp949)이
`src/pipeline/config.yaml`의 한국어 주석에서 `UnicodeDecodeError`를 낸다. 리눅스는 기본 UTF-8이라
재현되지 않는다 — 백로그가 아니라 환경 메모이며, 로컬 실행 시 `PYTHONUTF8=1`을 붙이면 된다.
