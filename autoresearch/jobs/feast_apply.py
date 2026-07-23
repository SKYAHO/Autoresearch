"""`feature_repo/`의 Feast 정의를 registry에 반영하는 공개 batch 명령.

[파이프라인] 피처 구간 — offline store feature 테이블 적재
(``autoresearch.jobs.feature_store_build``)와 online store 적재
(``autoresearch.jobs.feast_materialize``) 사이에서, ``feature_repo/``의
Entity·FeatureView·FeatureService 정의를 Feast registry에 반영하는 구간을
담당한다.

[기능] Redis TLS CA 조달과 ``feature_repo`` import 경로 준비를 마친 뒤 Feast
repo config를 읽어 ``apply_total``(기본) 또는 ``plan``(``--dry-run``)을
실행하고, batch-contract-v1 ``job_summary``와 종료 코드로 결과를 노출한다.

지금까지 ``feast apply``는 사람이 ``kubectl exec``로 실행하는 수동 절차로만
존재했고, 그 결과 FeatureView 정의를 바꿔도 registry가 상한 없이 낡은 채로
남았다. 이 명령은 apply를 공개 batch 명령으로 만들어
``feast_online_store_materialize`` DAG가 materialize 직전에 실행할 수 있게 한다.

feast CLI(``python -m feast.cli.cli apply``)를 그대로 쓰지 않는 이유가 이
모듈의 존재 이유다. feast 0.64의 ``apply_total_command``는
``FeastProviderLoginError``를 ``print`` 후 삼켜 **exit 0**으로 끝난다. 인증
실패가 성공으로 보이면 후속 materialize가 낡은 registry로 실행된다. 따라서 이
모듈은 어떤 feast 예외도 삼키지 않으며, 실패는 반드시 0이 아닌 종료 코드로
드러난다. ``apply_total``이 잘못된 project 이름에서 호출하는 ``sys.exit(1)``도
``SystemExit``으로 붙잡아 실패 summary를 남긴다.

이 명령이 담당하지 않는 인접 책임:

- DAG 배선·schedule·retry는 ``SKYAHO/Autoresearch-airflow``가 소유한다.
- offline store feature 테이블 생성은 ``autoresearch.jobs.feature_store_build``,
  online store 적재는 ``autoresearch.jobs.feast_materialize``가 담당한다.
- Entity·FeatureView 정의 자체는 ``feature_repo/``가 소유한다.
- registry teardown(``feast teardown``)은 제공하지 않는다.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Sequence

from autoresearch.jobs import BATCH_CONTRACT_VERSION
from feature_repo.bootstrap import ensure_redis_ca_bundle, ensure_repo_importable

logger = logging.getLogger(__name__)
_REVISION = os.getenv("AUTORESEARCH_REVISION", "unknown")
JOB_NAME = "feast_apply"


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


def _boolean(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        action="version",
        version=json.dumps(
            {
                "application_revision": _REVISION,
                "contract_version": BATCH_CONTRACT_VERSION,
            },
            sort_keys=True,
        ),
    )
    parser.add_argument("--repo-path", default="feature_repo")
    parser.add_argument(
        "--skip-source-validation",
        nargs="?",
        const=True,
        default=False,
        type=_boolean,
    )
    parser.add_argument(
        "--dry-run",
        nargs="?",
        const=True,
        default=False,
        type=_boolean,
    )
    return parser


def _validate_repo_path(repo_path: str) -> Path:
    resolved = Path(repo_path).resolve()
    if not (resolved / "feature_store.yaml").exists():
        raise BatchArgumentError(f"feature_store.yaml not found under {repo_path}")
    return resolved


def _ensure_definitions_importable(repo_path: Path) -> None:
    """repo 디렉터리 자체를 sys.path에 넣어 정의 파일 import를 가능하게 한다.

    feast의 ``parse_repo``는 ``py_path_to_module``로 정의 파일 경로를
    ``os.getcwd()`` 기준 상대 경로에서 module 이름으로 바꾼 뒤
    ``importlib.import_module``을 호출한다. ``apply_total``·``plan``이 먼저
    ``os.chdir(repo_path)``를 하므로 module 이름은 ``definitions``처럼 최상위
    이름이 되고, repo 디렉터리가 sys.path에 없으면 ``ModuleNotFoundError``가
    난다. ``python -m``은 sys.path[0]을 시작 시점 cwd의 절대 경로로 고정하므로
    chdir 뒤 자동으로 해결되지 않는다. feast CLI의 ``cli_check_repo``도 같은
    이유로 repo path를 sys.path에 넣는다.
    """

    path = str(repo_path)
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_repo_config(repo_path: Path) -> Any:
    """feature_store.yaml을 RepoConfig로 읽는다 (``${VAR}`` 치환 포함)."""

    from feast.repo_config import load_repo_config

    return load_repo_config(repo_path, repo_path / "feature_store.yaml")


def _apply_total(
    repo_config: Any, repo_path: Path, skip_source_validation: bool
) -> None:
    from feast.repo_operations import apply_total

    apply_total(repo_config, repo_path, skip_source_validation)


def _plan(repo_config: Any, repo_path: Path, skip_source_validation: bool) -> None:
    from feast.repo_operations import plan

    plan(repo_config, repo_path, skip_source_validation)


@contextlib.contextmanager
def _feast_console() -> Iterator[None]:
    """feast의 사람용 출력을 stderr로 보내고 chdir된 cwd를 되돌린다.

    ``apply_total``·``plan``은 diff와 진행 상황을 stdout에 ``print``·
    ``click.echo``로 쓰고 내부에서 ``os.chdir(repo_path)``를 수행한다. stdout은
    batch-contract-v1의 JSON Lines 전용 채널이므로 사람용 출력은 stderr로
    보내고, 호출 뒤 상대 경로가 깨지지 않도록 원래 cwd를 복원한다.
    """

    origin = Path.cwd()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        with contextlib.suppress(OSError):
            os.chdir(origin)


def _run(args: argparse.Namespace) -> dict[str, object]:
    repo_path = _validate_repo_path(args.repo_path)
    # feature_store.yaml의 ${REDIS_TLS_CA_PATH} 치환은 load_repo_config 안에서
    # 일어나므로 CA 조달과 sys.path 준비가 그 전에 끝나 있어야 한다.
    ensure_redis_ca_bundle()
    ensure_repo_importable(repo_path)
    _ensure_definitions_importable(repo_path)
    repo_config = _load_repo_config(repo_path)

    mode = "plan" if args.dry_run else "apply"
    with _feast_console():
        if args.dry_run:
            _plan(repo_config, repo_path, args.skip_source_validation)
        else:
            _apply_total(repo_config, repo_path, args.skip_source_validation)

    return {
        "status": "succeeded",
        "mode": mode,
        "repo_path": str(repo_path),
        "project": getattr(repo_config, "project", None),
        "skip_source_validation": bool(args.skip_source_validation),
    }


def _emit(payload: dict[str, object]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        flush=True,
    )


def _summary(
    *, status: str, details: dict[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "job_summary",
        "contract_version": BATCH_CONTRACT_VERSION,
        "job": JOB_NAME,
        "status": status,
    }
    if details:
        payload.update(details)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 인자를 검증·실행하고 공개 종료 코드를 반환한다."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except BatchArgumentError as exc:
        logger.error("Invalid feast_apply arguments: %s", exc)
        _emit(
            _summary(
                status="failed", details={"error_type": "invalid_arguments"}
            )
        )
        return 2

    try:
        result = dict(_run(args))
    except BatchArgumentError as exc:
        logger.error("Invalid feast_apply arguments: %s", exc)
        _emit(
            _summary(
                status="failed", details={"error_type": "invalid_arguments"}
            )
        )
        return 2
    except SystemExit as exc:
        # feast의 apply_total은 project 이름이 유효하지 않으면 sys.exit(1)을
        # 호출한다. SystemExit은 Exception이 아니므로 따로 붙잡아 summary를
        # 남기고 실패로 노출한다.
        logger.error("feast_apply exited during feast execution (code=%s)", exc.code)
        _emit(
            _summary(
                status="failed", details={"error_type": "runtime_failure"}
            )
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("feast_apply failed (%s)", type(exc).__name__)
        _emit(
            _summary(
                status="failed", details={"error_type": "runtime_failure"}
            )
        )
        return 1

    status = str(result.pop("status", "succeeded"))
    _emit(_summary(status=status, details=result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
