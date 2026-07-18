# Feast ↔ Redis Cluster GKE 검증 Runbook

> 이슈 #148 · PR #154 · 설계 `docs/specs/2026-07-15-feast-redis-online-store.md`

Redis Cluster는 PSC 전용이라 dev VPC 내부(GKE `autoresearch` namespace pod)에서만
접근할 수 있고, IAM 인증은 Workload Identity(KSA `autoresearch-app`) 신원이
필요하다. 따라서 실제 연결·materialize·온라인 조회 검증은 GKE pod에서 수행한다.

이 runbook은 **GCP Infrastructure 권한 보유자**(`container.clusters.get/*`,
`cloudbuild.builds.create`, BigQuery write, GKE RBAC pod 생성 권한, 그리고
control plane master authorized networks에 등재된 네트워크)가 실행한다.

민감한 인프라 식별자는 하드코딩하지 않고 gcloud로 런타임 조회한다. 시작 전
아래 변수만 확인한다.

```bash
export PROJECT_ID="<dev GCP project id — Autoresearch-infra output 참조>"
export REGION="asia-northeast3"
export ZONE="asia-northeast3-a"
export CLUSTER="autoresearch-dev-gke"
export NAMESPACE="autoresearch"
export KSA="autoresearch-app"
export AR_REPO="autoresearch-dev-docker"
export REDIS_CLUSTER="autoresearch-dev-redis-cluster"
export CA_SECRET_ID="autoresearch-dev-redis-server-ca"
export BQ_DATASET="feast_offline_store"
export CODE_ARTIFACTS_BUCKET="<코드 아카이브 버킷 — Autoresearch-infra output 참조>"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/autoresearch-feast:148-validation"

gcloud config set project "${PROJECT_ID}"
```

GCS 버킷 경로는 배포 환경 값을 사용한다(예: `gs://${PROJECT_ID}-feast-registry/registry.db`,
`gs://${PROJECT_ID}-feast-staging/`). 실제 값은 `Autoresearch-infra` terraform
output 또는 배포 문서에서 확인한다.

## 0. 사전 확인

```bash
gcloud container clusters get-credentials "${CLUSTER}" --zone "${ZONE}"
kubectl get ns "${NAMESPACE}"
kubectl get sa "${KSA}" -n "${NAMESPACE}"
gcloud redis clusters describe "${REDIS_CLUSTER}" --region "${REGION}" \
  --format="value(state,shardCount,discoveryEndpoints[0].address)"
```

kubectl이 timeout되면 실행 네트워크가 master authorized networks에 없는 것이다.
현재 IP를 등재하거나 등재된 네트워크에서 실행한다.

## 1. feast 이미지 빌드·push (Cloud Build)

이미지는 의존성(`pyproject.toml`/`uv.lock`)이나 부트스트랩 스크립트가 바뀔
때만 재빌드하면 된다. 코드는 이미지에 포함되지 않고, 파드 시작 시
부트스트랩이 GCS 코드 아카이브를 받아 실행한다 (#181,
`docs/specs/2026-07-18-feast-bootstrap-gcs-code.md`).

로컬 docker 없이 Cloud Build로 `Dockerfile.feast`를 빌드한다. 저장소 루트에서:

```bash
gcloud builds submit \
  --tag "${IMAGE}" \
  --substitutions=_VCS_REF="$(git rev-parse HEAD)" \
  --config=- <<'YAML' .
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "--build-arg", "VCS_REF=${_VCS_REF}", "-f", "Dockerfile.feast", "-t", "${_IMAGE}", "."]
images: ["${_IMAGE}"]
YAML
```

또는 간단히 (VCS_REF 기본값 unknown 허용 시):

```bash
gcloud builds submit --tag "${IMAGE}" -f Dockerfile.feast .
```

빌드 성공 시 이미지가 Artifact Registry에 push된다.

## 2. BigQuery 더미 feature 데이터 적재

로컬(또는 BigQuery write 권한이 있는 곳)에서 실행. 이 단계는 control plane이
필요 없다.

```bash
export GCP_PROJECT_ID="${PROJECT_ID}"
export BQ_DATASET="${BQ_DATASET}"
uv run --no-dev --group feast python scripts/generate_and_upload_dummy_data.py
```

4개 테이블(`user_static_feature`, `user_dynamic_feature`, `video_feature`,
`user_category_similarity`)이 `WRITE_TRUNCATE`로 적재된다.

## 3. 검증 pod 매니페스트

디스커버리 endpoint를 조회해 매니페스트를 생성한다. (스크래치 경로 사용)

```bash
REDIS_ADDR="$(gcloud redis clusters describe "${REDIS_CLUSTER}" --region "${REGION}" \
  --format='value(discoveryEndpoints[0].address)')"

cat > /tmp/feast-validation-pod.yaml <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: feast-redis-validation
  namespace: ${NAMESPACE}
spec:
  serviceAccountName: ${KSA}
  restartPolicy: Never
  containers:
    - name: feast
      image: ${IMAGE}
      # command는 ENTRYPOINT(부트스트랩)를 덮어쓰므로 args만 지정한다.
      # Airflow KubernetesPodOperator에서도 cmds 대신 arguments를 사용한다.
      args: ["sleep", "7200"]
      env:
        - name: CODE_ARTIFACTS_BUCKET
          value: "${CODE_ARTIFACTS_BUCKET}"
        - name: GCP_PROJECT_ID
          value: "${PROJECT_ID}"
        - name: BQ_DATASET
          value: "${BQ_DATASET}"
        - name: BQ_LOCATION
          value: "${REGION}"
        - name: GCS_REGISTRY_PATH
          value: "gs://${PROJECT_ID}-feast-registry/registry.db"
        - name: GCS_STAGING_LOCATION
          value: "gs://${PROJECT_ID}-feast-staging/"
        - name: REDIS_HOST
          value: "${REDIS_ADDR}"
        - name: REDIS_PORT
          value: "6379"
        - name: REDIS_CA_SECRET_ID
          value: "${CA_SECRET_ID}"
EOF

kubectl apply -f /tmp/feast-validation-pod.yaml
kubectl wait --for=condition=Ready pod/feast-redis-validation -n "${NAMESPACE}" --timeout=180s
kubectl logs pod/feast-redis-validation -n "${NAMESPACE}" | grep feast-bootstrap
```

`[feast-bootstrap] code: <sha>`가 보이면 GCS 코드 주입이 성공한 것이다.
파드 GSA에 코드 버킷 `roles/storage.objectViewer`가 없으면 여기서 실패한다.

## 4. 연결 스모크 (dry-run)

```bash
kubectl exec -n "${NAMESPACE}" feast-redis-validation -- \
  python -m autoresearch.jobs.feast_materialize --dry-run
```

기대: `job_summary` `status=succeeded`, `redis_ping: true`, exit 0.
(CA 조달 → IAM token AUTH → TLS PING 전 구간 검증)

## 5. Cluster hash slot 학습 검증 (infra #129 학습 목표)

```bash
kubectl exec -i -n "${NAMESPACE}" feast-redis-validation -- python - <<'PY'
import sys
sys.path.insert(0, "/app")
from autoresearch.jobs.feast_materialize import (
    _ensure_ca_bundle, _load_store, _online_client,
)

_ensure_ca_bundle()
store = _load_store("/app/feature_repo")
client = _online_client(store.config.online_store)

print("PING:", client.ping())
shards = client.execute_command("CLUSTER SHARDS")
print("shard count:", len(shards))

k1, k2, k3 = "feature:{user:100}:age", "feature:{user:100}:watch", "feature:{user:200}:age"
print("same-tag slots equal:", client.keyslot(k1) == client.keyslot(k2))
client.set(k1, "a"); client.set(k2, "b"); client.set(k3, "c")
print("same-tag MGET:", client.execute_command("MGET", k1, k2))
try:
    client.execute_command("MGET", k1, k3)
    print("CROSSSLOT: NOT reproduced")
except Exception as exc:
    print("CROSSSLOT reproduced:", type(exc).__name__)
client.delete(k1, k2, k3)
PY
```

기대: PING True, shard count 2, same-tag slot 일치·MGET 성공, 다른 tag
`CROSSSLOT` 재현.

## 6. feast apply + materialize + 온라인 조회

```bash
kubectl exec -n "${NAMESPACE}" feast-redis-validation -- \
  bash -c "cd /app/feature_repo && feast apply"

kubectl exec -n "${NAMESPACE}" feast-redis-validation -- \
  bash -c "cd /app && python -m autoresearch.jobs.feast_materialize"

kubectl cp scripts/verify_feature_retrieval.py \
  "${NAMESPACE}/feast-redis-validation:/tmp/verify_feature_retrieval.py"
kubectl exec -n "${NAMESPACE}" feast-redis-validation -- \
  bash -c "cd /app && python /tmp/verify_feature_retrieval.py"
```

기대: apply 성공, materialize `status=succeeded` exit 0, 조회 스크립트 `[OK]`
2건(Redis에서 실값 반환).

## 7. 정리

```bash
kubectl delete pod feast-redis-validation -n "${NAMESPACE}"
```

검증에 사용한 임시 리소스와 네트워크 허용 변경이 있었다면 원상복구한다.
성공 로그 요약을 이슈 #148 코멘트로 남긴다.

## 결과 판정

- [ ] dry-run: IAM AUTH + TLS PING 성공 (`redis_ping: true`)
- [ ] `CLUSTER SHARDS` shard count 2
- [ ] same-tag MGET 성공, 다른 slot `CROSSSLOT` 재현
- [ ] materialize `status=succeeded`, exit 0
- [ ] `get_online_features`로 Redis에서 feature 실값 반환

모두 통과하면 이슈 #148의 "GKE 실제 검증"을 완료로 종결한다.
