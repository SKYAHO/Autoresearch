"""Redis TLS CA 번들을 Secret Manager에서 고정 경로로 내려받는다.

[파이프라인] 피처 구간 — `feast apply`를 실행하는 GKE Job(#346)이 feast CLI를
띄우기 **직전**에 TLS CA를 조달하는 구간을 담당한다.

[기능] ``feature_repo.bootstrap.download_redis_ca_bundle``을 CLI로 감싼다.
`feature_store.yaml`의 ``tls_ca_cert_path: ${REDIS_TLS_CA_PATH}``는 파일이
이미 존재해야 하는데, feast 공식 CLI에는 CA를 조달하는 훅이 없다. Job이
``fetch_redis_ca.py <path> && feast apply`` 순서로 실행해 그 간극을 메운다.

[비책임] Redis 연결·IAM 인증은 ``feature_repo/redis_iam.py``가, Job 매니페스트와
env 주입은 ``deploy/feast/apply-job.yaml``과 `.github/workflows/feast-apply.yml`이
소유한다.

사용법:
  python scripts/fetch_redis_ca.py /tmp/redis-ca.pem

필요 환경 변수: ``REDIS_CA_SECRET_ID``, ``GCP_PROJECT_ID``
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(
            "사용법: python scripts/fetch_redis_ca.py <destination-path>",
            file=sys.stderr,
        )
        return 2

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from feature_repo.bootstrap import download_redis_ca_bundle

    path = download_redis_ca_bundle(args[0])
    print(f"[redis-ca] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
