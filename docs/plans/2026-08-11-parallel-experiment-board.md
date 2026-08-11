# 병렬 실험 실행 현황 보드 — 구현 계획

> 이슈: #671 | spec: `docs/specs/2026-08-11-parallel-experiment-board.md`
> 브랜치: `feat/671-parallel-experiment-board`

데모(`scratchpad/demo/board.py`, 커밋하지 않음)로 화면을 확인했고 카드 구성과 탭
구분은 합의됐다. 이 계획은 그것을 `agent_orchestration/ui`에 옮기는 순서다.

## 1. 단계 표 (`models.py`)

- [x] `EXECUTOR_STAGES: tuple[tuple[str, str], ...]` — init 컨테이너 이름과 화면 이름
      7쌍. spec의 표가 정본이다.
- [x] `stage_label(log_type) -> str | None`, `stage_index(log_type) -> int | None`
- [x] 표에 없는 `log_type`(에이전트가 만든 임의 값)은 `None`을 돌려 카드가 단계
      표시를 생략하게 한다 — 목록에 없는 값을 7단계 어딘가로 우겨넣지 않는다.

## 2. 보드 상태 (`state.py`)

- [x] `WorkbenchView`에 `BOARD` 추가.
- [x] `WorkbenchState`에 두 필드:
      `board_stages: dict[str, str]` (실험 id → 최신 `log_type`),
      `board_log_cursors: dict[str, str | None]`.
- [x] `show_board(state)`.
- [x] `record_board_stage(state, experiment_id, log_type, cursor)` — 두 dict를 함께
      갱신하는 단일 진입점.
- [x] `forget_board_entry(state, experiment_id)` — 종료된 실험의 cursor·단계를
      버린다. 안 버리면 dict가 실험 수만큼 무한히 자란다.

**주의:** `board_log_cursors`는 `WorkbenchState.log_cursor`와 별개다(spec 결정 4).
`select_experiment`가 상세 cursor를 비울 때 보드 cursor를 건드리지 않는지 확인한다.

## 3. 보드 렌더링 (`views.py`)

- [x] `render_board(state, *, on_open) -> str | None` — 열려는 실험 id를 반환한다.
      화면 전환은 `app.py`가 한다(views는 상태를 바꾸지 않는다는 기존 경계 유지).
- [x] 상단 지표 3개(`동시 실행 중` / `슬롯 대기` / `완료`).
- [x] 탭 3개. 탭 라벨에 건수를 넣는다.
- [x] `_render_board_card(...)` — 상태 배지, 이슈 번호·경과, 단계 이름·`단계 N/7`,
      진행바, 가설 요약, `상세 보기` 버튼.
- [x] 경과 시간 헬퍼는 `time.py`에 둔다(`format_elapsed`).
- [x] 카드 버튼 key는 실험 id로 고정한다 — 인덱스로 만들면 목록 순서가 바뀔 때
      Streamlit이 이전 클릭 상태를 엉뚱한 카드에 붙인다.

## 4. 화면 배선 (`app.py`)

- [x] sidebar에 `실행 현황` 진입 버튼.
- [x] `WorkbenchView.BOARD` 분기. 보드도 `@st.fragment(run_every="5s")`.
- [x] `refresh_board(client, state)` — `list_experiments()` 1회 + 비종료 실험별
      로그 증분 조회. 종료된 실험은 `forget_board_entry`.
- [x] 카드 버튼이 반환한 id로 `show_experiment()` 후 **`st.rerun(scope="app")`**
      (spec 결정 3). 기본 `st.rerun()`이면 fragment만 돌아 화면이 안 바뀐다.
- [x] 목록 조회 실패는 기존 `record_list_error`로 흘린다. 단계 조회 실패는
      **보드 전체를 죽이지 않는다** — 그 카드만 단계 표시를 비운다(리포트 조회를
      `report_error`로 격리한 것과 같은 이유).

## 5. 테스트

- [x] `tests/test_ui_board.py`
  - [x] `stage_label`/`stage_index`가 7단계를 정확히 매핑하고 미지 값에 `None`
  - [x] 상태 분류: `POLLING_STATUSES` → 실행 중, `CREATED` → 대기, 나머지 → 완료
  - [x] `record_board_stage`가 cursor와 단계를 함께 갱신
  - [x] `forget_board_entry`가 종료 실험의 항목을 지운다
  - [x] 보드 cursor 갱신이 `state.log_cursor`를 건드리지 않는다 (결정 4 회귀 방지)
  - [x] 단계 조회가 실패해도 보드가 렌더링된다
- [x] `AppTest`로 보드 화면이 예외 없이 뜨고 탭 3개가 존재하는지
- [x] 기존 UI 테스트 회귀 확인

## 6. 검증

- [ ] `uv run python -m pytest`
- [ ] `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`
- [ ] 실제 API에 붙여 화면 확인. **배치가 끝나면 실행 중이 0이 되므로** 새 배치가
      도는 동안 캡처한다(데모에서 이것 때문에 한 번 헛돌았다).
- [ ] 브라우저 계산 스타일로 카드가 잘리지 않는지 확인(#657에서 세 번 당한 종류)

## 7. 마무리

- [x] 모듈 docstring 갱신(`state.py`, `views.py`, `app.py`의 [기능]/[비책임])
- [ ] 구현 완료 후 spec/plan을 `docs/archive/`로 이동 — `docs/README.md` 규칙

## 범위 밖 — 후속

- **다중 가설 제출**(이슈 #671의 나머지 절반). 보드가 먼저다.
- 완료 탭의 지표 표시. 실제로 써 본 뒤 정한다(spec 미결).
- 카드 전체 클릭. custom component가 필요해 지금은 버튼으로 간다(spec 결정 2).
