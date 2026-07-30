"""Autoresearch Agent Orchestration 실험 서비스 패키지.

모듈 책임:
- FastAPI 기반 채팅 API 런타임 진입점(`agent_orchestration.app.main`) 제공합니다.
- LLM 호출 결과를 PostgreSQL에 영속화하기 위한 DB/스키마 유틸리티(`agent_orchestration.app.db`)를 함께 제공합니다.
- 로컬 검증에 필요한 독립 실행 진입점은 별도 배포 코드와 분리해 둡니다.

미구현 책임:
- 사용자 인증·권한 관리(1단계에서는 OpenAI key/server-side secret만 사용)
- 세션/대화 히스토리의 장기 보관 정책(2단계로 이관)
"""

from __future__ import annotations
