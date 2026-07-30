# build-features 사전 점검 가드 — 설계 문서

> Status: Draft | Issue: #404 | Branch: `feat/404-build-features-preflight-guard`

## 배경

`#396`(Auto Research 최소 흐름 1회 완주) 실측: 로컬 에이전트 환경에 GCP ADC와 feast
패키지가 모두 없는 상태에서 `python -m src.cli build-features`를 실행했더니, 빠른
`KeyError` 대신 BigQuery 인증 시도가 응답 없이 멈췄다(수십 초 타임아웃). 에이전트는
결국 "이건 지금 못 한다"를 스스로 판단하지 못하고 2026-07-28에 만들어진 낡은 CSV
스냅샷을 조용히 재사용했다.

원인은 `src/pipeline/build_training_dataset.py`의 실행 순서다.
`load_training_entity_spine()`(BigQuery 클라이언트를 생성하고 spine을 조회)이
`_assemble_via_feast()` 안에서 `GCS_REGISTRY_PATH`/`GCS_STAGING_LOCATION` 환경변수
확인보다 **먼저** 호출된다(138~162번째 줄). 즉 필수 설정이 없어도 네트워크 I/O부터
시도한다.

`#423`(capability probe round_001/002 공통 발견)이 같은 근본 원인을 다른 각도에서
재확인했다. 두 이슈의 책임은 이미 분리돼 있다 — **이 fail-fast 가드는 #404 담당**,
split/seed 일치 검증과 스냅샷 provenance는 #423 담당(#423 완료, 2026-07-30 머지).

## 범위

"지정 기간으로 학습 데이터셋을 조립한다"(`build-features`)를 **먼저 실행 가능 여부부터
빠르게 확인**하도록 만든다. 범위는 다음 세 가지로 좁힌다.

1. **fail-fast 사전 점검** — 이번 spec의 핵심. 네트워크 I/O 전에 필수 조건을 확인한다.
2. **⑥ champion 공정 비교 절차** — 코드 없이 이 문서에 절차만 기록한다(아래 참고).
3. **"무엇을 썼는지" 기록** — 스냅샷 재사용 자동 폴백은 만들지 않기로 결정했다(아래
   "범위 밖" 참고). 성공/실패가 항상 명확하므로 별도 기록 코드가 필요 없다.

## 사전 점검 설계

`src/pipeline/build_training_dataset.py`에 `_verify_assembly_environment()`를
추가하고, `main()`의 날짜 검증 바로 다음(즉 `_assemble_via_feast()` 호출보다 먼저)
호출한다. 검사는 **가장 저렴한 것부터** 순서대로 진행한다 — 뒤 단계에서 실패할
것을 앞 단계의 비싼 호출(feast import, 네트워크) 뒤에 알게 되는 낭비를 없앤다.

1. **환경변수** — `GCS_REGISTRY_PATH`, `GCS_STAGING_LOCATION` 존재 확인(단순
   dict 조회, 이 저장소에서 가장 저렴한 체크)
2. **feast 패키지 import 가능 여부** — dev 그룹과 격리 그룹이라 dev venv에는
   원래 없음(`uv sync --only-group feast` 필요)
3. **GCP 자격증명 존재 추정** — `GOOGLE_APPLICATION_CREDENTIALS` 환경변수 또는
   `~/.config/gcloud/application_default_credentials.json` 파일

각 실패는 무엇이 없는지 + 어떻게 고치는지를 담은 `ValueError`로 즉시 중단한다(기존
`main()`의 날짜 검증과 같은 예외 타입 사용, 이 모듈의 기존 스타일과 일치).

### GKE 오탐 방지 (검증 완료)

3번(GCP 자격증명) 체크는 로컬 파일/환경변수만 본다. 그런데 `build-features`/
`run-pipeline`은 운영에서 `Dockerfile.feast` 이미지로 GKE Job으로 실행되고
(`docs/guides/training-image.md`), 인증은 `serviceAccountName`(Workload Identity,
metadata server)으로 처리된다(`deploy/feast/apply-job.yaml`,
`docs/runbooks/2026-07-15-feast-redis-gke-validation.md`) — 즉 운영 환경에는
`GOOGLE_APPLICATION_CREDENTIALS`도 로컬 ADC 파일도 **원래 없다**.

이 체크를 그대로 두면 운영 GKE Job마다 오탐으로 실패한다. 따라서
`KUBERNETES_SERVICE_HOST`(모든 k8s pod에 자동으로 존재하는 환경변수)가 있으면 이
체크를 건너뛴다.

```python
def _verify_assembly_environment() -> None:
    """feast 조립에 필요한 환경을 BigQuery 접속 전에 확인한다(#404/#423).

    순서가 중요하다 — BigQuery 클라이언트 생성(load_training_entity_spine)보다
    먼저 실행돼야, 자격증명 없는 환경에서 응답 없이 멈추는 대신(#396/#423 실측)
    즉시 명확한 이유와 함께 실패한다. 검사는 가장 빠른 것부터: 환경변수 →
    feast import → GCP 자격증명.
    """
    missing_env = [
        name for name in ("GCS_REGISTRY_PATH", "GCS_STAGING_LOCATION")
        if not os.environ.get(name)
    ]
    if missing_env:
        raise ValueError(
            f"{', '.join(missing_env)} 환경변수가 필요합니다. .env.example을 참고해 설정하세요."
        )

    try:
        import feast  # noqa: F401
    except ImportError as error:
        raise ValueError(
            "feast 패키지가 설치되어 있지 않습니다. dev 그룹과 의존성 충돌로 "
            "격리 그룹입니다 — `uv sync --only-group feast`로 설치하세요."
        ) from error

    # GKE 등 컨테이너 환경은 Workload Identity(metadata server)로 인증하므로
    # 로컬 자격증명 파일이 없어도 정상이다(docs/guides/training-image.md,
    # deploy/feast/apply-job.yaml 확인). KUBERNETES_SERVICE_HOST(모든 k8s pod에
    # 자동 존재)가 있으면 이 체크를 건너뛴다.
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return

    adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and not os.path.exists(adc_path):
        raise ValueError(
            "GCP 자격증명이 감지되지 않습니다 — BigQuery 접속이 응답 없이 멈출 수 "
            "있습니다(#396/#423 실측). `gcloud auth application-default login`을 "
            "실행하거나 GOOGLE_APPLICATION_CREDENTIALS를 설정하세요."
        )
```

`main()`에서의 호출 위치:

```python
def main(output_path=None, events_start_date=None, events_end_date=None):
    if not events_start_date or not events_end_date:
        raise ValueError(...)  # 기존
    _verify_assembly_environment()  # 신규 — _assemble_via_feast()보다 먼저
    if output_path is None:
        output_path = os.path.join(get_data_dir(), "processed", "training_dataset.csv")
    _assemble_via_feast(output_path, events_start_date, events_end_date)
```

## 테스트 설계

체크 순서를 "환경변수 → feast import → 자격증명"으로 잡은 덕에, 앞 두 체크는
feast 설치 여부와 무관하게 dev 그룹에서 검증 가능하다.

**dev 그룹(`tests/test_build_training_dataset.py`)** — feast 불필요:
- 환경변수 누락 시 `ValueError`(feast 설치 여부와 무관하게 가장 먼저 걸림)
- feast 미설치 시 `ValueError`(이 저장소 dev venv에는 실제로 feast가 없어 mocking 불필요)
- **회귀 테스트**: 환경변수 없이 `main()`을 호출하면 `load_training_entity_spine`이
  아예 실행되지 않는지 확인(`monkeypatch.setattr`로 호출 시 `AssertionError`를 내는
  더미로 치환) — 원래 버그(BQ가 env var 확인보다 먼저 실행됨) 자체를 잡는 테스트

**feast 그룹(신규 `tests/test_build_training_dataset_env_check_feast.py`,
`.github/workflows/ci.yml`의 `pytest-feast` 목록에 추가, `pytest.importorskip("feast")`)**
— 자격증명 체크만 feast가 실제로 설치돼 있어야 도달 가능:
- 자격증명 없음(`GOOGLE_APPLICATION_CREDENTIALS` 미설정 + ADC 파일 없음 monkeypatch) → `ValueError`
- `KUBERNETES_SERVICE_HOST` 설정 시 자격증명 체크를 건너뛰고 통과
- `GOOGLE_APPLICATION_CREDENTIALS` 설정 시 통과

## ⑥ champion 공정 비교 절차 (코드 없음, 문서만)

`run_pipeline`(`src/cli.py`)은 이미 champion 후보 학습 시 MLflow run 파라미터로
`events_start_date`/`events_end_date`/`feast_registry_path`/`assembly_source`/
`feature_service`를 기록한다(152~157번째 줄). 이 사전 점검 가드가 들어가 조립이
안정적으로 재현 가능해지면, champion과 공정 비교하는 절차는 다음과 같다 — 추가
코드 없이 기존 로깅만으로 충분하다.

1. `MlflowClient`로 champion(`ctr-model@champion`) run을 조회한다.
2. `run.data.params`에서 `events_start_date`/`events_end_date`를 읽는다.
3. 이번 사전 점검을 통과한 `build-features`를 같은 기간으로 재실행한다.
4. champion과 동일 조건(같은 기간·같은 조립 경로)의 데이터셋을 확보한 상태로 비교
   실험을 진행한다.

## 범위 밖

- **스냅샷 재사용 자동 폴백** — Auto Research 이슈 템플릿(`auto_research.yml`)의
  `snapshot_reuse` 드롭다운(허용/불허)을 이 스킬이 코드로 지원하지 않는다. 항상
  fail-fast만 하며, 스냅샷을 재사용하고 싶으면 사람이 기존 CSV를 직접 지정한다.
  필요성이 실제로 확인되면 별도 이슈로 판단한다.
- **champion 자동 조회 CLI**(`--from-champion` 같은 플래그) — 위 절차가 기존 MLflow
  로깅만으로 수동 수행 가능해 지금은 코드가 필요 없다.
- **GCP 자격증명 실제 유효성 검증**(네트워크 호출) — 파일/환경변수 "존재"만 보고
  실제로 유효한 토큰인지는 확인하지 않는다(그건 다시 네트워크 I/O라 이 가드의
  목적과 어긋난다).

## 완료 조건

- [ ] 에이전트가 기간(KST `YYYY-MM-DD ~ YYYY-MM-DD`)만 주면 학습 CSV가 만들어진다
      (기존 경로 유지, 회귀 없음)
- [ ] 필수 환경변수/feast 패키지/GCP 자격증명 중 하나라도 없으면, BigQuery 호출
      전에 명확한 안내와 함께 즉시 중단한다(조용히 스냅샷으로 넘어가지 않음)
- [ ] GKE 등 컨테이너 환경(`KUBERNETES_SERVICE_HOST` 존재)에서는 자격증명 파일
      체크가 오탐으로 막지 않는다
- [ ] champion 공정 비교 절차가 이 문서에 기록된다(코드 없음)

## 관련

- #396 — 최소 흐름 1회 완주, 이 문제의 최초 실측 기록
- #423 — 같은 근본 원인의 다른 측면(split/seed 검증, provenance) 담당, 완료·머지됨
- `docs/specs/2026-07-29-auto-research-minimum-loop-gaps.md` — ① 항목 근거
