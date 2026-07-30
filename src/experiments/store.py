"""실험 이벤트의 append-only 파일 저장소.

[파이프라인] 에이전트 실험 축의 상태 보존 계층. 에이전트가 보고한 이벤트를
받아 실험별 JSONL 한 파일에 순서대로 쌓고, 조회 시 그대로 읽어 돌려준다.

[제공 기능] 실험 생성(제출 이벤트 기록), 이벤트 append, 실험 단위 이벤트 읽기,
실험 id 목록 제공. 이벤트는 수정·삭제하지 않는다 — 상태는 파생이고 이벤트가
사실이다.

[비책임] 상태 파생·검증(`src/experiments/service.py`), HTTP 계약
(`src/experiments/api.py`), 장기 보관·다중 프로세스 동시성. 이 구현은 단일
프로세스 로컬 디스크 전제이며, GCS·DB 백엔드는 후속 이슈 소관이다(#338 spec).
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from src.experiments.schemas import EventKind, ExperimentEvent

STORE_DIR_ENV = "AUTORESEARCH_EXPERIMENT_STORE_DIR"
DEFAULT_STORE_DIR = "data/experiments"

# 실험 id는 파일명이 되므로 경로 조작 문자를 원천 차단한다(외부 입력 신뢰 금지).
EXPERIMENT_ID_PATTERN = re.compile(r"^exp_\d{8}_[0-9a-f]{8}$")


class ExperimentNotFoundError(LookupError):
    """존재하지 않는 실험 id로 조회·보고를 시도했을 때."""


class InvalidExperimentIdError(ValueError):
    """실험 id 형식이 계약과 다를 때. 경로 조작 입력도 여기서 걸린다."""


def resolve_store_dir(store_dir: str | os.PathLike[str] | None = None) -> Path:
    """저장 루트를 결정한다. 인자 > 환경 변수 > 기본값 순."""
    if store_dir is not None:
        return Path(store_dir)
    return Path(os.environ.get(STORE_DIR_ENV, DEFAULT_STORE_DIR))


def validate_experiment_id(experiment_id: str) -> str:
    """실험 id를 검증해 그대로 돌려준다. 형식 위반은 예외."""
    if not EXPERIMENT_ID_PATTERN.match(experiment_id):
        raise InvalidExperimentIdError(f"실험 id 형식이 올바르지 않습니다: {experiment_id!r}")
    return experiment_id


class JsonlExperimentStore:
    """실험별 JSONL 파일 저장소.

    파일 하나가 실험 하나이고, 한 줄이 이벤트 하나다. seq는 1부터 증가하며
    append 시점에 파일의 기존 줄 수로 결정한다.
    """

    def __init__(self, store_dir: str | os.PathLike[str] | None = None) -> None:
        self._root = resolve_store_dir(store_dir)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, experiment_id: str) -> Path:
        return self._root / f"{validate_experiment_id(experiment_id)}.jsonl"

    def exists(self, experiment_id: str) -> bool:
        return self._path(experiment_id).exists()

    def append(
        self,
        experiment_id: str,
        kind: EventKind,
        payload: dict[str, object],
        *,
        at: datetime | None = None,
        create: bool = False,
    ) -> ExperimentEvent:
        """이벤트 한 건을 append하고 기록된 레코드를 돌려준다.

        `create=False`인데 실험 파일이 없으면 `ExperimentNotFoundError`다 —
        제출 없이 진행 보고가 먼저 오는 경로를 막는다.
        """
        path = self._path(experiment_id)
        if not path.exists():
            if not create:
                raise ExperimentNotFoundError(experiment_id)
            path.parent.mkdir(parents=True, exist_ok=True)
        seq = self._count(path) + 1
        event = ExperimentEvent(
            seq=seq,
            kind=kind,
            at=at if at is not None else datetime.now(UTC),
            payload=payload,
        )
        line = event.model_dump_json() + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        return event

    def read_events(self, experiment_id: str) -> list[ExperimentEvent]:
        """실험의 이벤트 전체를 seq 순으로 읽는다."""
        path = self._path(experiment_id)
        if not path.exists():
            raise ExperimentNotFoundError(experiment_id)
        events: list[ExperimentEvent] = []
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                events.append(ExperimentEvent.model_validate(json.loads(raw)))
        events.sort(key=lambda event: event.seq)
        return events

    def list_experiment_ids(self) -> list[str]:
        """저장된 실험 id를 최신 제출이 뒤로 가도록(id 사전순) 돌려준다."""
        if not self._root.exists():
            return []
        ids = [
            path.stem
            for path in self._root.glob("exp_*.jsonl")
            if EXPERIMENT_ID_PATTERN.match(path.stem)
        ]
        return sorted(ids)

    @staticmethod
    def _count(path: Path) -> int:
        if not path.exists():
            return 0
        with path.open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
