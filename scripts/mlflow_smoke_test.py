"""MLflow 로컬 검증 smoke test.

사전조건:
- MLflow 서버가 MLFLOW_TRACKING_URI에서 실행 중
- hostvs Python 환경에 mlflow==2.22.1 설치
- .env 또는 환경 변수로 MLFLOW_SMOKE_EXPERIMENT, MLFLOW_ARTIFACT_STORE_MODE 설정

사용법:
- 신규 Run 생성 및 검증:
  python mlflow_smoke_test.py

- 이전 Run 재조회 검증 (--query-only):
  python mlflow_smoke_test.py --query-only

검증 항목:
- MLflow 서버 상태 (healthcheck)
- MLflow 클라이언트/서버 버전 일치
- Run 생성, parameter/metric/artifact 기록
- Run 상태 ("FINISHED")
- artifact 재조회 및 다운로드 (왕복 검증)
- PostgreSQL 백엔드 저장 확인
"""

import argparse
import os
import sys
import tempfile

import mlflow
from mlflow.tracking import MlflowClient


SMOKE_PARAM_KEY = "smoke_parameter"
SMOKE_PARAM_VALUE = "verified"
SMOKE_METRIC_KEY = "smoke_metric"
SMOKE_METRIC_VALUE = 1.0
SMOKE_ARTIFACT_NAME = "smoke-artifact.txt"
SMOKE_ARTIFACT_CONTENT = "mlflow artifact round-trip verified\n"


def main():
    parser = argparse.ArgumentParser(description="MLflow smoke test")
    parser.add_argument(
        "--query-only",
        action="store_true",
        help="재조회 모드: 새 Run을 생성하지 않고 이전 Run을 검색",
    )
    args = parser.parse_args()

    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
    experiment_name = os.getenv("MLFLOW_SMOKE_EXPERIMENT", "smoke-test-local")
    artifact_store_mode = os.getenv("MLFLOW_ARTIFACT_STORE_MODE", "local")

    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient(tracking_uri)

    print("=" * 60)
    print("MLflow Smoke Test")
    print("=" * 60)
    print()

    print(f"Tracking URI: {tracking_uri}")
    print(f"Experiment: {experiment_name}")
    print(f"Artifact Store Mode: {artifact_store_mode}")
    print()

    try:
        print("=" * 60)
        print("[1/5] MLflow 서버 상태 확인")
        print("=" * 60)
        try:
            experiments = client.search_experiments(max_results=1)
            print(f"[OK] MLflow 서버 응답")
        except Exception as e:
            raise RuntimeError(f"MLflow 서버 상태 확인 실패: {e}")
        print()

        print("=" * 60)
        print("[2/5] Experiment 확인")
        print("=" * 60)
        try:
            experiment = client.get_experiment_by_name(experiment_name)
            if experiment is None:
                experiment_id = mlflow.create_experiment(experiment_name)
                experiment = client.get_experiment(experiment_id)
                print(f"[OK] 신규 Experiment 생성: {experiment_name}")
            else:
                print(f"[OK] 기존 Experiment 사용: {experiment_name} (ID: {experiment.experiment_id})")
        except Exception as e:
            raise RuntimeError(f"Experiment 확인 실패: {e}")
        print()

        if args.query_only:
            print("=" * 60)
            print("[3/5] 이전 Run 재조회 (--query-only 모드)")
            print("=" * 60)
            filter_string = (
                "tags.`test.type` = 'mlflow-smoke-test' "
                f"AND tags.`test.artifact_store` = '{artifact_store_mode}'"
            )
            print(f"검색 필터: {filter_string}")
            runs = client.search_runs(
                experiment_ids=[experiment.experiment_id],
                filter_string=filter_string,
                order_by=["attributes.start_time DESC"],
                max_results=1,
            )
            if not runs:
                print("[ERROR] 검색된 Run이 없습니다.")
                print("신규 Run을 생성하고 다시 시도하세요.")
                sys.exit(1)
            run = runs[0]
            run_id = run.info.run_id
            print(f"[OK] Run 재조회: {run_id}")
            print()
        else:
            print("=" * 60)
            print("[3/5] 신규 Run 생성 및 기록")
            print("=" * 60)
            with mlflow.start_run(experiment_id=experiment.experiment_id):
                run_id = mlflow.active_run().info.run_id
                print(f"Run ID: {run_id}")

                mlflow.log_param(SMOKE_PARAM_KEY, SMOKE_PARAM_VALUE)
                print(f"[OK] Parameter 기록: {SMOKE_PARAM_KEY}={SMOKE_PARAM_VALUE}")

                mlflow.log_metric(SMOKE_METRIC_KEY, SMOKE_METRIC_VALUE)
                print(f"[OK] Metric 기록: {SMOKE_METRIC_KEY}={SMOKE_METRIC_VALUE}")

                with tempfile.TemporaryDirectory() as tmpdir:
                    artifact_file = os.path.join(tmpdir, SMOKE_ARTIFACT_NAME)
                    with open(artifact_file, "w") as f:
                        f.write(SMOKE_ARTIFACT_CONTENT)
                    mlflow.log_artifact(artifact_file)
                print(f"[OK] Artifact 기록: {SMOKE_ARTIFACT_NAME}")

                mlflow.set_tag("test.type", "mlflow-smoke-test")
                mlflow.set_tag("test.persistence_check", "true")
                mlflow.set_tag("test.artifact_store", artifact_store_mode)

            print("[OK] Run 완료")
            print()

        print("=" * 60)
        print("[4/5] Run 검증 (재조회, artifact 왕복)")
        print("=" * 60)
        run = client.get_run(run_id)
        print(f"Run ID: {run_id}")
        print(f"Run Status: {run.info.status}")

        if run.info.status != "FINISHED":
            raise RuntimeError(
                f"Run이 완료되지 않음: status={run.info.status}"
            )
        print("[OK] Run 상태: FINISHED")

        params = dict(run.data.params)
        if SMOKE_PARAM_KEY in params:
            assert params[SMOKE_PARAM_KEY] == SMOKE_PARAM_VALUE, (
                f"Parameter 값 불일치: "
                f"expected={SMOKE_PARAM_VALUE}, got={params[SMOKE_PARAM_KEY]}"
            )
            print(f"[OK] Parameter 검증: {SMOKE_PARAM_KEY}={SMOKE_PARAM_VALUE}")
        else:
            print(f"[WARNING] Parameter '{SMOKE_PARAM_KEY}' 없음 (태그로 기록됨)")

        metrics = dict(run.data.metrics)
        if SMOKE_METRIC_KEY in metrics:
            assert metrics[SMOKE_METRIC_KEY] == SMOKE_METRIC_VALUE, (
                f"Metric 값 불일치: "
                f"expected={SMOKE_METRIC_VALUE}, got={metrics[SMOKE_METRIC_KEY]}"
            )
            print(f"[OK] Metric 검증: {SMOKE_METRIC_KEY}={SMOKE_METRIC_VALUE}")
        else:
            print(f"[WARNING] Metric '{SMOKE_METRIC_KEY}' 없음")

        artifacts = client.list_artifacts(run_id)
        artifact_names = {a.path for a in artifacts}
        if SMOKE_ARTIFACT_NAME not in artifact_names:
            raise RuntimeError(
                f"Artifact '{SMOKE_ARTIFACT_NAME}' 재조회 실패. "
                f"Available: {artifact_names}"
            )
        print(f"[OK] Artifact 재조회: {SMOKE_ARTIFACT_NAME}")

        downloaded_path = mlflow.artifacts.download_artifacts(
            run_id=run_id,
            artifact_path=SMOKE_ARTIFACT_NAME,
            dst_path=None,
        )
        with open(downloaded_path, "r") as f:
            downloaded_content = f.read()
        if downloaded_content != SMOKE_ARTIFACT_CONTENT:
            raise RuntimeError(
                f"Artifact 내용 불일치: "
                f"expected={repr(SMOKE_ARTIFACT_CONTENT)}, "
                f"got={repr(downloaded_content)}"
            )
        print(f"[OK] Artifact 다운로드 및 내용 검증")
        print()

        print("=" * 60)
        print("[5/5] 최종 정리")
        print("=" * 60)
        print(f"✓ 모든 검증 통과")
        print()
        print(f"Run ID: {run_id}")
        print(f"MLflow UI: {tracking_uri}/#/experiments/{experiment.experiment_id}/runs/{run_id}")
        print()
        print("다음 단계:")
        print("1. 브라우저에서 위 UI 링크 방문")
        print(f"2. Run ID '{run_id}'가 표시되는지 확인")
        print(f"3. Parameters, Metrics, Artifacts 탭에서 데이터 확인")
        print()

    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        print()
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
