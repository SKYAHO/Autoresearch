# 자연어 가설 → Auto Research 이슈 발행 경로 (#490)

> 계약 정본: `docs/specs/2026-08-03-auto-research-issue-authoring.md`

## PR 분해

이슈 지시대로 **PR 2개로 나눈다.** 두 덩어리는 의존이 단방향이고(② 는 ① 이 없어도
성립) 리뷰 성격도 다르다. 하나의 PR에 DB 마이그레이션과 GitHub 토큰 도입을 함께
담지 않는다.

### PR ① 본문 렌더러 + 발행 경로

대상: `tools/`, `docs/guides/`, `docs/README.md`, `.env.example`, `README.md`,
`.claude/docs/agent-project-reference.md`, `tests/`

1. **fixture를 실제 렌더에 맞춘다.** `tests/fixtures/auto_research_issue_form_rendered.md`의
   `허용 범위`를 체크박스 3줄로 고친다. 실측으로 `criteria_id`/`reproducibility_id`가
   불변임을 확인했으므로 봉인 재검증에 영향이 없다(spec 결정 1).
2. **렌더러를 추가한다.** 필드 mapping을 받아 `### ` 본문 문자열을 만드는 **순수
   함수** 하나를 `tools/` 아래 둔다. 파서는 새로 만들지 않는다.
3. **자가 검증을 렌더러 출력에 건다.** 출력을 그대로
   `tools.auto_research_issue_branch.parse_issue_input()`에 넣고, 실패하면 발행
   경로로 넘기지 않는다. 사전 검증은 placeholder 이슈 번호로 한다(spec 결정 5).
4. **발행 CLI를 추가한다.** dry-run 기본값, 1회 실행당 발행 상한, 동일 가설 재발행
   차단 키(spec 결정 4). 발행 시 `auto-research`·`experiment` label을 함께 부여한다.
5. **토큰을 도입한다.** `issues: write`만 필요. `.env.example`에 빈 값 + 주석,
   `README.md`와 `.claude/docs/agent-project-reference.md`를 같은 PR에서 갱신
   (spec 결정 3).
6. **가이드를 쓴다.** `docs/guides/auto-research-issue-authoring.md`. 새 가이드는
   `docs/README.md`의 역할별 인덱스에 **함께 등재한다** — 현재 `docs/guides/` 아래
   15개 파일이 모두 등재돼 있어 누락하면 이 가이드만 예외가 된다. 정본이 아니라
   파생물임을 명시하고 spec의 "작성 템플릿이 지켜야 할 제약"을 사례와 함께 담는다.
7. **검수 발행 1건**을 spec 결정 6의 절차대로 수행한다.

### PR ② `Experiment` lineage 스키마

대상: `agent_orchestration/app/experiments/{models,schemas,service}.py`,
`agent_orchestration/migrations/versions/`, 관련 테스트

1. `Experiment`에 `issue_number` / `issue_branch` 컬럼 추가 (nullable).
2. `down_revision = "0001_experiment_tables"`인 Alembic revision 추가.
   `upgrade()`/`downgrade()` 대칭.
3. `ExperimentResponse`에 노출, pydantic으로 값 형식 검증.
4. `models.py` 모듈 docstring을 migration과 맞춘다.
5. `issue_number` index 추가, unique 제약은 두지 않는다.

**주 소비자가 #338이므로 순서를 #338과 조율할 수 있다.** 컬럼 이름·타입을 정할 때
#338의 요구를 함께 검토한다.

## 검증 체크리스트

- [ ] 렌더러가 만든 본문을 `parse_issue_input()`에 넣는 **왕복 테스트**가 있고,
      fixture와 동등한 본문이 파싱에 성공하며 `criteria_id`/`reproducibility_id`가
      계산된다
- [ ] 계약 위반 입력에 대해 **발행이 일어나지 않고** 실패 사유가 사람이 읽을 수 있게
      보고되는 테스트가 있다 — 알 수 없는 heading, 중복 heading, `허용 범위`에
      체크박스가 아닌 줄, Guardrail 3필드 불일치, 중복 시드,
      `test_size + validation_size >= 1`
- [ ] 가이드 문서와 `_HEADING_NAMES` / `_SCOPE_LABELS` / Issue Form yml이 어긋나면
      실패하는 drift 테스트가 있다
- [ ] 판정기준·재현조건이 같고 가설·피처만 다른 두 본문이 **차단되지 않는다**
- [ ] 가설·피처가 같고 시드만 다른 두 본문이 **차단된다**
- [ ] `docs/README.md`에 새 가이드가 등재되었다
- [ ] fixture 3줄 변경 후에도 `tests/test_experiment_promotion_gate.py`가 통과한다
- [ ] fixture 3줄 수정 후에도 `criteria_id`/`reproducibility_id`가 수정 전과 같음을
      고정하는 회귀 테스트가 있다
- [ ] 실제로 발행한 이슈 1건이 `.github/workflows/auto-research-issue-branch.yml`을
      트리거해 `exp/<issue>-<slug>` 브랜치가 생성되고, `github-actions[bot]`이 남긴
      `<!-- auto-research-issue-branch:v1 -->` marker 코멘트에 `base_dev_sha` /
      `criteria_id` / `reproducibility_id`가 기록된 것을 확인했다 (실행 링크를 코멘트로
      남긴다)
- [ ] 같은 이슈에 `labeled` 이벤트를 다시 발생시켜도 워크플로가 실패하지 않고
      lineage 재검증만 수행한다 (멱등성)
- [ ] 검수용 발행물이 spec 결정 6의 절차대로 정리되었다
- [ ] Alembic `upgrade head` → `downgrade` → `upgrade` 왕복이 성공하고,
      `issue_number` / `issue_branch`가 API 응답에 노출된다
- [ ] 새 환경 변수가 `.env.example`에 빈 값 + 주석으로 등록되고 `README.md`와
      `.claude/docs/agent-project-reference.md`가 같은 PR에서 갱신되었다
- [ ] 토큰 값이 로그·에러 메시지·테스트 fixture 어디에도 남지 않는다
- [ ] `uv run python -m pytest`
- [ ] `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`
- [ ] `git diff --check`

## 관련 이슈

- **#467** — Agent Orchestration `/chat` API 호출 계약 정본화. 발행 경로를
  오케스트레이션 서비스에 두는 후보를 기각했으므로(spec 결정 2) 직접 충돌하지
  않지만, 계약 서술이 어긋나지 않는지 확인한다.
- **#338** — 실험 상태 대시보드. PR ② 가 추가하는 컬럼의 주 소비자다.
- **#454** — 머지 완료. 이 이슈가 만드는 본문이 `criteria_id`/`reproducibility_id`를
  통해 #454의 paired 비교 계약으로 흘러간다.
- **#493** — 판정 엔진 단일화. Issue Form의 시드·지표 필드를 바꾸므로, 가이드 문서의
  해당 제약 서술이 #493 머지 후 갱신 대상이 된다. **착수 순서 조율이 필요하다.**
- **#425**, **#470** — 템플릿이 권장 기본값을 제시할 때 참고한다.
