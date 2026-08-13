"""Agent Orchestration 런타임 패키지.

[파이프라인]
오케스트레이션 실험 API의 HTTP 진입점, LLM 호출, PostgreSQL 영속화 모듈을
묶는 패키지 경계다.

[기능]
서브모듈 import 경로만 제공하며, 이 패키지 자체는 환경을 읽거나 FastAPI 앱을
생성하지 않는다. 앱 엔트리포인트는 `applications.experiment_platform.api.main:app`이다.

[비책임]
클라우드 인증, Google OAuth, VPC/네트워크 라우팅과 사용자별 대화 세션.
"""
