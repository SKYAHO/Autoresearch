# #278 action log -> BigQuery -> incremental feature -> Redis 검증 Runbook

이 문서는 이슈 #278의 뒷반부 폐루프를 Airflow 없이 검증하는 절차다.

```text
data/generated/round_a/event_log.parquet
  -> GCS data_lake/action_log/dt=YYYY-MM-DD/
  -> data_lake_raw.data_lake_action_log
  -> feast_offline_store.user_dynamic_feature
  -> Redis UserDynamicView
```

## 불변 조건

- `autoresearch/action_logs/` 스키마와 `daily.py`의 shard/merge 경로는 수정하지 않는다.
- action log의 일일 `dt` 파티션 하나는 독립적인 30일 히스토리다. 소비자는 검증 대상 `dt` 하나만 선택하며 파티션 간 UNION을 하지 않는다.
- 기존 champion, 기존 GCS 파티션, Terraform 소유 BigQuery 스키마를 수정하지 않는다.
- 생성 parquet, `.env`, 시크릿은 커밋하지 않는다.
- `load_raw_to_bigquery.py --tables action_log`는 `WRITE_TRUNCATE`로 전체 action log 테이블을 재적재하므로 업로드 전 GCS 파티션을 확인한다.

## 변수

실제 프로젝트 값은 `Autoresearch-infra` 출력과 현재 serving deployment에서 확인한다.

```bash
export PROJECT_ID="ar-infra-501607"
export REGION="asia-northeast3"
export ZONE="asia-northeast3-a"
export BUCKET="ar-infra-501607-autoresearch-dev-raw-data"
export RAW_DATASET="data_lake_raw"
export FEATURE_DATASET="feast_offline_store"
export PARTITION_DATE="2026-07-23"
export ACTION_LOG_PARQUET="data/generated/round_a/event_log.parquet"
export USER_ID="vu_0403"
```

`PARTITION_DATE`는 산출물의 입력 라운드 날짜와 충돌하지 않는 새 GCS 파티션으로
선택한다. 이 기록에서는 `2026-07-23`을 사용했다.

## 1. 산출물과 새 GCS 파티션 확인

```bash
test -f "$ACTION_LOG_PARQUET"
uv run python - <<'PY'
import pyarrow.parquet as pq
from collections import Counter

table = pq.read_table("data/generated/round_a/event_log.parquet")
rows = table.to_pylist()
print("rows=", table.num_rows)
print("event_types=", dict(Counter(row["event_type"] for row in rows)))
PY

gcloud storage ls "gs://${BUCKET}/data_lake/action_log/"
gcloud storage ls "gs://${BUCKET}/data_lake/action_log/dt=${PARTITION_DATE}/" 2>/dev/null && {
  echo "target dt already exists; choose another partition or stop"
  exit 1
} || true
```

기존 `dt` 목록을 검토한 뒤에만 업로드한다. `dt=2026-07-23`에 기존 객체가 없을
때 다음을 실행한다.

```bash
gcloud storage cp "$ACTION_LOG_PARQUET" \
  "gs://${BUCKET}/data_lake/action_log/dt=${PARTITION_DATE}/part-0.parquet"
gcloud storage ls -l \
  "gs://${BUCKET}/data_lake/action_log/dt=${PARTITION_DATE}/"
```

## 2. raw BigQuery 적재 및 `dt` 복원 확인

`--dataset`의 기본값은 `feast_offline_store`이므로 raw 적재에서는 반드시
`data_lake_raw`를 명시한다.

```bash
uv run python scripts/load_raw_to_bigquery.py \
  --project "$PROJECT_ID" \
  --dataset "$RAW_DATASET" \
  --location "$REGION" \
  --bucket "$BUCKET" \
  --tables action_log
```

성공 로그의 대상은 `data_lake_raw.data_lake_action_log`이어야 한다. BigQuery에서
`dt` 복원과 새 파티션 행 수를 확인한다.

```bash
bq query --use_legacy_sql=false --location="$REGION" \
  "SELECT CAST(dt AS STRING) AS dt, COUNT(*) AS row_count
   FROM \`${PROJECT_ID}.${RAW_DATASET}.data_lake_action_log\`
   GROUP BY dt ORDER BY dt DESC"
```

완료 조건은 `dt=${PARTITION_DATE}` 행이 입력 parquet 행 수와 같고, `dt`가
`DATE` 컬럼으로 존재하는 것이다. 이 기록에서는 새 파티션이 174행이었다.

## 3. `user_dynamic_feature` 대상 날짜 증분 갱신

동일한 action log 파티션을 반복해서 읽지 않도록 `feature_store_build`는
`data_lake_action_log.dt = PARTITION_DATE`를 SQL에 적용한다. `--tables`를
명시해 이 검증에서는 동적 유저 feature만 갱신한다.

```bash
uv run python -m autoresearch.jobs.feature_store_build \
  --project "$PROJECT_ID" \
  --dataset "$FEATURE_DATASET" \
  --raw-dataset "$RAW_DATASET" \
  --location "$REGION" \
  --partition-date "$PARTITION_DATE" \
  --tables user_dynamic_feature
```

마지막 stdout `job_summary`가 `status=succeeded`, `tables=["user_dynamic_feature"]`
인지 확인한다. 대상 유저의 snapshot 값도 확인한다.

```bash
bq query --use_legacy_sql=false --location="$REGION" \
  "SELECT user_id, event_timestamp, recent_click_count_7d,
          recent_view_count_7d, recent_like_count_7d, total_event_count_7d
   FROM \`${PROJECT_ID}.${FEATURE_DATASET}.user_dynamic_feature\`
   WHERE user_id = '${USER_ID}'
     AND event_timestamp = TIMESTAMP(DATE '${PARTITION_DATE}', 'Asia/Seoul')"
```

## 4. VPC 내부 진단 파드에서 online baseline 조회

Redis는 VPC 사설 endpoint이므로 로컬 venv에서 `feast`를 설치해 실행하거나
로컬에서 Redis에 직접 연결하지 않는다. `kubectl`이 private GKE endpoint에
접근 가능한 운영 bastion 또는 VPC runner에서 아래를 실행한다.

serving deployment의 image digest와 환경을 재사용한다. `serving` pod의
`serviceAccountName`은 `autoresearch-app`이어야 한다. 기존 deployment를
수정하지 말고 일회성 파드만 생성한다.

```bash
export SERVING_IMAGE="$(kubectl get deployment autoresearch-serving -n autoresearch \
  -o jsonpath='{.spec.template.spec.containers[0].image}')"
export REDIS_CA_SECRET_ID="autoresearch-dev-redis-server-ca"
export REGISTRY_PATH="gs://${PROJECT_ID}-feast-registry/registry.db"
export STAGING_PATH="gs://${PROJECT_ID}-feast-staging/"
export POD="autoresearch-feature-loop-278"

cat >/tmp/${POD}.yaml <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${POD}
  namespace: autoresearch
spec:
  serviceAccountName: autoresearch-app
  restartPolicy: Never
  containers:
  - name: feast-materialize
    image: ${SERVING_IMAGE}
    command: ["python", "-c"]
    args: ["import time; time.sleep(3600)"]
    env:
    - name: GCP_PROJECT_ID
      value: "${PROJECT_ID}"
    - name: REDIS_CA_SECRET_ID
      value: "${REDIS_CA_SECRET_ID}"
    - name: GCS_REGISTRY_PATH
      value: "${REGISTRY_PATH}"
    - name: GCS_STAGING_LOCATION
      value: "${STAGING_PATH}"
    - name: BQ_DATASET
      value: "${FEATURE_DATASET}"
    - name: REDIS_HOST
      valueFrom:
        secretKeyRef:
          name: autoresearch-serving-redis
          key: REDIS_HOST
    - name: REDIS_PORT
      valueFrom:
        secretKeyRef:
          name: autoresearch-serving-redis
          key: REDIS_PORT
EOF

kubectl apply -f /tmp/${POD}.yaml
kubectl wait --for=condition=Ready "pod/${POD}" -n autoresearch --timeout=180s
```

파드 안에서 materialize 전에 online 값을 기록한다.

```bash
kubectl exec -i -n autoresearch "${POD}" -- python - <<'PY'
from feature_repo.bootstrap import ensure_redis_ca_bundle, load_feature_store

ensure_redis_ca_bundle()
store = load_feature_store("feature_repo")
print(store.get_online_features(
    features=["UserDynamicView:recent_click_count_7d"],
    entity_rows=[{"user_id": "vu_0403"}],
).to_dict())
PY
```

## 5. target 날짜 materialize 및 online 재조회

`start-ts`/`end-ts`는 feature snapshot의 KST 구간을 정확히 감싼다. 로컬
`feast`가 없어도 serving image 안에서 명령을 실행할 수 있다.

```bash
kubectl exec -n autoresearch "${POD}" -- \
  python -m autoresearch.jobs.feast_materialize \
  --repo-path feature_repo \
  --views UserDynamicView \
  --start-ts "${PARTITION_DATE}T00:00:00+09:00" \
  --end-ts "2026-07-24T00:00:00+09:00"
```

`job_summary`가 `status=succeeded`, `mode=range`, `views=["UserDynamicView"]`인지
확인한 뒤 같은 online 조회를 반복한다. 입력 round가 baseline보다 click count를
증가시키는 경우에만 완료 조건을 충족한다.

동일 `event_timestamp`에 대해 이전에 잘못된 값을 materialize한 뒤 source를
고쳐 재실행하면 Feast Redis online store가 기존 timestamp를 보존할 수 있다.
그 상황에서는 정상 경로의 결과로 판정하지 말고, 해당 synthetic 유저의 stale
online state를 운영 승인 아래 정리한 뒤 target materialize를 재실행한다. 임의의
UNION이나 feature snapshot timestamp 변경으로 값을 맞추지 않는다.

## 6. 정리

```bash
kubectl delete pod "${POD}" -n autoresearch --wait=true
rm -f /tmp/${POD}.yaml
```

파드, 임시 파일, 진단 로그에 secret/token이 남지 않았는지 확인한다.

## 이번 검증 기록 (2026-07-23)

- 입력: `data/generated/round_a/event_log.parquet`, 174행, `click=6`.
- 업로드: `gs://ar-infra-501607-autoresearch-dev-raw-data/data_lake/action_log/dt=2026-07-23/part-0.parquet`.
- raw 적재: `data_lake_action_log` 성공, `dt=2026-07-23` 복원 174행.
- feature build: `user_dynamic_feature`, `status=succeeded`.
- 계약을 지킨 offline 결과: `vu_0403`의 `recent_click_count_7d=1`.
- serving image digest: `sha256:efd10acd04a39c66fe658d78c94f84bdd804bc38e794337768737246a1232c9b`.
- Redis materialize: `UserDynamicView`, 2026-07-23 00:00 KST부터 2026-07-24 00:00 KST, `status=succeeded`.
- Feast historical 조회와 target offline 조회는 모두 `1`을 반환했다.
- 첫 UNION 실행에서 나온 online `3`은 파티션 계약 위반 결과로 폐기했다. stale timestamp marker를 정리한 뒤 corrected materialize와 online 조회는 `1`을 반환했다.
- 최초 online baseline은 `2`였으므로, 제공된 round의 단일 신규 click 1건만으로는 "기존 online보다 증가" 조건을 충족하지 못한다. 이 수치는 데이터/기존 Redis 상태의 판정 결과이며, UNION으로 우회하지 않는다.
