# PASSED 실험 Pull Request 생성 계약 (#689)

## 목적

완주한 실험의 `exp` 브랜치를 `dev`로 향하는 Pull Request로 올린다. 지금은
executor가 커밋·push하고 `report.md`까지 쓴 뒤 아무것도 하지 않아, **결과가
브랜치에만 남고 `dev`에 닿을 경로가 없다.**

## 비목적

- **머지와 `PROMOTED` 전이.** 사람이 판단한다. 이 계약은 "판단할 자리를 만드는 것"까지다
- 리뷰어 지정·라벨·자동 승인
- `main` 반영 — 베이스 브랜치 정책은 #509의 범위다

## 이미 있는 것

없던 조각은 PR 생성 하나뿐이다. 나머지는 갖춰져 있다.

| 조각 | 위치 |
|---|---|
| head 브랜치 | `Experiment.issue_branch` (`exp/<issue>-<slug>`) |
| base 좌표 | `Experiment.base_dev_sha` — 실험 시작 시점의 `dev` tip이 봉인돼 있다 |
| PR 본문 | `Experiment.report_markdown` — Codex #2가 쓴 `report.md`(#639, #644) |
| App 토큰 발급 | `github_app.py` |
| GitHub API 호출 관례 | `github_refs.GitHubRefs` |
| PR 번호 저장 자리 | `experiment_metadata` — `(experiment_id, key)` unique |

## 결정 1 — 필터를 두지 않는다

`PASSED`인 실험은 **전부** PR을 만든다. 지표가 나빠도 만든다.

`docs/specs/2026-08-09-agent-authored-experiment-report.md` §결정 6이 `PASSED`를
"통계적으로 유의한 개선"이 아니라 **"실험이 완주하고 결과가 나왔다"**로 재정의했고,
§결정 1이 통계 게이트를 승격 관문에서 제거했다. 여기서 "좋은 결과만 PR" 같은
조건을 두면 **제거한 게이트를 다른 이름으로 되살리는 것**이다.

가설의 성패는 `report.md`가 서술하고, 머지 여부는 사람이 그것을 읽고 정한다.

## 결정 2 — executor 밖에서 만든다

`candidate-finalizer` 컨테이너에 넣지 않는다. 그 컨테이너에는 **이미 push token과
API token이 함께** 있고 Codex #2가 `--sandbox danger-full-access`로 그 안에서 돈다.
같은 스펙 §결정 3이 그 상태를 "미결 모순"으로 적어 뒀다. 거기에
`Pull requests: write`까지 더하면 그 모순이 넓어진다.

#559 로그 수집기와 같은 방향을 쓴다 — **밖에서 관측하고 밖에서 행동한다.**
Codex가 도는 컨테이너의 권한이 늘지 않는다.

### 관측 대상은 DB다 (로그 수집기와 반대다)

#559는 수집 대상을 Kubernetes에서 찾았다. DB의 `RUNNING`으로 거르면 `EVALUATING`
전환 뒤에도 같은 Job이 계속 도는 구간을 놓치기 때문이다.

이 계약은 반대로 **DB를 본다.** 찾는 것이 "지금 살아 있는 프로세스"가 아니라
"이미 확정된 상태"이고, `PASSED`는 API가 트랜잭션 안에서 기록한 사실이기 때문이다.
Kubernetes에는 그 사실이 없다 — Job은 TTL로 사라진다.

## 결정 3 — 멱등성은 PR 존재 여부가 아니라 기록으로 판정한다

같은 실험을 두 번 처리해도 PR은 하나여야 한다.

**`experiment_metadata`에 `pull_request_number`를 남기고, 그 키가 있으면 건너뛴다.**
`(experiment_id, key)` unique 제약이 있어 동시 실행에서도 두 번째가 실패한다.

GitHub에 "열린 PR이 있는지" 물어보는 방식은 쓰지 않는다. 사람이 PR을 닫으면 다시
만들게 되고, 그건 닫은 사람의 의도를 되돌리는 것이다.

다만 **기록보다 PR 생성이 먼저 일어나므로** 그 사이에 죽으면 기록 없는 PR이 남는다.
그때는 GitHub이 같은 head→base 조합에 이미 PR이 있다고 422로 거부하므로, 그 응답을
정상으로 다루고 **기존 PR 번호를 조회해 기록만 채운다.** 중복 PR은 생기지 않는다.

## 결정 4 — 만들지 않고 넘어가는 정상 상황

오류가 아니다. 사유를 남기고 다음 주기로 간다.

| 상황 | 판정 | 처리 |
|---|---|---|
| `issue_branch`·`report_markdown`이 비어 있음 | 아직 준비 안 됨 | skip |
| `candidate_sha`가 없음 (커밋이 없었다) | 변경 없음 | skip, 기록으로 남겨 재시도하지 않는다 |
| 이미 `pull_request_number`가 있음 | 정상 | skip |
| head와 base가 같음 (GitHub 422 `No commits between`) | 변경 없음 | skip, 위와 같이 기록 |

`candidate_sha`가 없는 경우를 기록으로 남기는 이유는, 그렇지 않으면 그 실험이
**영원히 매 주기 재시도 대상**이 되기 때문이다.

## 오류 분류

사유 코드는 `executor/phase2.py`의 `_safe_failure_reason` 관례를 따라 접미사 없는
`^[a-z][a-z0-9_]*$` 고정 코드로 남긴다. 토큰·응답 본문은 싣지 않는다.

| 상황 | 사유 코드 |
|---|---|
| App 토큰 발급 실패 | `pull_request_token_failed` |
| PR 생성 API 실패 | `pull_request_create_failed` |
| 기록 실패 | `pull_request_record_failed` |
| 그 외 실험 단위 예외 | `experiment_promotion_failed` |

**전체 정책은 fail-open이다.** 한 실험이 실패해도 나머지를 계속 처리하고, PR 생성
실패가 실험 실행을 막지 않는다.

## 필요한 인프라 변경

**`autoresearch-branch-writer` App에 `Pull requests: write`를 추가해야 한다.**
현재 권한은 `Contents: write` 하나뿐이다
(`docs/plans/2026-08-05-experiment-branch-bootstrap-k8s-job-phase1.md`).

조직 App 설정이라 이 저장소만으로 끝나지 않는다. 코드가 준비돼도 이 권한 없이는
PR 생성이 403으로 실패한다.

## 알려진 한계

- **PR 본문이 `report_markdown` 하나에 의존한다.** 리포트가 비어 있으면 PR을 만들지
  않고 기다린다 — 본문 없는 PR보다 낫다
- 리뷰어·라벨을 붙이지 않으므로 PR이 조용히 쌓일 수 있다. 알림 경로는 이 범위 밖이다
- `dev`가 그 사이 움직여 충돌이 나도 PR은 생성된다. 충돌 해소는 사람의 몫이다
