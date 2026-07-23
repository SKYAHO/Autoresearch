# 학습 이미지 GCS 코드 부트스트랩 전환 — 구현 계획

- **관련 spec**: `docs/specs/2026-07-20-training-image-gcs-bootstrap.md`
- **이슈**: #177 (Autoresearch)

## 작업 분해

### Autoresearch (이 PR)

- [x] `scripts/feast_bootstrap.sh` → `scripts/gcs_code_bootstrap.sh` rename,
      로그 프리픽스·usage 메시지 범용화
- [x] `Dockerfile.feast`가 새 스크립트 경로를 참조하도록 갱신
- [x] `Dockerfile.train`을 부트스트랩 패턴으로 전환(`COPY src` 제거,
      ENTRYPOINT 추가)
- [x] `.gitattributes` 추가(`*.sh text eol=lf`) — 로컬 검증 중 발견한
      CRLF 셔뱅 결함 수정
- [x] `ci.yml` 학습 이미지 스모크 체크를 로컬 아카이브 주입 모드로 전환
- [x] 로컬 `docker build` + 아카이브 주입 실행으로 부트스트랩 왕복 실측
- [ ] `uv run python -m pytest -q`, `ruff check` (Docker 변경이라 Python
      회귀는 없을 것으로 예상되지만 CI와 동일 절차로 확인)
- [ ] PR 생성, 이슈 #177에 완료조건("코드 배포 방식 결정 및 문서화")
      충족 코멘트

### Autoresearch-airflow (별도 PR, 이어서 진행)

- [ ] `AutoresearchBatchPodOperator`: `cmds` 제거, `arguments`를
      `["python", "-m", module, *arguments]`로 통합해 이미지 ENTRYPOINT
      보존
- [ ] `cmds`를 직접 검증하던 기존 테스트 갱신(`test_ctr_training_dag_parse.py`,
      `test_feast_materialize_dag_parse.py`, `test_youtube_backfill_dag_parse.py`,
      `test_action_log_dag_parse.py`)
- [ ] `ctr_training/config.py`에 `CODE_ARTIFACTS_BUCKET` env 추가
      (`feast_materialize/config.py`와 동일한 `_airflow_env` 패턴)
- [ ] `ctr_training/dag.py`의 `plain_env`에 `CODE_ARTIFACTS_BUCKET` 배선
- [ ] `pytest` 전체 실행, PR 생성(이 PR이 `feast_online_store_materialize`
      DAG의 추정 장애도 함께 고침을 PR 본문에 명시)

### Autoresearch-infra (별도 PR, 코드만 — apply는 요청)

- [ ] `autoresearch-batch` GSA에 코드 아카이브 버킷
      `roles/storage.objectViewer` 바인딩 Terraform 추가
      (`terraform/envs/dev/airflow.tf` 기존 `raw_data` 버킷 바인딩 패턴 참고)
- [ ] `terraform plan` 결과를 PR에 첨부(로컬 실행 가능한 범위까지),
      실제 `apply`는 인프라 권한 보유자에게 요청

## 배포 순서(중요 — spec의 "경계·의존성" 참고)

Autoresearch-airflow의 오퍼레이터 수정이 머지되기 전까지는
`AUTORESEARCH_TRAINING_IMAGE` Airflow Variable을 이번에 새로 빌드한
부트스트랩 이미지 digest로 갱신하면 안 된다 — 갱신하는 순간
`ctr_model_training` DAG가 코드 없는 컨테이너에서 즉시 실패한다. 세
저장소 PR이 모두 머지된 뒤에 이미지 digest 승격 PR(#81과 동일한 패턴)을
별도로 진행한다.

## 검증 체크리스트

- [x] 로컬 docker build(Dockerfile.train) 성공
- [x] 로컬 아카이브 주입 모드: env 없이 실행 시 exit 2 + 명확한 오류 메시지
- [x] 로컬 아카이브 주입 모드: `python -m src.cli`, `train-model --help`,
      `run-pipeline --help` 정상 동작(코드가 실제로 풀렸다는 증거)
- [ ] CI에서 동일 스모크 체크 재현(Dockerfile.feast 포함)
- [ ] Autoresearch-airflow 테스트에서 `cmds` 키가 더 이상 존재하지 않고
      `arguments`에 `python -m <module>`이 포함됨을 확인
