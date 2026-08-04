# Streamlit 실험 워크벤치 UI v0 설계

## 목적

사용자가 가설 한 줄을 제출하고, Agent Orchestration 실험 API v0가 기록한 실험의
상태, 판단, 지표, 이벤트, 원본 로그를 Streamlit 화면에서 관찰하게 한다. 이 문서는
Issue #484의 UI 범위와 병합된 실험 API v0의 소비 계약을 정의한다.

## 배경과 범위

`#483`은 실험 생성, 상태 전이, 이벤트, 로그, 메타데이터를 저장하고 조회하는
FastAPI API를 제공한다. Streamlit UI는 그 API의 소비자이며, 실행기를 대체하지 않는다.

v0 범위:

- 가설 입력과 `POST /experiments` 호출
- 최근 실험 목록과 선택 상태
- 선택된 실험의 상태, 지표, 메타데이터, 이벤트, 로그 표시
- 1초 polling 기반의 실행 관찰
- API 연결, 인증, 빈 목록, 조회 오류의 명확한 표시

v0 비범위:

- 에이전트 실행, 상태 전이, 이벤트/로그 생성
- `PATCH /experiments/{id}/status`, `POST /events`, `POST /logs`, `POST /promote` 호출
- 기존 `/chat` 계약 변경
- 사용자 인증 또는 권한 모델
- 목 데이터를 실제 실행처럼 표시하는 데모 모드

에이전트 실행기가 아직 이벤트와 로그를 기록하지 않는 환경에서는 새 실험이
`CREATED` 상태로 표시된다. UI는 이 상태를 숨기거나 스스로 진행 상태를 바꾸지 않는다.

## 사용자 흐름

1. 사용자는 시작 화면에서 가설을 입력한다.
2. UI는 `POST /experiments`로 실험을 생성한다.
3. UI는 생성 응답의 ID를 선택 실험으로 저장하고 워크벤치로 전환한다.
4. 워크벤치는 선택된 실험의 상세, 이벤트, 로그를 조회한다.
5. 활성 상태에서는 1초마다 새 이벤트와 로그 및 최신 상세를 갱신한다.
6. 사용자는 좌측 목록에서 과거 실험을 선택해 같은 정보를 조회한다.

## 화면 설계

### 시작 화면

시작 화면은 사용자가 가설만 입력한다는 제품 원칙을 유지한다.

- 상단: `AUTORESEARCH / EXPERIMENT CONSOLE` 제목과 API 연결 상태
- 본문: 큰 가설 입력 상자와 `실험 시작` 버튼
- 보조 안내: 실행 뒤 표시될 단계, 평가 지표, 원본 로그를 짧게 설명
- 유효성: 공백만 있는 입력은 제출할 수 없다.
- 제출 중: 버튼을 비활성화하고 진행 상태를 표시한다.
- 생성 실패: 가설은 유지하고 API 오류 메시지를 입력 상자 아래에 표시한다.

### 워크벤치

데스크톱은 세 열로 구성한다.

| 영역 | 책임 | 표시 내용 |
| --- | --- | --- |
| 좌측 목록 | 실험 탐색과 선택 | 새 가설 버튼, 최근 실험, 상태 배지, 가설 요약, 생성 시각 |
| 중앙 작업 영역 | 선택 실험의 맥락과 상세 관찰 | 전체 가설, 상태 타임라인, 결과/이벤트/원본 로그 탭 |
| 우측 요약 | 실행 상태의 빠른 확인 | 상태, 평가 지표, 메타데이터, 마지막 갱신 시각 |

중앙의 상태 타임라인은 `CREATED -> RUNNING -> EVALUATING -> PASSED 또는 FAILED`를
보이고, `ERROR`, `PROMOTED`는 해당 상태에 맞는 종료 표기로 표시한다. API가 아직
기록하지 않은 이후 단계는 비활성화한다.

탭은 다음 책임으로 고정한다.

- 결과: `metric_summary`를 키-값 카드로 표시하며, 값이 없으면 평가 전 안내를 표시한다.
- 이벤트: Event의 시각, 상태 전이, reason, metric snapshot을 시간순으로 표시한다.
- 원본 로그: Log의 시각, `log_type`, content를 콘솔형 행으로 표시한다.

빈 목록에서는 시작 화면으로 이동할 행동을 제공한다. 선택한 실험이 삭제되었거나
404가 되면 선택을 해제하고 목록을 다시 읽는다.

### 반응형 동작

모바일에서는 좌측 목록을 실험 선택 드롭다운으로 바꾸고, 우측 요약을 중앙 가설과
탭 사이에 세로로 배치한다. 로그 내용은 가로 스크롤이 가능한 고정폭 영역으로 표시한다.

## 상태 표현

| API 상태 | UI 색상 | 사용자 문구 | polling |
| --- | --- | --- | --- |
| `CREATED` | 회색 | 실행 대기 | 계속 |
| `RUNNING` | 파랑 | 에이전트 실행 중 | 계속 |
| `EVALUATING` | 주황 | 지표와 유의성 평가 중 | 계속 |
| `PASSED` | 초록 | 판정 통과, 승격 대기 | 계속 |
| `FAILED` | 빨강 | 실험 가설 미통과 | 마지막 조회 후 중지 |
| `ERROR` | 진한 빨강 | 실행 또는 인프라 오류 | 마지막 조회 후 중지 |
| `PROMOTED` | 청록 | prod 승격 완료 | 마지막 조회 후 중지 |

`PASSED`는 `PROMOTED`로 바뀔 수 있는 비종료 상태이므로 polling을 유지한다.

## API 소비 계약

모든 요청은 `X-Orch-Token`을 포함한다. UI는 API 주소와 토큰을 환경 변수로만 읽고,
브라우저 화면이나 로그에 토큰을 표시하지 않는다.

| 목적 | API | 호출 시점 |
| --- | --- | --- |
| 실험 생성 | `POST /experiments` | 가설 제출 |
| 실험 목록 | `GET /experiments?limit=50&offset=0` | 진입, 제출 성공, 목록 새로고침 |
| 실험 상세 | `GET /experiments/{id}` | 선택, polling |
| 이벤트 | `GET /experiments/{id}/events?after_id=...` | 선택, polling |
| 로그 | `GET /experiments/{id}/logs?after_id=...` | 선택, polling |
| 메타데이터 | `GET /experiments/{id}/metadata` | 새 실험 선택 시 한 번 |

Event와 Log는 API가 주는 `next_cursor`를 다음 `after_id`로 저장한다. 선택을 바꾸면
두 cursor를 비우고, 해당 실험의 처음부터 다시 읽는다. 새 항목은 기존 화면 항목 뒤에
추가한다.

`CREATED`, `RUNNING`, `EVALUATING`, `PASSED`에서는 1초 간격으로 상세/Event/Log를
조회한다. `FAILED`, `ERROR`, `PROMOTED`가 감지되면 Event와 Log를 한 번 더 조회해
마지막 기록을 반영한 뒤 자동 갱신을 멈춘다.

## 오류 처리

- `401`: API 토큰이 없거나 유효하지 않다는 연결 오류를 전역 상태에 표시한다.
- `404`: 선택 실험을 해제하고 목록을 다시 불러온다.
- 네트워크 오류 또는 `5xx`: 마지막으로 받은 화면 데이터는 유지하고, 재시도 가능 안내와
  마지막 오류 시각을 표시한다.
- `422`: 가설 입력 또는 API 계약 오류를 제출 영역에 표시한다.
- cursor 오류 `404`: 해당 이벤트 또는 로그 cursor를 초기화하고 해당 목록을 처음부터
  다시 불러온다.

## 모듈 경계

| 파일 | 책임 |
| --- | --- |
| `agent_orchestration/ui/app.py` | Streamlit 진입점, 화면 전환, fragment polling 조립 |
| `agent_orchestration/ui/client.py` | HTTP 요청, 인증 헤더, API 오류의 UI 예외 변환 |
| `agent_orchestration/ui/types.py` | API JSON을 화면 모델로 변환하는 불변 타입 |
| `agent_orchestration/ui/state.py` | 선택 실험, cursor, 마지막 갱신, API 오류의 session state |
| `agent_orchestration/ui/views.py` | 시작 화면, 목록, 타임라인, 탭, 요약 패널 렌더링 |
| `agent_orchestration/ui/styles.py` | 상태 색상, 배지, 로그 형식 등 표현 규칙 |

`client.py`만 HTTP 세부 구현을 알고, 화면 모듈은 `types.py`의 모델만 사용한다.

## 설정과 실행

- `ORCH_UI_API_BASE_URL`: FastAPI base URL, 기본값은 `http://127.0.0.1:8000`
- `ORCH_UI_API_TOKEN`: API 요청용 공유 토큰, 필수
- `streamlit`: orchestration 의존성 그룹에 추가

실행 명령과 환경 변수는 `agent_orchestration/README.md`와 `.env.example`에 문서화한다.

## 테스트

- API client: 인증 헤더, 요청 경로, 성공 응답 변환, `401`/`404`/`422`/`5xx` 변환
- 상태: 실험 선택 전환, cursor 누적과 초기화, 종료 상태 polling 중지
- view: 빈 목록, 가설 제출 오류, metric summary 없음, Event/Log 표시

실제 서버 연결 없이 HTTP client를 대체하는 단위 테스트를 기본으로 하며, UI는 저장소의
FastAPI API 계약 테스트와 중복하여 서버 동작을 검증하지 않는다.

## 완료 조건

- 사용자는 가설만 입력해 실험을 만들고 생성된 실험을 워크벤치에서 볼 수 있다.
- API가 기록한 상태, 이벤트, 로그, 지표, 메타데이터가 정의된 위치에 표시된다.
- polling은 cursor 기반이며 종료 상태에서 불필요하게 계속되지 않는다.
- UI는 상태·이벤트·로그를 생성하거나 기존 `/chat`을 호출하지 않는다.
- 연결과 조회 실패가 빈 화면이나 traceback 대신 사용자 메시지로 표시된다.
