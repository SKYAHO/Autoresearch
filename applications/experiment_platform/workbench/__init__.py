"""Streamlit Experiment Workbench UI package.

[파이프라인]
사용자 가설 제출과 실험 관찰 구간에서 FastAPI Experiment API의 서버 측 소비자 역할을
담당한다. Agent 실행과 GitHub 이슈 발행은 이 패키지의 책임이 아니다.

[기능]
실험 목록, 상태, Event, Log, metadata를 Streamlit 화면에 표시하기 위한 API client,
session state, view 조립을 제공한다. client는 조회뿐 아니라 실험 상태 전이와 실행 Log
기록도 전송한다 — 어떤 상태로 전이할지와 무엇을 기록할지는 호출자가 정한다.

[비책임]
Experiment API의 영속화, 인증 정책, 실행기 스케줄링, GitHub Auto Research 흐름.
"""

