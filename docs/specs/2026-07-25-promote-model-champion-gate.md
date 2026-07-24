# promote-model CLI — champion 승격 게이트 (#342)

> 작성: 2026-07-25 | 상태: 설계(리뷰 대기) | 선행: Autoresearch-airflow#137, #302(PR #334), #300

## 목표

`Autoresearch-airflow`가 준비 중인 `ctr_model_promote` DAG(Autoresearch-airflow#137)가
KubernetesPodOperator로 호출할 `promote-model` CLI 서브커맨드를 신설한다. 사람 개입
없이 champion alias를 자동 교체하되, 지표가 떨어지거나 downsampling 페어링이 깨진
후보는 승격을 거부하는 조건부 게이트 역할을 한다.

Airflow는 이 CLI를 실행하는 것 외에 아무 판정 로직도 갖지 않는다(이 프로젝트의
저장소 경계 원칙 — 애플리케이션 로직은 전부 이 저장소). 게이트 판정은 전부 이
저장소 안에 있어야 한다.

## CLI 계약 (Airflow DAG가 의존 — 임의 변경 금지)

```text
python -m src.cli promote-model \
  --model-name ctr-model \
  --champion-alias champion \
  --calibration-model-name ctr-calibration-model
```

- 세 옵션 모두 `config.yaml`의 기존 `registry.*` 기본값과 이름이 일치하는 리터럴
  기본값을 갖는다(`ctr-model` / `champion` / `ctr-calibration-model`). DAG는 항상
  세 값을 명시로 넘기므로, 기본값이 `config.yaml`과 어긋나도 DAG 동작에는 영향이
  없지만 사람이 수동 실행할 때 혼선을 막기 위해 일치시킨다.
- `MLFLOW_TRACKING_URI`는 CLI 인자가 아니라 `train-model`/`run-pipeline`과 동일하게
  환경변수로 읽는다(`os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")`,
  `src/tracking/client.set_tracking_uri`로 적용).
- **종료 코드:** `0` = 승격 완료 또는 평가할 신규 후보 없음(no-op). `0`이 아니면
  게이트 미달 또는 실행 중 에러 — Airflow는 둘 다 표준 실패 알림으로 처리하므로
  종료 코드 자체를 구분하지 않는다. 대신 stderr 메시지 접두어로 사람이 구분한다:
  `[게이트 미달]` vs `[에러]`.
- stdout에 비교 대상 버전·지표 값·통과/거부 사유를 최소 한 줄 남긴다.

## 게이트 조건 (모두 통과해야 승격)

1. **지표 게이트:** 신규 후보 버전의 held-out 지표(`val_roc_auc`)가 현재
   `champion` alias 버전 값 **이상**.
   - champion alias가 아직 한 번도 설정되지 않은 경우(최초 배포) 비교 대상이
     없으므로 **자동 통과**한다. 이 CLI로 최초 champion 지정도 가능하게 하려는
     의도적 결정이다.
2. **downsampling 페어링 게이트:** 후보 버전이 downsampling(`sampling_rate<1.0`
     tag)으로 학습됐으면, 짝 calibration 모델(`ctr-calibration-model`)에 후보와
     같은 학습에서 나온 버전(`main_run_id` tag가 후보의 run_id와 일치)이 등록돼
     있어야 한다. 서빙 쪽 `_resolve_paired_calibration_run_id`
     (`src/serving/model_loader.py`)가 alias resolve 시점에 하는 검증과 같은
     불변식을 승격 **전에** 선제 확인한다. 조회 방향이 다르므로(저쪽은 "이미
     alias된 조합이 맞는가", 이쪽은 "alias 걸기 전에 짝이 존재하는가") 직접
     재사용하지 않고 경량 재구현한다.

## 후보 버전 판별

`registry.get_latest_version(model_name)`으로 가장 큰 버전 번호를 "신규 후보"로
본다. 이 버전이 이미 champion alias가 가리키는 버전과 같으면(또는 애초에 등록된
버전이 없으면) "평가할 신규 후보 없음" → no-op(exit 0).

명시적 challenger/candidate alias 같은 별도 태깅 인프라는 도입하지 않는다(현재
없고, 이번 게이트 목적에 불필요한 과설계).

## 설계

### 모듈 위치

`src/tracking/promote.py` 신설. `registry.py`/`client.py`/`logger.py`와 같은
tracking 도메인에 둔다. `src/cli.py`는 다른 서브커맨드와 동일하게 얇게
`promote.main()`을 호출만 한다.

### 게이트 판정 흐름 (`promote.main`)

```python
def main(model_name: str, champion_alias: str, calibration_model_name: str) -> str | None:
    """게이트 통과 시 champion(+ 짝 calibration) alias를 후보 버전으로 옮긴다.

    반환값은 승격된 후보 버전 문자열, 평가할 후보가 없으면 None(no-op).

    Raises:
        GateRejectedError: 게이트 조건 미달로 승격 거부.
        (기타) 실행 중 인프라/설정 오류는 그대로 전파한다.
    """
```

1. `candidate_version = registry.get_latest_version(model_name)`; None이면
   등록된 버전 자체가 없음 → `None` 반환(no-op).
2. `client.get_model_version(model_name, candidate_version)`으로 후보의
   `run_id`·`tags`(sampling_rate 포함) 확보.
3. `registry.get_model_versions(model_name)`에서 `champion_alias`를 가진
   버전을 찾는다(있으면 champion 버전 번호). 후보 버전과 같으면 → `None`
   반환(no-op, 이미 champion).
4. `registry.get_model_metrics_by_alias(model_name, champion_alias)`로 champion
   run 지표(dict 또는 champion 없으면 None) 확보.
5. **게이트 1:** champion 지표가 있고 `candidate.val_roc_auc <
   champion.val_roc_auc`면 `GateRejectedError`. candidate 지표는
   `client.get_run(candidate.run_id).data.metrics["val_roc_auc"]`(run 원본 float,
   champion 쪽과 정밀도를 맞추기 위해 버전 tag의 반올림 문자열 대신 사용)로 읽는다.
6. **게이트 2:** `sampling_rate = float(candidate.tags.get("sampling_rate", 1.0))`가
   1.0 미만이면, `calibration_model_name`의 전체 버전 중 `main_run_id` tag가
   candidate의 run_id와 일치하는 버전을 찾는다(`client.search_model_versions`
   클라이언트 사이드 필터, `registry.get_model_versions`와 동일 패턴). 못 찾으면
   `GateRejectedError`. 찾으면 그 버전 번호를 기억해 둔다.
7. `registry.set_model_alias(model_name, champion_alias, candidate_version)`.
8. 게이트 2에서 짝 calibration 버전을 찾았으면
   `registry.set_model_alias(calibration_model_name, champion_alias,
   calibration_version)`도 **같은 alias 이름**으로 실행한다(model_loader가
   `calibration_alias` 미지정 시 main alias를 그대로 쓰는 것과 대칭 — 안 맞추면
   서빙 페어링 검증이 깨진다).
9. `candidate_version` 반환.

`GateRejectedError(RuntimeError)`: 게이트 미달 사유를 담는 전용 예외. 그 외
예외(MLflow 연결 실패 등)는 그대로 전파해 "에러"로 구분한다.

### CLI 어댑터 (`src/cli.py`)

```python
@app.command()
def promote_model(
    model_name: str = typer.Option("ctr-model", help="..."),
    champion_alias: str = typer.Option("champion", help="..."),
    calibration_model_name: str = typer.Option("ctr-calibration-model", help="..."),
) -> None:
    """게이트 통과 시 신규 후보 버전을 champion alias로 승격."""
    try:
        promoted_version = promote.main(
            model_name=model_name,
            champion_alias=champion_alias,
            calibration_model_name=calibration_model_name,
        )
    except promote.GateRejectedError as exc:
        typer.echo(f"[게이트 미달] {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"[에러] promote-model 실행 중 오류: {exc}", err=True)
        raise typer.Exit(code=1)

    if promoted_version is None:
        typer.echo("평가할 신규 후보 버전 없음 — no-op")
    else:
        typer.echo(f"[OK] {model_name} v{promoted_version} -> @{champion_alias} 승격 완료")
```

typer는 함수명 `promote_model`을 커맨드 이름 `promote-model`로 자동 변환한다
(기존 `train_model` → `train-model`과 동일 패턴).

### 기존 안전장치와의 상호작용 (설계 변경 없음, 운영 참고용)

`registry.set_model_alias`는 이미 `CTR_SERVING_CALIBRATION_READY` env가
`true`가 아니면 downsampling 모델의 champion 승격 자체를 거부한다(#300 순서
가드). 이번 게이트 2를 통과해도 이 env가 배포 환경(Airflow Pod)에 세팅돼
있지 않으면 최종 `set_model_alias` 호출에서 막힌다 — 기존 안전장치이며 이번
구현에서 건드리지 않는다.

## 테스트 계획

`tests/test_tracking_promote.py` 신설. `tests/test_tracking_registry.py`와
동일하게 `MlflowClient`를 `MagicMock`으로 monkeypatch(실제 MLflow 서버 불필요).

- **정상 케이스:** 지표가 더 좋은 신규 후보 버전이 champion으로 승격됨(반환값 확인).
- **지표 하락 거부:** 후보 `val_roc_auc` < champion `val_roc_auc` → `GateRejectedError`,
  `set_registered_model_alias` 호출 안 됨.
- **downsampling 페어링 불일치 거부:** 후보가 downsampling인데 매칭되는
  calibration 버전이 없음(또는 `main_run_id` 불일치) → `GateRejectedError`,
  alias 호출 안 됨.
- **downsampling 페어링 성공:** 매칭되는 calibration 버전이 있으면 main과
  calibration 양쪽에 동일 alias가 걸림(2회 `set_model_alias` 호출 검증).
- **no-op — 후보 없음:** 등록된 버전이 없음, 또는 최신 버전이 이미 champion →
  `None` 반환, alias 호출 안 됨.
- **부트스트랩:** champion alias 미설정 상태에서 후보가 자동 통과해 승격됨.
- **CLI 어댑터:** `tests/test_cli.py`에 추가 — `promote.main`을 monkeypatch해
  `GateRejectedError`/일반 예외/no-op/성공 네 경로의 exit code와 메시지 접두어
  (`[게이트 미달]`/`[에러]`) 검증.

## 범위 밖

- `Autoresearch-airflow` DAG 배선(Autoresearch-airflow#137, 별도 저장소).
- `CTR_SERVING_CALIBRATION_READY` env 값 자체를 이 CLI가 설정하거나 우회하는 것.
- champion 외 alias(challenger/rollback 등) 운영 워크플로우.
- 후보 버전을 최신 번호가 아닌 다른 기준(명시적 candidate 태깅 등)으로 고르는 것.

## 롤백

`promote-model` 서브커맨드와 `src/tracking/promote.py` 삭제. 기존 `register_model`/
`set_model_alias`/`get_model_metrics_by_alias`는 변경하지 않으므로 다른 경로에
영향 없음.

## 완료 기준

- `promote-model` 서브커맨드가 이슈 #342의 CLI 계약(옵션 이름/기본값, 환경변수,
  종료 코드 규칙)대로 동작한다.
- 지표가 더 좋은 신규 후보가 정상 승격된다.
- 지표가 떨어지는 후보는 승격이 거부된다(게이트 1).
- downsampling 페어링이 어긋난 후보는 승격이 거부된다(게이트 2).
- downsampling 페어링이 맞는 후보는 main과 calibration 양쪽 alias가 함께 옮겨진다.
