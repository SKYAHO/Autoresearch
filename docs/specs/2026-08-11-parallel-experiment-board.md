# 병렬 실험 실행 현황 보드

> 이슈: #671 | 작성: 2026-08-11 | 상태: 구현 대기

## 배경

워크벤치는 실험을 **한 번에 하나만** 보여준다. 사이드바에서 고른 실험의 상세만
렌더링하므로, 여러 실험이 동시에 진행돼도 화면에는 언제나 하나뿐이다. 병렬성이
표현될 자리가 없다.

실제로는 이미 병렬로 돈다. 배포된 launcher의 `ORCH_MAX_CONCURRENT_EXPERIMENTS`는
**5**이고, 2026-08-10 17:43 관측에서 `RUNNING` 4건 + `EVALUATING` 1건이 동시에
진행 중이었으며 `CREATED` 1건이 슬롯을 기다리고 있었다. 팀은 이 상황을
`kubectl get pods`로 보고 있다 — 워크벤치가 그 자리를 대신하지 못한다.

Autoresearch의 목표가 자율 실험 에이전트인 이상 "여러 가설이 동시에 검증된다"는
것이 제품의 핵심 장면인데, 현재 UI는 그 장면을 담지 못한다.

## 핵심 발견 — 단계를 API만으로 알 수 있다

executor Job의 init 컨테이너 7개가 곧 진행 단계이고, **로그 수집기(#559)가 모든
로그에 `log_type = init 컨테이너 이름`을 붙인다.**

| # | `log_type` | 화면 이름 |
|---|---|---|
| 1 | `branch-token-minter` | 브랜치 토큰 발급 |
| 2 | `branch-creator` | 브랜치 생성 |
| 3 | `clone-token-minter` | clone 토큰 발급 |
| 4 | `workspace-preparer` | 작업공간 준비 · baseline 학습 |
| 5 | `codex-worker` | 에이전트 구현 |
| 6 | `candidate-verifier` | 후보 검증 · 테스트 |
| 7 | `push-token-minter` | push 토큰 발급 |

**가장 최근 로그의 `log_type`이 곧 현재 단계다.** 쿠버네티스에 붙지 않고 Experiment
API만으로 `kubectl`의 `Init:N/7`과 같은 정보를 얻는다.

`kubectl`의 `Init:N/7`은 "N개 완료"라 N번째(0-based) 컨테이너가 실행 중이라는
뜻이다. 즉 `Init:4/7` ↔ `log_type = codex-worker`(index 4) ↔ 화면 표기 `단계 5/7`.

### 결정 1 — 진행 표시는 Step이 아니라 Log에서 얻는다

실행 중 실험 세 건을 조회한 결과 `steps`는 **전부 0건**이었다(`events` 2~3건,
`logs` 4~18건). Step은 에이전트가 자발적으로 기록하는 값이라 init 단계 구간을
덮지 못한다. 로그는 수집기가 컨테이너 단위로 확실히 채운다.

## 화면 계약

### 탐색

`WorkbenchView`에 세 번째 값 `BOARD`를 추가한다. sidebar에 진입 버튼을 두고,
기존 `CREATE`/`DETAIL`과 같은 방식으로 전환한다.

### 구성

```
동시 실행 중 5        슬롯 대기 1        완료 N

[ 실행 중 5 ] [ 대기 1 ] [ 완료 N ]
```

- **실행 중** — `POLLING_STATUSES`(비종료) 실험. 카드에 진행 단계를 그린다.
- **대기** — `CREATED`. 슬롯을 기다리는 상태임을 문구로 설명한다.
- **완료** — 종료 상태. 최근 N건만 보여준다.

### 카드

| 요소 | 출처 |
|---|---|
| 상태 배지 | `Experiment.status` |
| 이슈 번호 · 경과 시간 | `issue_number`, `created_at` |
| 단계 이름과 진행률 | 최신 로그의 `log_type` |
| 가설 요약 | `hypothesis` 첫 문장 |
| 상세 이동 | 버튼 |

### 결정 2 — 카드 전체 클릭이 아니라 버튼으로 상세를 연다

Streamlit의 `st.container`는 클릭 이벤트를 노출하지 않는다. 카드 전체를 클릭
영역으로 만들려면 custom component가 필요한데, 그 하나 때문에 프론트엔드 빌드
파이프라인을 들이지 않는다. 카드 안에 버튼을 두고 `show_experiment()`를 부른다.

### 결정 3 — 보드에서 상세로 갈 때 `st.rerun(scope="app")`을 쓴다

보드는 `@st.fragment(run_every="5s")` 안에서 렌더링된다. fragment 안의 기본
`st.rerun()`은 **fragment만** 다시 돌리므로 화면 전환이 일어나지 않는다.
전체 앱을 다시 돌려야 `WorkbenchView.DETAIL` 분기로 넘어간다.

## 폴링 계약

- 목록은 `list_experiments()` **한 번**으로 얻는다. 카드 수에 비례하지 않는다.
- 단계는 비종료 실험마다 로그를 조회한다. 로그는 `created_at asc` cursor
  페이지네이션이므로(`repository.py:259`) 최신 한 건만 집는 질의가 없다.
  **보드 상태에 실험별 cursor를 들고 가** 첫 조회 이후에는 증분만 읽는다.
- 로그 본문은 보드 상태에 쌓지 않는다. 실험별로 **최신 `log_type` 하나와 cursor만**
  남긴다. 상세 화면의 `state.logs`와는 별개의 저장소다.

### 결정 4 — 보드 cursor와 상세 cursor를 섞지 않는다

`WorkbenchState.log_cursor`는 선택된 실험 하나의 것이고 상세 화면의 원본 로그 탭이
소유한다. 보드가 그 값을 건드리면 상세 화면이 이미 읽은 로그를 다시 읽거나 건너뛴다.
보드는 `board_log_cursors: dict[str, str | None]`을 따로 갖는다.

## 비책임

- **API 서버를 바꾸지 않는다.** 기존 엔드포인트 조합만 쓴다.
- 동시 실행 상한 조정은 이 저장소 밖이다 — launcher CronJob 매니페스트는
  `SKYAHO/Autoresearch-infra` 소유다. 보드는 상한이 얼마든 "실행 중 N · 대기 M"을
  사실대로 보여준다.
- 쿠버네티스 조회를 하지 않는다. UI는 Experiment API만 본다.

## 미결

- **완료 탭의 정보량.** 지금은 최근 N건 나열이다. PASSED/FAILED/ERROR 분리나 지표
  delta 표시는 실제로 써 본 뒤 정한다.
- **다중 가설 제출**(이슈 #671의 나머지 절반)은 보드가 들어간 뒤 별도로 진행한다.
  보드가 없으면 5개를 제출해도 결과를 볼 화면이 없으므로 순서가 이렇다.
