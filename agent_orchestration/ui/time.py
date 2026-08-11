"""Streamlit Experiment Workbench의 화면 시각을 KST로 변환한다.

[파이프라인]
Experiment API가 UTC로 기록한 생성·갱신·Event·Log 시각을 사용자 화면에
표시하는 UI 경계다.

[기능]
timezone-aware API 시각을 Asia/Seoul로 명시 변환하고, 컨테이너의 로컬 timezone과
무관한 짧은 화면 문자열을 제공한다. 보드 카드가 쓰는 경과 시간 문구도 여기서 만든다.

[비책임]
Experiment API·DB의 UTC 저장 계약, Event·Log 기록, 브라우저별 timezone 선택.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


def format_time(value: datetime) -> str:
    """UTC API 시각을 KST 화면 문자열로 반환한다."""
    return value.astimezone(KST).strftime("%m-%d %H:%M KST")


def format_elapsed(since: datetime, *, now: datetime | None = None) -> str:
    """지금까지 흐른 시간을 짧은 화면 문구로 반환한다.

    보드 카드는 "언제 시작했나"보다 "얼마나 돌고 있나"를 먼저 묻는 자리다. 절대
    시각은 상세 화면이 이미 보여준다.

    `now`는 테스트가 시각을 고정하려고 넣는다 — 없으면 현재 UTC를 쓴다.
    """
    reference = now if now is not None else datetime.now(timezone.utc)
    minutes = int((reference - since).total_seconds() // 60)
    if minutes < 1:
        return "방금"
    if minutes < 60:
        return f"{minutes}분"
    hours, remainder = divmod(minutes, 60)
    if remainder == 0:
        return f"{hours}시간"
    return f"{hours}시간 {remainder}분"


def format_short_time(value: datetime) -> str:
    """목록 한 줄에 들어갈 짧은 KST 시각을 반환한다.

    `KST` 접미사를 뗀다. 사이드바는 폭이 좁고 같은 화면의 모든 시각이 KST라
    항목마다 세 글자씩 되풀이할 이유가 없다.
    """
    return value.astimezone(KST).strftime("%m-%d %H:%M")
