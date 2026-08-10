"""Streamlit Experiment Workbench의 화면 시각을 KST로 변환한다.

[파이프라인]
Experiment API가 UTC로 기록한 생성·갱신·Event·Log 시각을 사용자 화면에
표시하는 UI 경계다.

[기능]
timezone-aware API 시각을 Asia/Seoul로 명시 변환하고, 컨테이너의 로컬 timezone과
무관한 짧은 화면 문자열을 제공한다.

[비책임]
Experiment API·DB의 UTC 저장 계약, Event·Log 기록, 브라우저별 timezone 선택.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


def format_time(value: datetime) -> str:
    """UTC API 시각을 KST 화면 문자열로 반환한다."""
    return value.astimezone(KST).strftime("%m-%d %H:%M KST")


def format_short_time(value: datetime) -> str:
    """목록 한 줄에 들어갈 짧은 KST 시각을 반환한다.

    `KST` 접미사를 뗀다. 사이드바는 폭이 좁고 같은 화면의 모든 시각이 KST라
    항목마다 세 글자씩 되풀이할 이유가 없다.
    """
    return value.astimezone(KST).strftime("%m-%d %H:%M")
