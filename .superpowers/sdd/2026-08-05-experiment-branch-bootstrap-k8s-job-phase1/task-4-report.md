# Task 4 보고서 — GitHub Actions 제거와 두 runtime image 게시

## 상태

**DONE_WITH_CONCERNS** — 코드·문서·정적 release 계약·전체 테스트는 완료했습니다.
WSL의 Linux Docker CLI/daemon 연동이 없어 executor image build와 두 image의
non-root/read-only smoke를 끝까지 확인하지 못했으므로 container runtime gate는
미완료로 남깁니다.

## 구현 결과

- `.github/workflows/auto-research-issue-branch.yml`을 삭제해 GitHub Actions의
  `createRef`와 `github-actions[bot]` marker 생성 책임을 제거했습니다.
- Issue Form parser, `branch_name_for()`, candidate 선택, descendant 검증,
  `auto-experiment` 분류 label과 promotion label guard는 유지했습니다.
- `deploy/agent_orchestration/launcher.Dockerfile`과
  `deploy/agent_orchestration/executor.Dockerfile`을 추가했습니다. 두 image는 uv
  0.11.26 lock export의 orchestration group, UID/GID 10001, OCI revision label을
  사용하며 Node/Codex를 설치하지 않습니다. executor image에는 기본 branch bootstrap
  command와 command override용 token-minter module이 함께 들어갑니다.
- `release.yml`에 launcher/executor 독립 build/push job을 추가했습니다. 각 job은
  역할별 GAR image를 만들고 digest 형식, source revision, non-root image user,
  read-only module import를 검증한 뒤 immutable `digest_ref`를 출력합니다.
- `.env.example`, README, project/workflow reference, CONTRIBUTING, 관련 활성
  spec/plan 문서의 branch 생성 주체와 역할별 환경 변수·digest handoff를 갱신했습니다.
- Phase 1 executor는 legacy GitHub Actions bot marker를 재현하지 않습니다. 따라서
  marker 없는 Phase 1 branch는 현재 promotion workflow 입력이 아니며 marker
  작성·신뢰 계약 재설계는 후속 gate입니다.
- infra manifest, Secret, RBAC, Terraform, apply 작업은 변경하지 않았습니다.

## TDD 증거

1. 변경 전 회귀 기준:
   `uv run python -m pytest tests/test_auto_experiment_trigger_label.py tests/test_auto_research_issue_branch.py tests/test_agent_orchestration_container.py -v`
   → **137 passed**.
2. 테스트를 먼저 추가한 RED:
   `uv run python -m pytest tests/test_agent_orchestration_container.py -v`
   → **4 failed, 7 passed**. 두 Dockerfile과 두 release job이 아직 없어서 예상대로
   실패했습니다.
3. 구현 후 focused GREEN:
   `uv run python -m pytest tests/test_auto_experiment_trigger_label.py tests/test_auto_research_issue_branch.py tests/test_agent_orchestration_container.py tests/test_branch_protection_contract.py -v`
   → **140 passed**.

## 최종 검증

| 검증 | 결과 |
| --- | --- |
| `uv run python -m pytest` | **2180 passed, 23 skipped**, 121.30초 |
| release/container 구조 테스트 | **21 passed**; `yaml.BaseLoader`로 release YAML을 실제 구조 파싱 |
| `uv run --no-sync ruff check agent_orchestration autoresearch tests tools` | **PASS** |
| `uv lock --check` | **PASS**, 187 packages resolved |
| `git diff --check` | **PASS** |
| `actionlint` | **N/A** — 설치되어 있지 않음 (`which actionlint` exit 1) |
| launcher image build | **PASS 1회** — `autoresearch-launcher:ci` build 완료 |
| executor image build | **미검증** — 완료 결과를 회수하지 않아 PASS로 기록하지 않음 |
| non-root/read-only container smoke | **미검증** |

## 자체 리뷰와 남은 게이트

- workflow 부재 자체나 파일 change detector를 고정하는 테스트는 추가하지 않았습니다.
  대신 release job·Dockerfile 계약을 YAML 구조와 역할별 필드로 검증했습니다.
- 전체 suite 때문에 삭제된 workflow를 직접 읽던 branch-protection 계약 테스트도
  workflow 전용 단언만 제거했고, 유지 중인 dev/promotion/CI 계약은 보존했습니다.
- 자체 리뷰에서 label 위치 개수 설명 1건을 수정한 뒤 해당 테스트·Ruff·diff 검사를
  다시 통과시켰습니다.
- 실제 GAR push, registry digest/revision 검증, infra의 digest 소비는 release/infra
  단계에서만 확인할 수 있어 관찰하지 않았습니다.
- 다음 진행 전 executor image build와 두 image의 UID/GID 10001 + `--read-only`
  module import smoke를 Docker 사용 승인을 받은 환경에서 실행해야 합니다.
