# Archmap sidecar staleness blocking gate

- **상태**: Approved
- **날짜**: 2026-07-19
- **이슈**: #187 Phase 1 Todo 4
- **관련 구현**: `tools/archmap/sidecar.py`, `tools/archmap/delta.py`,
  `tools/archmap/__main__.py`, `.github/workflows/archmap.yml`

## 목적

PR이 공개 심볼 또는 버전 상수를 바꾸었는데 모듈의 `__arch__` 설명이
갱신되지 않았으면, 리포트를 서버로 전송하거나 PR 코멘트를 쓰기 전에 CI를
차단한다. 계약 사실은 기존 AST 추출기와 `pr-delta.json`이 결정론적으로
판정하고, 이 게이트는 `sidecar_stale` 목록만 읽는다.

이 문서는 Issue #187 Phase 1의 sidecar 파서·staleness 판정·CLI·PR workflow
경계를 정의한다. 코드 품질 또는 버그 리뷰를 대체하지 않는다.

## 1. sidecar parser와 binding 규칙

모듈 설명은 모듈 최상위의 `__arch__` 리터럴 dict 하나로 선언한다. 파서는
소스를 import하거나 실행하지 않고 Python AST만 읽는다.

- 허용 binding은 모듈 최상위의 단일 `ast.Assign`이다. target은 정확히 하나의
  `ast.Name(id="__arch__")`이어야 하며 값은 dict 리터럴이어야 한다.
- 허용 키는 `stage`, `role`, `owns`, `not_owns` 네 개뿐이다. `stage`와
  `role`은 비어 있지 않은 문자열이고, `owns`와 `not_owns`는 중복 없는 비어
  있지 않은 문자열 목록이어야 한다. `stage`는 추출 대상 모듈의 stage와
  일치해야 한다.
- 값은 문자열·목록 리터럴만 허용한다. 호출, 이름 참조, f-string 등 동적
  표현식은 import/eval 없이 전용 `InvalidArchSidecarError`로 거부한다.
- 모듈 scope에서 `AnnAssign`, `AugAssign`, chained assignment, named
  expression, for/comprehension target, with-as, except-as, import alias,
  함수·클래스 이름, match capture 또는 control-flow 내부 assignment가
  `__arch__`를 bind하면 거부한다. 함수·클래스 내부의 같은 이름은 모듈
  sidecar가 아니므로 무시한다.
- sidecar가 없으면 기존 manifest 슬롯인 `role=None`, `owns=[]`,
  `not_owns=[]`를 유지한다. `owns`와 `not_owns`의 목록 순서와 포맷·주석은
  의미값 비교에서 무시한다.

## 2. staleness와 실제 삭제 판정

`pr-delta` 생성은 head/base manifest의 사실과 `git diff --name-status -z`를
함께 사용한다. `--numstat -z`와 함께 NUL-delimited 경로를 사용하여 Git의
C-style quoting에 의한 Unicode 경로 변형을 막는다. 실제 삭제 증거는 상태가
정확히 `D`인 행의 경로만 의미하며,
rename/copy/modify 상태는 삭제로 취급하지 않는다. `sidecar_stale`는 중복
없는 POSIX 경로의 사전순 배열이다.

| 상황 | staleness 결과 |
|---|---|
| 기존 모듈의 public symbol 또는 version constant 변경 + head sidecar 없음 | stale |
| 기존 모듈의 public/version 변경 + sidecar 의미값 동일 | stale |
| 기존 모듈의 public/version 변경 + sidecar 의미값 변경 | fresh |
| 설명의 주석·포맷·`owns`/`not_owns` 순서만 변경 | 의미값 변경 아님 |
| 신규 모듈에 public symbol 또는 version constant 존재 + sidecar 없음 | stale |
| 신규 모듈에 public/version 표면 존재 + 유효 sidecar 존재 | fresh |
| 신규 모듈이 private-only | stale 대상 아님 |
| 파일은 남아 있으나 마지막 public/version 표면이 사라짐 | stale 판정 대상 |
| manifest에서 모듈이 사라지고 name-status가 실제 `D` | 삭제로 증명되어 stale 제외 |
| rename 또는 copy만 보고됨 | 실제 삭제 아님; 일반 staleness 규칙 적용 |

실제 삭제와 manifest 변화는 별개의 사실이다. 따라서 파일이 유지된 채
public 표면만 private로 바뀐 경우에는 삭제 예외를 적용하지 않는다. 반대로
실제 `D` 행으로 증명된 삭제 모듈은 PR delta에 제거 사실을 남기되 sidecar
게이트의 stale 목록에서는 제외한다.

## 3. `check-sidecar` CLI 계약

`python -m tools.archmap check-sidecar --delta <path>`는 전체
`pr-delta` schema를 재검증하지 않고 top-level object와
`sidecar_stale` 필드 경계만 읽는다.

| 조건 | stdout/stderr | exit |
|---|---|---:|
| `sidecar_stale: []` | 무출력 | 0 |
| stale 경로가 있음 | stdout 무출력, stderr에 정렬된 각 경로를 한 번씩 출력 | 1 |
| JSON/UTF-8/I/O 오류, top-level 비object, 필드 누락·null·비배열·비문자열·빈 문자열·중복 | traceback 없는 stderr 진단 | 2 |

입력 오류는 `check-sidecar` 경계에서만 처리한다. 복구 계층이나 서버 호출을
CLI에 추가하지 않는다.

## 4. PR workflow 실행 순서

`pr-report` job은 다음 순서를 지킨다.

```text
head-SHA checkout(fetch-depth: 0)
  -> merge-base 계산
  -> head/base architecture.json 생성
  -> 연결 이슈 조회
  -> git diff --name-status -z <merge-base> <head> > /tmp/name-status.txt
  -> pr-delta.json 생성(--numstat -z, --name-status -z)
  -> check-sidecar --delta /tmp/pr-delta.json  [blocking]
  -> 선택적 POST /api/pr-report               [continue-on-error 유지]
  -> PR 코멘트 upsert                           [fork 동작 유지]
```

게이트 step에는 `continue-on-error`를 두지 않는다. stale이면 그 job은 서버
POST와 PR 코멘트보다 먼저 실패하여 외부 부수효과를 만들지 않는다. head-SHA
checkout, merge-base를 이용한 3-dot 의미론, 서버 URL이 설정된 경우에만 POST하는
조건, fork PR에서 코멘트 쓰기 실패를 허용하는 기존 동작은 유지한다.

## 5. 서버·schema 경계와 삭제 pair 제한

이번 Todo는 서버, `architecture.json`/`pr-delta.json` schema, 런타임
애플리케이션, backfill을 변경하지 않는다. 서버는 CI가 보낸 JSON을 기존
`archmap-v0` 계약으로 검증하고 리포트를 조립한다.

삭제를 포함하지 않는 실제 생성 pair는 `validate_architecture`,
`validate_pr_delta`, `validate_report_pair`의 세 검증을 통과해야 한다.
삭제 pair에는 현재 서버의 알려진 제한이 있다. extractor는 삭제된 모듈의
제거 사실을 `pr-delta.changed_modules` 또는 claim에 담을 수 있지만 head
`architecture.json`에는 그 모듈이 없으므로, 서버의 `validate_report_pair`가
"architecture에 존재하는 모듈" 조건으로 pair를 거부한다. 이 이슈에서는
삭제 pair를 우회하거나 서버를 수정하지 않고, 앞의 두 schema 검증과 이
report-pair 거부를 별도 음성 QA로 기록한다.

## 6. 백필과 후속 범위

기존 모듈 전체의 `__arch__` backfill은 이번 PR의 gate 구현에 포함하지 않는다.
기존 41개 모듈의 설명 추가와 CI sidecar 의무화는 후속 작업으로 이월한다.
현재 게이트는 변경·신규 모듈의 결정론적 public/version 표면을 기준으로
필요한 갱신을 차단하며, 서버·schema·런타임/backfill 변경 없이 동작한다.

## 검증 기준

- YAML 구조에서 `name-status` 수집 < delta < blocking gate < 서버 POST < PR
  코멘트 순서를 확인한다.
- 집중 CLI 테스트, 전체 `tests/test_archmap_*.py` 테스트, Ruff,
  `git diff --check`, actionlint를 실행한다.
- 실제 stale delta를 gate에 넣었을 때 exit 1이 되고 외부 POST 단계에
  도달하지 않는 경로를 수동으로 확인한다.
