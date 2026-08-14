"""공개 batch CLI의 `--version` 출력을 기계가 읽을 수 있는 한 줄 JSON으로 고정한다.

[파이프라인] 이 모듈은 파이프라인 단계를 담당하지 않는다. `autoresearch/jobs/*`와
`autoresearch/recommendation/daily_recommendations.py`가 공유하는 argparse 어댑터다.

[기능] `--version` 액션 하나를 제공한다. `batch-contract-v1`이 규정한
`{"application_revision": ..., "contract_version": ...}` payload를 **줄바꿈 없이** 찍는다.

[비책임] revision 값의 출처(`AUTORESEARCH_REVISION`)와 계약 버전 상수는 각 모듈이
소유한다. 이 모듈은 출력 형태만 책임진다.

**왜 argparse 기본 동작을 쓰지 않는가.** `action="version"`은 문자열을 `HelpFormatter`에
넘기고, formatter는 터미널 폭에 맞춰 **줄바꿈한다**. 폭은 `shutil.get_terminal_size()`가
정하는데 TTY가 없는 컨테이너에서는 `COLUMNS` 환경변수가 없으면 80으로 떨어진다. revision이
40자 커밋 SHA면 payload가 80자를 넘어 두 줄로 쪼개지고, 소비자의
`--version | tail -1 | jq`가 깨진다(#752 릴리스 검증이 이렇게 실패했다).

계약은 "JSON을 출력한다"이므로 폭에 따라 형태가 달라져서는 안 된다. 그래서 formatter를
거치지 않고 직접 찍는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Final, Sequence


class BatchVersionAction(argparse.Action):
    """`batch-contract-v1` version payload를 한 줄 JSON으로 출력하고 종료한다."""

    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str = argparse.SUPPRESS,
        *,
        application_revision: str,
        contract_version: str,
        help: str = "application revision과 batch contract version을 출력하고 종료합니다",
    ) -> None:
        super().__init__(option_strings=list(option_strings), dest=dest, nargs=0, help=help)
        self._payload: Final = {
            "application_revision": application_revision,
            "contract_version": contract_version,
        }

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        """`print`로 직접 찍는다 — formatter를 거치면 폭에 따라 줄바꿈된다."""
        print(json.dumps(self._payload, sort_keys=True), file=sys.stdout)
        parser.exit()


def add_version_argument(
    parser: argparse.ArgumentParser,
    *,
    application_revision: str,
    contract_version: str,
) -> None:
    """`--version`을 계약 형태로 등록한다."""
    parser.add_argument(
        "--version",
        action=BatchVersionAction,
        application_revision=application_revision,
        contract_version=contract_version,
    )
