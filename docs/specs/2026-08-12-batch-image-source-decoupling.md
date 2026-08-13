# 배치 이미지(Dockerfile.app) GCS 코드 부트스트랩 전환

- **상태**: Approved
- **날짜**: 2026-08-12
- **이슈**: #750
- **관련 문서**:
  - `docs/specs/2026-07-18-code-archive-gcs-upload.md` (#174 — 코드 아카이브 업로드 계약)
  - `docs/specs/2026-07-18-feast-bootstrap-gcs-code.md` (#181 — 부트스트랩 원형)
  - `docs/specs/2026-07-20-training-image-gcs-bootstrap.md` (#177 — 학습 이미지 전환 선례)
  - `docs/specs/2026-07-13-public-batch-execution-contract.md` (batch-contract-v1)
  - `docs/guides/release-pipeline.md` (이 전환으로 갱신 대상)

## 목적

`Dockerfile.app`은 `COPY autoresearch ./autoresearch`, `COPY src ./src`로 코드를
이미지 빌드 시점에 baking한다. 그 결과 파이썬 코드 한 줄 변경이 release 발행 →
이미지 빌드 → GAR push → `values.yaml` digest 승격 PR → 리뷰·머지 →
`helm upgrade` 전 과정을 요구한다. 변경 빈도가 다른 두 축(자주 바뀌는 앱 코드,
드물게 바뀌는 의존성)이 같은 배포 단위에 묶여 느린 쪽이 빠른 쪽의 배포 주기를
지배한다.

`Dockerfile.feast`(#181)와 `Dockerfile.train`(#177)이 이미 구현한 "실행
환경(이미지)과 코드(GCS 아카이브) 분리" 패턴을 배치 본체에도 적용한다. 전환 후
이미지 재빌드는 의존성·OS 변경 시에만 필요하다.

이 spec은 **새 메커니즘을 설계하지 않는다.** 부트스트랩 스크립트, 아카이브
업로드 워크플로우, GCS 경로 규약은 모두 이미 존재하고 운영 중이다. 남은 것은
배치 이미지를 그 규약에 편입시키고, 그 과정에서 갈라진 세 가지 규칙을 확정하는
일이다.

## 범위

**이 spec이 다루는 것**

- `Dockerfile.app`의 소스 분리와 그에 딸린 CI·release 검증 변경
- 배치 DAG의 `CODE_ARTIFACTS_BUCKET` 주입 위치
- 아래 세 결정의 확정과 근거 기록

**이 spec이 다루지 않는 것**

- Terraform plan/apply 게이트, Kubernetes admin root, Argo CD workload 동기화,
  Airflow Helm 배포 절차 — 모두 `Autoresearch-infra`·`Autoresearch-airflow`에
  이미 구현되어 있으며 이 전환으로 바뀌지 않는다.
- 이미지 digest 정본의 이동 (결정 2에서 명시적으로 기각)

## 결정

### 결정 1 — 아카이브 최신 표시는 포인터 방식을 유지한다

`code/<sha>.tar.gz`(불변) + `code/latest.txt`(포인터) 구조를 그대로 쓴다.
부트스트랩은 `latest.txt`를 읽어 SHA를 확정한 뒤 해당 아카이브를 받는다.

**근거**

- `scripts/upload_code_archive.sh`와 `scripts/gcs_code_bootstrap.sh`에 이미
  구현되어 feast·training 경로에서 운영 중이다. 추가 구현이 없다.
- `CODE_ARCHIVE_SHA` 환경 변수로 특정 커밋 고정 실행이 가능하다. 사고 조사와
  재현에 필요하다.
- 롤백이 포인터 한 줄 교체다. 옛 코드를 다시 빌드해 올릴 필요가 없다.

**포기하는 것**

- 다운로드가 2단계(포인터 → 아카이브)다. 오브젝트가 커밋마다 누적되므로 버킷
  수명 정책이 필요하다(현재 미설정 — "미해결 사항" 참조).

### 결정 2 — 이미지 digest 정본은 Git(`values.yaml`)에 유지한다

`deploy/airflow/values.yaml`의 세 변수와 `release.yml`의 승격 PR job 세 개를
그대로 둔다.

```
AIRFLOW_VAR_AUTORESEARCH_BATCH_IMAGE      ← promote-airflow-digest
AIRFLOW_VAR_AUTORESEARCH_TRAINING_IMAGE   ← promote-airflow-digest-training
AIRFLOW_VAR_AUTORESEARCH_FEAST_IMAGE      ← promote-airflow-digest-feast
```

**근거**

- 이번 전환의 목적("코드 변경이 이미지 배포를 요구하지 않게 한다")은
  `Dockerfile.app` 전환만으로 달성된다. digest 정본의 위치는 별개 문제이고,
  이번 작업에 묶을 이유가 없다.
- training·feast 이미지는 이미 소스를 담지 않으므로 digest가 의존성 변경
  시에만 바뀐다. 전환 후 batch도 같은 성격이 된다. 세 이미지를 다르게 취급할
  근거가 사라지므로 **한 규칙으로 묶는다.**
- digest를 Git 밖으로 빼면 변경 이력·리뷰 지점이 사라지고,
  `deploy-gke-dev.yml`이 배포 후 수행하는 "`values.yaml` digest = 실제 적용된
  Airflow Variable 값" 일치 검증이 성립하지 않는다.

**포기하는 것**

- 운영자가 Airflow Admin UI에서 즉시 digest를 바꾸는 민첩성. digest 변경은
  계속 PR 머지를 거친다. 단, 전환 후 batch digest 변경 빈도 자체가 크게
  줄어들므로 실질 마찰은 감소한다.

### 결정 3 — main merge된 코드는 사전 승인 게이트 없이 즉시 반영된다

`main` merge → `code-archive.yml`이 아카이브 업로드 + `latest.txt` 갱신 → 그
이후 시작되는 배치 Pod부터 새 코드로 실행된다. 실행 중인 Pod는 영향받지 않는다.

**근거**

- feast·training DAG가 이미 이 규칙으로 운영 중이다. batch만 다른 규칙을 두면
  "이 DAG의 코드는 언제 반영되는가"가 DAG마다 달라진다.
- 방어선은 PR 리뷰와 CI다. 사고 대응 수단은 사전 게이트가 아니라 사후
  롤백이며, 두 가지가 이미 확보되어 있다: `latest.txt` 되돌리기(분 단위),
  `CODE_ARCHIVE_SHA` 핀.

**포기하는 것 — 명시적으로 기록한다**

전환 전까지 `youtube_gcs_action_log`(운영 DAG)의 코드는 이미지에 구워져
있었고, 운영에 반영되려면 digest 승격 PR 머지라는 **사람 게이트**를 반드시
지나야 했다. 전환 후 이 게이트가 사라진다. `main`에 들어간 코드는 별도
승인 없이 다음 배치 실행부터 운영에 반영된다. 이는 의도된 변경이며, 그
대가로 코드 배포 지연이 제거된다.

## 전환 후 배포 경로

세 갈래가 서로 독립적으로 흐른다.

```
[소스]    main merge
            → code-archive.yml (자동, 게이트 없음)
            → gs://<bucket>/code/<sha>.tar.gz + code/latest.txt

[런타임]  release 발행
            → 이미지 빌드 + GAR push + 계약 검증
            → values.yaml digest 승격 PR → 리뷰·머지
            → deploy-gke-dev.yml (DAG pause → helm upgrade --atomic → 검증 → 상태 복원)

[차트]    Airflow Helm/DAG 변경
            → helm-lint / DAG parse CI → deploy-gke-dev.yml
```

Pod 실행 시점의 합성:

```
이미지가 제공:  /app/.venv                      (uv sync --locked --no-dev)
GCS가 제공:     /app/autoresearch, /app/src     (부트스트랩이 압축 해제)
실행:           PYTHONPATH=/app → python -m autoresearch.jobs.<module>
```

## `application_revision`의 의미 변화 (선례 승계)

배치 CLI 6개는 모두 `--version`에 `application_revision`을 담고, 그 값은
`os.getenv("AUTORESEARCH_REVISION")`이다. 이 환경 변수는 `Dockerfile.app`의
`ARG VCS_REF` → `ENV AUTORESEARCH_REVISION=${VCS_REF}`로 **이미지 빌드 시점
커밋**이 박힌다.

전환 후 이미지는 의존성 변경 시에만 재빌드되므로, 실행 중인 Pod에서
`--version`이 보고하는 revision과 실제 실행 코드의 SHA가 갈라진다.

이는 `Dockerfile.feast`·`Dockerfile.train`이 **이미 내린 결정을 승계하는
것**이다. 두 Dockerfile에 같은 주석이 달려 있다:

> revision 라벨·AUTORESEARCH_REVISION은 이미지 빌드 시점 커밋을 뜻하며,
> 실행 코드 버전은 부트스트랩 로그(`[gcs-bootstrap] code: ...`)가 담당한다.

영향 범위를 실측한 결과 `application_revision`은 **`--version` 출력에만**
쓰이고 action log·산출물 등 데이터 payload에는 기록되지 않는다. 따라서
데이터 provenance는 훼손되지 않는다. `release.yml`의 계약 검증
(`cli_revision == SOURCE_SHA`)도 이미지와 주입 아카이브가 같은 SHA에서
나오므로 계속 성립한다.

배치 이미지도 같은 주석을 달아 의미를 명시한다. 실행 코드 SHA를
`AUTORESEARCH_REVISION`에 반영하는 개선은 이 spec의 범위 밖이다
("미해결 사항" 4 참조).

## 영향 범위

DAG별 사용 이미지와 현재 버킷 설정 위치를 `Autoresearch-airflow`에서 실측한
결과다.

| DAG | 이미지 | 코드 출처 (현재) | 전환 후 |
|---|---|---|---|
| `youtube_gcs_action_log` (운영) | batch | 이미지 baking | GCS 아카이브 |
| `feature_store_build` | batch | 이미지 baking | GCS 아카이브 |
| `youtube_backfill` | batch | 이미지 baking | GCS 아카이브 |
| `ctr_training` | feast | GCS 아카이브 | 변화 없음 |
| `feast_materialize` | feast | GCS 아카이브 | 변화 없음 |
| `ctr_model_promote` | training | GCS 아카이브 | 변화 없음 |
| `lake_to_bigquery` | 없음 | 해당 없음 | 변화 없음 |

`CODE_ARTIFACTS_BUCKET`은 현재 아카이브를 쓰는 세 DAG가 각자 `dag.py`에서
설정한다. 전환 후 batch DAG 세 개가 추가로 이 값을 필요로 하므로, DAG별 설정을
늘리는 대신 **`dags/common/batch_pod_operator.py`의
`AutoresearchBatchPodOperator`가 공통 주입**한다. 공통 operator가 소유해야 새
DAG에서 누락되지 않는다. 기존 세 DAG의 개별 설정도 같은 커밋에서 공통 주입으로
흡수한다(`dags/ctr_training/config.py` 주석이 이미 "같은 버킷·같은 패턴"임을
명시한다).

## 저장소별 변경

### `SKYAHO/Autoresearch`

1. **`Dockerfile.app`**
   - `COPY autoresearch ./autoresearch`, `COPY src ./src` 제거
   - `COPY scripts/gcs_code_bootstrap.sh /usr/local/bin/gcs_code_bootstrap.sh`
   - `ENTRYPOINT ["/usr/local/bin/gcs_code_bootstrap.sh"]`
   - `PYTHONPATH=/app` 명시 — **선례와 다른 선택이며 의도적이다.**
     `Dockerfile.feast`·`Dockerfile.train`은 `PYTHONPATH`를 설정하지 않고
     `WORKDIR /app`에 의존한다(`python -m`이 cwd를 `sys.path`에 넣는다).
     동작하지만 working directory에 의존하는 암묵적 계약이라, KPO가
     working_dir을 바꾸면 조용히 깨진다. 비용이 없으므로 배치 이미지는
     명시한다.
   - `RUN chown appuser:appuser /app` — **재귀(`-R`) 없이** 디렉토리 소유권만
     넘긴다. 최종 스테이지가 `/app`에 COPY하는 것은 `.venv` 하나뿐이고
     (`pyproject.toml`/`uv.lock`은 builder 스테이지의 bind mount로만 쓰인다),
     아카이브가 덮어써야 할 기존 root 소유 파일이 없다. 아카이브는 `/app`
     아래에 새 엔트리를 만들 뿐이므로 디렉토리 쓰기 권한이면 충분하다
     (`Dockerfile.train`과 같은 판단, `Dockerfile.feast`는 `-R`을 쓴다).
   - 기존 `CMD ["python", "-c", "import autoresearch; ..."]`는 부트스트랩이
     코드를 푼 뒤 실행되는 스모크로 계속 유효하다.

2. **`.github/workflows/ci.yml`**
   - `changes` 필터의 `app` 항목에 `scripts/gcs_code_bootstrap.sh` 추가
     (train·feast 항목과 동일)
   - `docker-build-app` job에 부트스트랩 스모크 추가. `docker-build-train`의
     기존 패턴을 그대로 따른다:
     - `git archive --format=tar.gz -o /tmp/code-archive.tar.gz HEAD`
     - `-v /tmp/code-archive.tar.gz:...:ro -e CODE_ARCHIVE_LOCAL_PATH=...`로
       실행하여 batch-contract-v1 6개 모듈 `--help` 성공 확인
     - 환경 변수 없이 실행하면 **실패해야 한다**는 음성 검증
       (`부트스트랩이 env 없이 성공해서는 안 된다`)
   - 이미지에 파이프라인 패키지(`autoresearch/`)가 포함되지 않았음을 확인하는 검증 추가

3. **`.github/workflows/release.yml`**
   - batch 이미지 계약 검증(현재 `docker run --rm "$digest_ref" python -m
     <module> --help` / `--version`으로 `batch-contract-v1` 확인)이 전환 후
     그대로면 코드가 없어 실패한다. 로컬 아카이브를 주입해 실행하도록 수정한다.
   - 의존성만 확인하는 검증이 필요하면 train·feast job이 쓰는
     `--entrypoint python` 우회 패턴을 따른다.
   - digest 승격 PR job 세 개는 **유지한다**(결정 2).

4. **`tests/test_release_workflow.py`** — 현행 계약 테스트가 이 전환과 정면으로
   충돌한다. 먼저 테스트를 새 계약으로 뒤집고 구현한다.
   - `test_application_image_contains_daily_recommendations_command`가
     `"COPY src ./src" in dockerfile`을 단언한다. `src/`가 이미지에 없어야
     하므로 **반대 방향으로 뒤집는다**: 소스 COPY가 없을 것 + 부트스트랩
     ENTRYPOINT가 있을 것. 이 테스트의 원래 의도(daily_recommendations가 배치
     이미지에서 실행 가능할 것)는 `src/`가 아카이브에 들어가는지로 옮긴다.
   - `test_release_workflow_verifies_all_public_batch_commands`는 6개 모듈
     목록을 검사한다. 목록 자체는 그대로이나, release.yml의 검증 스텝이
     아카이브를 주입하도록 바뀌면 단언 대상을 함께 갱신한다.
   - `test_release_workflow_opens_an_airflow_digest_promotion_pr`는 **그대로
     통과해야 한다**(결정 2 — 승격 PR 유지). 이 테스트가 깨지면 범위를 벗어난
     것이다.

5. **문서**
   - `docs/guides/release-pipeline.md`의 batch 이미지 절 갱신: 이미지가 코드를
     담지 않는다는 사실과 소스 배포 경로를 반영한다.

### `SKYAHO/Autoresearch-airflow`

1. `dags/common/batch_pod_operator.py` — `AutoresearchBatchPodOperator`가
   `CODE_ARTIFACTS_BUCKET`을 모든 배치 Pod에 공통 주입
2. `dags/ctr_training/`, `dags/feast_materialize/`, `dags/ctr_model_promote/`의
   개별 버킷 설정 제거
3. KPO는 `cmds`가 아니라 `arguments`만 전달해야 한다. `cmds`를 쓰면 이미지
   ENTRYPOINT가 대체되어 부트스트랩이 실행되지 않는다. 전환 대상 DAG 세 개가
   이 규약을 지키는지 확인한다.
4. DAG parse 테스트 갱신

### 운영 / `SKYAHO/Autoresearch-infra`

- batch Pod의 GSA에 코드 아카이브 버킷 `roles/storage.objectViewer`가
  부여되어 있는지 확인한다. feast·training Pod는 이미 보유하고 있으므로 같은
  GSA를 쓰면 추가 작업이 없다. 다르면 infra 이슈로 분리한다.

## 검증

전환을 증명하는 가장 좁은 검증부터 넓힌다.

1. **이미지 내용**: 빌드된 이미지에 `/app/autoresearch`, `/app/src`가 없다.
2. **로컬 부트스트랩**: `CODE_ARCHIVE_LOCAL_PATH`로 아카이브를 주입해
   batch-contract-v1 6개 모듈의 `--help`와 `--version`이 성공하고
   `contract_version == "batch-contract-v1"`이다. `application_revision`은
   CI·release에서만 source SHA와 일치한다(이미지 빌드 SHA와 주입 아카이브
   SHA가 같기 때문). 운영 Pod에서는 갈라진다 — 위 "`application_revision`의
   의미 변화" 참조. 6개 중 다섯은 `autoresearch.jobs.*`이지만 마지막 하나는
   `autoresearch.recommendation.daily_recommendations`로 **네임스페이스가 다르다** — 두
   패키지(`autoresearch/`, #754 이전에는 `src/`도)가 아카이브에 들어가야 계약이
   성립한다.

   ```
   autoresearch.jobs.youtube_trending
   autoresearch.jobs.youtube_backfill
   autoresearch.jobs.action_log
   autoresearch.jobs.action_log_quality
   autoresearch.jobs.feature_store_build
   autoresearch.recommendation.daily_recommendations
   ```
3. **음성 검증**: 아카이브 없이 실행하면 exit 2로 실패한다.
4. **GAR digest**: push 후 digest를 pull해 2·3을 반복한다.
5. **실환경**: 배포 후 `youtube_gcs_action_log` 1회 실행이 성공하고 Pod 로그에
   `[gcs-bootstrap] code: <sha>`가 기록되며, 그 SHA가 `latest.txt`와 일치한다.

로컬 명령:

```bash
uv run python -m pytest -v
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
docker build -f Dockerfile.app -t autoresearch:ci .
git archive --format=tar.gz -o /tmp/code-archive.tar.gz HEAD
docker run --rm -v /tmp/code-archive.tar.gz:/tmp/code-archive.tar.gz:ro \
  -e CODE_ARCHIVE_LOCAL_PATH=/tmp/code-archive.tar.gz \
  autoresearch:ci python -m autoresearch.jobs.youtube_trending --help
```

## 채택하지 않은 대안

같은 논의가 재발하지 않도록 기각 사유를 남긴다.

| 제안 | 기각 사유 |
|---|---|
| `code/latest.tar.gz` 단일 오브젝트 덮어쓰기 + metadata에 source SHA | 과거 버전이 남지 않아 커밋 고정 실행이 불가능하고, 롤백에 재빌드·재업로드가 필요하다. 현행 포인터 방식이 이미 운영 중이며 두 기능을 모두 제공한다(결정 1). |
| batch image digest를 Airflow Admin → Variables로 이동 | digest 변경 이력이 Git에서 사라지고 배포 후 일치 검증이 성립하지 않는다. GitOps 원칙("workload의 정본은 Git manifest")과 충돌한다(결정 2). |
| batch만 UI로, training·feast는 Git에 유지 | 같은 성격의 이미지 셋에 규칙이 둘 생긴다. 새 배치 이미지를 추가할 때마다 판단이 필요하다(결정 2). |
| `latest.txt` 갱신을 수동 승인 게이트로 분리 | 이번 전환의 목적인 코드 배포 지연 제거를 절반 무효화하고, feast·training과 규칙이 갈라진다(결정 3). |
| 운영 DAG만 `CODE_ARCHIVE_SHA`로 고정 | 핀이 방치되면 운영 DAG만 오래된 코드로 돌면서 데이터 계약 불일치가 조용히 쌓인다(결정 3). |

## 미해결 사항

착수 전 또는 착수 중 결론이 필요하다.

1. **아카이브 오브젝트 수명 정책** — `code/<sha>.tar.gz`가 커밋마다 누적되는데
   현재 버킷 lifecycle 규칙이 확인되지 않았다. 보관 기간을 정해야 한다. 단,
   `CODE_ARCHIVE_SHA` 핀과 롤백이 과거 오브젝트에 의존하므로 지나치게 짧으면
   결정 1의 이점이 사라진다.
2. **batch Pod GSA 권한** — 위 "운영 / Autoresearch-infra" 항목. 확인 결과에
   따라 별도 이슈가 필요할 수 있다.
3. **전환 중 혼재 구간** — 새 이미지 digest가 승격되기 전까지 옛 이미지(코드
   baking)가 계속 뜬다. 옛 이미지는 `CODE_ARTIFACTS_BUCKET`을 무시하므로
   동작에 문제는 없으나, 승격 완료 전까지 "코드 변경이 즉시 반영되지 않는"
   상태가 유지된다는 점을 배포 담당자가 알아야 한다.
4. **`AUTORESEARCH_REVISION`을 실행 코드 SHA로 맞추는 개선** (이 spec 범위
   밖). 부트스트랩은 이미 실행할 SHA를 알고 있으므로
   (`code_version` 변수, `[gcs-bootstrap] code: <sha>` 로그)
   `exec` 직전에 `export AUTORESEARCH_REVISION="${sha}"` 한 줄이면 세 이미지
   모두의 revision 보고가 실제 실행 코드와 일치한다. 다만
   `scripts/gcs_code_bootstrap.sh`는 batch·feast·train 공용이라 세 이미지의
   동작이 함께 바뀌고, `--version` 계약을 소비하는 쪽(release.yml 검증)의
   기대값도 재검토해야 한다. 별도 이슈로 분리한다.
