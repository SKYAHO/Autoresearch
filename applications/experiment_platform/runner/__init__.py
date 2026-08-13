"""Agent Orchestration 비공개 Codex Runner 패키지.

[파이프라인]
API 서버가 비공개 서비스로 전달한 Codex 요청을 실제 CLI 추론으로 연결하는
구간을 담당한다.

[기능]
Runner 설정과 FastAPI 애플리케이션을 제공한다.

[비책임]
외부 API 인증·PostgreSQL 저장(applications.experiment_platform.api), OAuth 자격 증명
프로비저닝과 Kubernetes 배포 구성.
"""

from applications.experiment_platform.runner.app import create_runner_app

__all__ = ["create_runner_app"]
