# Archmap sidecar staleness blocking gate 구현 계획

> **이슈**: #187 Phase 1 Todo 4
> **기준 SHA**: `114e92fec238d572894f6a2df4620da23c463df5`
> **범위**: PR workflow gate, 회귀 테스트, 계약 문서. 서버·schema·runtime은
> 변경하지 않는다.

## 목표

결정론 extractor가 만든 `pr-delta.json`의 `sidecar_stale`를 PR workflow의
blocking step에서 검사한다. stale 설명은 서버 POST와 PR 코멘트보다 먼저
실패하고, clean delta만 기존 외부 동작으로 진행한다.

## 구현 순서와 결과

### 1. RED 회귀 테스트

- [x] `tests/test_archmap_sidecar_cli.py`에서 YAML을 구조적으로 읽는다.
- [x] `git diff --name-status` 수집 단계와 delta의
  `--name-status /tmp/name-status.txt` 전달을 검증한다.
- [x] 같은 `pr-report` job 안에서 name-status < delta < gate < server POST <
  comment 순서를 검증한다.
- [x] gate command와 `continue-on-error` 부재를 검증한다.
- [x] production workflow/documents를 바꾸기 전에 집중 테스트를 실행해
  의도한 2개 RED 실패를 `.omo/evidence/task-4-workflow-docs-red.txt`에
  기록한다.

### 2. Workflow 변경

- [x] merge-base와 head SHA 사이의 `git diff --name-status -z`를
  `/tmp/name-status.txt`에 기록한다. `--numstat -z`와 함께 NUL-delimited
  경로를 사용하여 Unicode/C-style quoting 변형을 막는다.
- [x] 기존 delta command에 `--name-status /tmp/name-status.txt`를 전달한다.
  CLI는 `--name-status -z` 및 `--numstat -z`의 bytes 입력을 파싱한다.
- [x] delta 생성 직후 `python -m tools.archmap check-sidecar --delta
  /tmp/pr-delta.json`을 추가한다.
- [x] gate에 `continue-on-error`를 두지 않는다.
- [x] head-SHA checkout, merge-base, optional server POST, fork comment 동작은
  수정하지 않는다.

### 3. 계약 문서

- [x] parser의 literal-only AST와 `__arch__` binding 규칙을 기록한다.
- [x] stale 대 실제 삭제의 판정 행렬과 rename/copy 예외를 기록한다.
- [x] CLI exit 0/1/2와 stderr 계약을 기록한다.
- [x] workflow gate 순서와 blocking 경계를 기록한다.
- [x] 기존 모듈 41개 backfill은 후속으로 이월한다고 명시한다.
- [x] 삭제 pair의 서버 `validate_report_pair` 제한과 server/schema 비변경
  범위를 명시한다.

## 검증 계획

### 자동 검증

```text
uv run --no-sync python -m pytest tests/test_archmap_sidecar_cli.py -q
uv run --no-sync python -m pytest $(printf '%s\n' tests/test_archmap_*.py) -q
uv run --no-sync ruff check tools/archmap tests/test_archmap_sidecar_cli.py
git diff --check
```

YAML은 `pr-report` job의 parsed step list를 출력하여 name-status < delta <
gate < server POST < comment와 gate의 `continue-on-error` 부재를 확인한다.
`actionlint`가 없으면 그 사실과 정확한 확인 명령을 evidence에 남긴다.
다운로드가 필요하면 공식 `rhysd/actionlint` v1.7.12 Windows amd64 zip만
사용하고 SHA256
`6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9`를 먼저
검증한다.

### 수동 QA

임시 Git 저장소에서 clean, stale, private-only, 신규 public, 실제 `D`,
public-to-private, malformed delta를 실행한다. 각 시나리오는 실제 CLI
return code와 stderr/JSON을 판정한다. 별도 workflow 구조 QA에서는 stale
delta를 gate command에 주입해 exit 1로 멈추며 가상의 external POST marker가
실행되지 않는지 확인한다.

서버 cross-repo 확인은 삭제가 없는 실제 pair에서 세 validator 통과를
확인하고, 삭제 pair에서는 `validate_architecture`와 `validate_pr_delta`가
통과한 뒤 기존 `validate_report_pair`가 head architecture에 없는 삭제
모듈을 이유로 거부하는지 확인한다. 이 제한을 해결하기 위해 서버나 schema를
수정하지 않는다.

## 산출물과 정리

- `.github/workflows/archmap.yml`
- `tests/test_archmap_sidecar_cli.py`
- `docs/specs/2026-07-19-archmap-sidecar-gate.md`
- `docs/plans/2026-07-19-archmap-sidecar-gate.md`
- `.omo/evidence/task-4-workflow-docs-red.txt`
- `.omo/evidence/task-4-workflow-docs.txt`

검증 중 만든 임시 workflow 파싱 파일과 `/tmp/issue187.*` 디렉터리는 QA가
끝난 뒤 삭제한다. 다른 worktree, `C:/PR_report`, 기존 Todo 1-3 WIP는 전후
상태를 비교하고 변경하지 않는다. commit, push, PR 생성은 수행하지 않는다.

## 위험과 대응

| 위험 | 대응 |
|---|---|
| name-status를 2-dot diff로 계산해 base 쪽 변경을 삭제로 오인 | 기존 merge-base step의 SHA를 양쪽 입력에 그대로 사용 |
| stale gate가 외부 부수효과 뒤에 실행 | YAML 구조 테스트와 parsed order QA로 gate 위치를 확인 |
| fork comment 실패 허용 동작을 변경 | 기존 comment step과 `continue-on-error`를 보존 |
| 삭제 pair를 서버가 조립하지 못함 | 기존 report-pair 제한으로 기록하고 server/schema 변경을 후속 범위로 둠 |
| 문서만 green인 misleading success | 실제 CLI exit code, stderr, actionlint, diff check 산출물을 evidence에 기록 |
