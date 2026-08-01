# Task 4 완료 후보 선택과 dev 자동 병합 구현 보고

## 변경

- `tools/auto_research_issue_branch.py`에 schema version 1 completion candidate
  selector를 추가했습니다. 후보별 exact schema, issue·experiment·base SHA,
  criteria/reproducibility ID, SHA/ID 중복, 유한 Decimal 문자열, artifact/log
  단일 줄, baseline canonical equality를 fail-closed로 검증합니다.
- 주 지표 방향·최소 delta와 선택 guardrail 방향·최대 악화폭으로 적격 여부를
  정하고, 주 지표 최선값 및 candidate SHA 오름차순 동률 규칙을 적용했습니다.
  적격 후보가 없으면 정상 `no_qualified_candidate`를 반환합니다.
- result-set ID는 canonical Decimal 및 candidate SHA 정렬을 사용하므로 후보
  입력 순서와 동치 Decimal 표기에 독립적입니다.
- `auto-research-dev-promotion.yml`은 completion dispatch와 동등한 수동 입력을
  받고, trusted issue-branch marker를 검증한 뒤 모든 candidate의 baseline
  descendant와 issue-branch ancestor compare를 끝낸 후 selector를 호출합니다.
  selector가 다시 계산한 criteria/reproducibility ID도 marker 기록값과 비교해
  Issue Form 변경을 fail-closed 처리합니다.
  merge base는 `dev`로 고정했으며 ref/PR API를 사용하지 않습니다.
- issue+experiment result marker는 같은 result-set을 no-op 처리하고 다른
  result-set은 fail-closed합니다. 같은 이슈의 다른 experiment marker는
  분리해 허용합니다. no-qualified, merge 거절, API 실패, 병합 성공을 source
  issue의 idempotent marker comment로 기록합니다.

## RED

- selector import 전 `uv run python -m pytest tests/test_auto_research_issue_branch.py -v`가
  `select_best_candidate` ImportError로 실패함을 확인했습니다.
- non-object candidate schema test는 최초에 `unknown candidate keys`로 실패함을
  확인하고, object type fail-closed 검증을 추가했습니다.

## GREEN 및 검증

- `uv run python -m pytest tests/test_auto_research_issue_branch.py -v` — 99 passed
- `uv run --no-sync ruff check tools/auto_research_issue_branch.py tests/test_auto_research_issue_branch.py` — passed
- `ruby -e "require 'yaml'; YAML.load_file('.github/workflows/auto-research-dev-promotion.yml')"` — passed
- `git diff --check` — passed
- `actionlint`은 로컬에 설치되어 있지 않아 실행하지 못했습니다.

## 잔여 리스크

- `repository_dispatch` 실행은 workflow 정의가 기본 branch에 반영된 뒤에만
  GitHub에서 수신됩니다. 실제 GitHub event payload와 merge API 결과는 로컬
  검증 범위를 벗어나므로 PR CI 및 controlled dispatch로 확인해야 합니다.
