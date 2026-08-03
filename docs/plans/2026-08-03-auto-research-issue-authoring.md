# 자연어 가설 → Auto Research 이슈 발행 경로 (#490)

> 계약 정본: `docs/specs/2026-08-03-auto-research-issue-authoring.md`

## PR 분해

### PR ① 작성 계약과 가이드 (이 PR)

대상: `docs/guides/`, `docs/specs/`, `docs/plans/`, `docs/README.md`,
`tests/fixtures/`, `tests/`

1. **fixture를 실제 렌더에 맞춘다.** `tests/fixtures/auto_research_issue_form_rendered.md`의
   `허용 범위`를 체크박스 3줄로 고친다. 실측으로 `criteria_id`/`reproducibility_id`가
   불변임을 확인했으므로 봉인 재검증에 영향이 없다(spec 결정 4). 이 fixture는
   에이전트의 **작성 예시 정본**이 된다.
2. **작성 가이드를 쓴다.** `docs/guides/auto-research-issue-authoring.md`. 정본이
   아니라 파생물임을 명시하고, spec의 "작성 가이드가 담아야 할 제약"을 사례와 함께
   담는다. `gh issue create` 명령과 label 두 개를 그대로 싣는다.
3. **drift 테스트를 둔다.** 가이드·`_HEADING_NAMES`·`_SCOPE_LABELS`·Issue Form yml이
   어긋나면 실패하게 한다. fixture 3줄 수정 후 ID 불변도 회귀로 고정한다.
4. `docs/README.md` 역할별 인덱스에 가이드를 등재한다 — 현재 `docs/guides/` 아래
   파일이 모두 등재돼 있어 누락하면 이 가이드만 예외가 된다.

**전용 도구를 만들지 않는다**(spec 결정 1). 렌더러·발행 CLI·전용 토큰은 초안에
있었으나 기각했다. 발행 전 검증은 기존 `tools/auto_research_issue_branch.py`를
placeholder 이슈 번호로 실행하면 되고, 새 토큰은 `gh`의 기존 인증 대비 순손해이며,
폭주 방지는 호출 주체가 생기는 시점에 그 주체에 두는 것이 옳다.

### PR ② `Experiment` lineage 스키마

대상: `agent_orchestration/app/experiments/{models,schemas,service}.py`,
`agent_orchestration/migrations/versions/`, 관련 테스트

1. `Experiment`에 `issue_number` / `issue_branch` 컬럼 추가 (nullable).
2. `down_revision = "0001_experiment_tables"`인 Alembic revision 추가.
   `upgrade()`/`downgrade()` 대칭.
3. `ExperimentResponse`에 노출, pydantic으로 값 형식 검증.
4. `models.py` 모듈 docstring을 migration과 맞춘다.
5. `issue_number` index 추가, unique 제약은 두지 않는다.

**주 소비자가 #338이므로 순서를 #338과 조율할 수 있다.**

## 검증 체크리스트

- [ ] fixture의 `허용 범위`가 `_SCOPE_LABELS` 세 줄과 정확히 일치한다
- [ ] fixture 3줄 수정 후에도 `criteria_id`/`reproducibility_id`가 수정 전과 같다
- [ ] `_HEADING_NAMES`가 Issue Form label과 순서까지 일치한다
- [ ] `_SCOPE_LABELS`가 `허용 범위` 옵션과 순서까지 일치한다
- [ ] 가이드가 20개 heading과 scope label 3개를 모두 담고 있다
- [ ] 가이드가 `--label auto-research`와 `--label experiment`를 모두 싣는다
- [ ] 가이드가 스스로 파생물임을 밝히고 정본 두 곳을 가리킨다
- [ ] fixture 수정 후에도 `tests/test_experiment_promotion_gate.py`가 통과한다
      (같은 fixture의 두 번째 소비자)
- [ ] `docs/README.md`에 가이드가 등재되었다
- [ ] 실제로 발행한 이슈 1건이 워크플로를 트리거해 `exp/<issue>-<slug>` 브랜치가
      생성되고, marker 코멘트에 `base_dev_sha` / `criteria_id` /
      `reproducibility_id`가 기록된 것을 확인했다
- [ ] 같은 이슈에 `labeled` 이벤트를 다시 발생시켜도 워크플로가 실패하지 않고
      lineage 재검증만 수행한다 (멱등성)
- [ ] 검수용 발행물이 spec 결정 5의 절차대로 정리되었다
- [ ] Alembic `upgrade head` → `downgrade` → `upgrade` 왕복이 성공한다 (PR ②)
- [ ] `uv run python -m pytest`
- [ ] `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`
- [ ] `git diff --check`

## 관련 이슈

- **#338** — 실험 상태 대시보드. PR ② 가 추가하는 컬럼의 주 소비자다.
- **#454** — 머지 완료. 이 이슈가 만드는 본문이 `criteria_id`/`reproducibility_id`를
  통해 #454의 paired 비교 계약으로 흘러간다.
- **#493** — 판정 엔진 단일화. Issue Form의 시드·지표 필드를 바꾸므로, 가이드의 해당
  제약 서술이 #493 머지 후 갱신 대상이 된다. **착수 순서 조율이 필요하다.**
- **#425**, **#470** — 가이드가 권장 기본값을 제시할 때 참고한다.
- **#467** — 발행 경로를 오케스트레이션 서비스에 두는 선택지를 기각했으므로 직접
  충돌하지 않는다. 다만 자율 실행 주체를 나중에 그쪽에 둔다면 그 시점에 #467의 계약
  서술과 맞춰야 한다.
