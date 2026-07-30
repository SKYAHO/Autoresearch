"""Agent Orchestration 실행 계층.

담당 구간:
- FastAPI 앱 라우팅 엔트리(`main.py`) 조립과 런타임 부트스트랩.
- OpenAI 호출, 요청 검증, 저장 경로를 연결하는 인바운드 적재 파이프라인.

담당하지 않는 구간:
- 클라우드 인증, Google OAuth, VPC/네트워크 라우팅.
- 대화 세션 확장(사용자별 히스토리, 장기 사용자 상태 관리).
"""

from __future__ import annotations

from .main import app

__all__ = ["app"]
