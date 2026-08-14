"""공개 batch CLI의 `--version` 출력이 폭에 상관없이 한 줄 JSON인지 고정한다(#752).

`batch-contract-v1`은 `--version`이 기계가 읽는 JSON을 낸다고 규정한다. argparse 기본
`action="version"`은 문자열을 `HelpFormatter`에 넘겨 **터미널 폭에 맞춰 줄바꿈**하는데,
TTY가 없는 컨테이너에서는 폭이 80으로 떨어진다. revision이 40자 커밋 SHA면 payload가
80자를 넘어 두 줄로 쪼개지고, 소비자의 `--version | tail -1 | jq`가 깨진다.

릴리스 검증이 실제로 이 방식으로 실패했다 — 그 검증은 #752가 추가했고 첫 실행에서 터졌다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# batch-contract-v1이 규정한 공개 batch 명령 전부.
BATCH_MODULES = (
    "autoresearch.jobs.youtube_trending",
    "autoresearch.jobs.youtube_backfill",
    "autoresearch.jobs.action_log",
    "autoresearch.jobs.action_log_quality",
    "autoresearch.jobs.feature_store_build",
    "autoresearch.jobs.feast_materialize",
    "autoresearch.recommendation.daily_recommendations",
)

# 40자 커밋 SHA. 이보다 짧으면 80폭에서도 줄바꿈이 일어나지 않아 회귀를 놓친다.
_RELEASE_REVISION = "0" * 40


@pytest.mark.parametrize("module", BATCH_MODULES)
def test_version_stays_single_line_json_at_narrow_width(module: str) -> None:
    """폭이 좁아도 `--version`은 한 줄 JSON이어야 한다."""
    environment = {
        **os.environ,
        "AUTORESEARCH_REVISION": _RELEASE_REVISION,
        # TTY 없는 컨테이너가 떨어지는 기본 폭. argparse가 이 값으로 줄바꿈한다.
        "COLUMNS": "80",
    }

    completed = subprocess.run(
        [sys.executable, "-m", module, "--version"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    lines = completed.stdout.splitlines()
    assert len(lines) == 1, f"{module}의 --version이 {len(lines)}줄로 쪼개졌다: {lines}"

    # 소비자(release.yml)가 하는 것과 같은 방식으로 읽는다.
    payload = json.loads(lines[-1])
    assert payload["application_revision"] == _RELEASE_REVISION
    assert payload["contract_version"] == "batch-contract-v1"
