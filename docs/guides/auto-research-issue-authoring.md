# 가이드 — 자연어 가설로 Auto Research 이슈 작성·발행하기

> **이 문서는 정본이 아니라 파생물입니다.** 필드 정본은
> `.github/ISSUE_TEMPLATE/auto_research.yml`이고, 파싱·검증 계약 정본은
> `tools/auto_research_issue_branch.py`의 `_HEADING_NAMES`, `_REQUIRED_SECTIONS`,
> `_COMPARISONS`, `_SNAPSHOT_REUSE`, `_SCOPE_LABELS`입니다. 이 문서와 정본이
> 어긋나면 **정본이 이깁니다.** 드리프트는
> `tests/test_auto_research_issue_authoring.py`의 drift 테스트가 잡습니다.
>
> 설계 근거는 `docs/specs/2026-08-03-auto-research-issue-authoring.md`입니다.

가설 한 줄에서 시작해 에이전트가 실행할 수 있는 Auto Research 이슈를 만들고,
발행 전에 기존 파서로 자가 검증하는 절차를 설명합니다.

## 흐름

1. 가설을 아래 20개 항목의 값으로 옮긴다 (필드 mapping).
2. `render_issue_body()`로 `### ` 본문을 만든다.
3. 그 출력을 그대로 `parse_issue_input()`에 넣어 자가 검증한다 — CLI가 대신 한다.
4. dry-run으로 본문과 `criteria_id`/`reproducibility_id`를 사람이 확인한다.
5. `--publish`로 발행한다. 이슈가 열리는 순간
   `.github/workflows/auto-research-issue-branch.yml`이 `exp/<이슈번호>-<slug>`
   브랜치를 만들고 marker 코멘트를 남긴다 — **되돌릴 수 없습니다.**

## 필드 mapping

렌더러의 키는 heading 문자열이 아니라 `_HEADING_NAMES`의 **필드 이름**입니다.
heading 순서와 문자열은 정본에서 파생되므로 직접 적지 않습니다.

| 필드 이름 | heading | 필수 | 값 규칙 |
| --- | --- | --- | --- |
| `hypothesis` | `연구 가설` | 필수 | 무엇이 왜 개선되는지. 공백만 있으면 거부 |
| `change` | `변경할 피처 · 모델` | 필수 | 코드로 옮길 수 있을 만큼 구체적으로 |
| `primary_metric_name` | `주 지표 이름` | 필수 | `^[A-Za-z][A-Za-z0-9._-]{0,63}$` |
| `primary_metric_direction` | `주 지표 방향` | 필수 | `higher_is_better` 또는 `lower_is_better` |
| `minimum_primary_delta` | `최소 주 지표 개선폭` | 필수 | 유한한 0 이상 십진수 |
| `guardrail_metric_name` | `Guardrail 지표 이름` | 필수 | 지표 이름 규칙 또는 `없음` |
| `guardrail_metric_direction` | `Guardrail 지표 방향` | 필수 | 위 두 방향 + `not_applicable` |
| `maximum_guardrail_regression` | `최대 Guardrail 악화폭` | 필수 | 유한한 0 이상 십진수 또는 `없음` |
| `secondary_metrics` | `보조 관측 지표` | 선택 | 채우거나 heading 자체를 생략 |
| `comparison` | `비교 대상` | 필수 | 정해진 3개 문자열 중 하나 |
| `dataset_snapshot` | `데이터셋 스냅샷` | 필수 | 1~256자 |
| `random_seeds` | `랜덤 시드 목록` | 필수 | 쉼표로 구분한 중복 없는 0 이상 정수 |
| `split_seed` | `Split 시드` | 필수 | 0 이상 정수 |
| `test_size` | `Test 비율` | 필수 | 0 초과 1 미만 |
| `validation_size` | `Validation 비율` | 필수 | 0 초과 1 미만, `test_size`와 합이 1 미만 |
| `training_config_ref` | `학습 설정 참조` | 필수 | 1~256자 |
| `dataset` | `대상 데이터 · 기간` | 필수 | 데이터셋 경로와 KST 기간 |
| `snapshot_reuse` | `스냅샷 재사용` | 필수 | 정해진 2개 문자열 중 하나 |
| `allowed_scope` | `허용 범위` | 필수 | `fields`가 아니라 전용 인자로 지정 |
| `result` | `결과 (에이전트가 채웁니다)` | 선택 | 실험 종료 후 에이전트가 채움 |

## 지켜야 할 제약

### heading 문자열과 순서

- heading은 `### ` 뒤에 `_HEADING_NAMES` 키와 **완전히 동일한 문자열**이어야
  합니다. 알 수 없는 heading과 중복 heading은 즉시 `ValueError`입니다.
- `변경할 피처 · 모델`과 `대상 데이터 · 기간`의 가운뎃점은 **U+00B7**입니다.
  재타이핑하지 말고 정본에서 복사하세요. 렌더러를 쓰면 이 문제가 없습니다.
- 필드 **값** 안에 `### `로 시작하는 줄을 넣으면 heading이 하나 더 생겨
  본문 구조가 깨집니다. 렌더러가 조립 시점에 거부합니다.

```text
# 나쁜 예 — 값 안에 heading 줄
hypothesis = "가설 요약\n### 연구 가설\n(설명)"   → 중복 heading

# 나쁜 예 — 재타이핑한 가운뎃점
"### 변경할 피처 ㆍ 모델"                          → 알 수 없는 heading
```

### 필수 섹션

- 필수 섹션은 20개 heading에서 `보조 관측 지표`와
  `결과 (에이전트가 채웁니다)` 둘을 뺀 **18개**이며, 값이 공백이면 실패합니다.

### 지표

- `주 지표 이름`과 `Guardrail 지표 이름`은 `^[A-Za-z][A-Za-z0-9._-]{0,63}$`입니다.
  숫자로 시작하거나 공백·한글이 들어가면 거부됩니다.
- `주 지표 방향`은 `higher_is_better` 또는 `lower_is_better`입니다.
  `Guardrail 지표 방향`은 추가로 `not_applicable`을 허용합니다.
- Guardrail을 **쓰지 않으면 세 필드가 함께** `없음` / `not_applicable` / `없음`이어야
  합니다. 하나라도 어긋나면 거부됩니다.

```text
# 좋은 예 — guardrail 미사용
guardrail_metric_name        = "없음"
guardrail_metric_direction   = "not_applicable"
maximum_guardrail_regression = "없음"

# 좋은 예 — guardrail 사용
guardrail_metric_name        = "logloss"
guardrail_metric_direction   = "lower_is_better"
maximum_guardrail_regression = "0.001"

# 나쁜 예 — 세 필드 불일치
guardrail_metric_name        = "logloss"
guardrail_metric_direction   = "not_applicable"   → 거부
```

### 정확한 문자열 옵션

`비교 대상`은 다음 셋 중 하나여야 합니다 (괄호·공백까지 동일).

```text
동일 조건 baseline 재학습 (권장)
champion (ctr-model@champion)
둘 다
```

`스냅샷 재사용`은 다음 둘 중 하나입니다.

```text
허용 (진행하되 실제로 쓴 데이터를 결과에 명시)
불허 (정규 조립 경로 실패 시 중단)
```

### 시드와 split

- `랜덤 시드 목록`은 쉼표로 구분한 **중복 없는** 0 이상 정수이며 ASCII 숫자만
  허용합니다. `Split 시드`도 0 이상 정수입니다.
- `Test 비율`·`Validation 비율`은 각각 0 초과 1 미만이고 **합이 1 미만**이어야
  하며, Decimal과 float 두 경로 모두에서 이 조건을 만족해야 합니다.
- `데이터셋 스냅샷`과 `학습 설정 참조`는 1~256자입니다.

```text
# 나쁜 예
random_seeds    = "42, 42, 43"      → 중복 시드
random_seeds    = "４２"             → 전각 숫자
test_size       = "0.6"
validation_size = "0.5"             → 합이 1 이상, 학습 데이터가 남지 않음
```

### 허용 범위

- `허용 범위` 섹션은 **체크박스 줄만** 포함해야 합니다. 체크박스 3줄 **사이에**
  빈 줄이나 설명 줄을 넣지 마십시오 — 빈 줄 하나라도 거부됩니다. 섹션 앞뒤 개행은
  무관합니다(파서가 섹션 본문을 `strip()`한 뒤 검사하므로 선행·후행 빈 줄은 이미
  제거된 상태입니다).
- 각 줄의 label은 다음 세 문자열과 정확히 일치해야 합니다.

```text
- [ ] prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다
- [ ] Feast 정의(`feature_repo/`) 수정을 허용한다
- [ ] 실험 결과를 champion으로 승격하는 것까지 검토한다
```

- GitHub Issue Form의 `type: checkboxes`는 옵션을 **모두** 렌더하고 **미체크는
  불허**를 뜻하므로, 에이전트가 만드는 본문도 세 줄을 항상 출력합니다.
  렌더러에는 체크할 항목만 scope 키로 넘깁니다:
  `prod_model_contract`, `feast_definition`, `promotion`.

### 선택 섹션

- `보조 관측 지표`는 `required: false`이고 기본값이 없어, 사람이 비워 두면 GitHub이
  `_No response_`를 넣습니다. 파서는 이 섹션을 검증 없이 담으므로 실패하지는
  않지만, 에이전트가 만드는 본문에서는 **채우거나 heading 자체를 생략**합니다.
  렌더러는 값이 없으면 heading을 생략합니다.

### 제목

- 제목은 Issue Form과 같은 `[AR] ` prefix로 시작해야 합니다. 발행 CLI가 이를
  강제합니다.
- `branch_name_for()`는 `[AR] ` prefix를 제거한 뒤 slug를 만들므로, 제목에 ASCII
  영소문자·숫자가 전혀 없으면 slug가 비어 `issue-<sha256 앞 12자>`로 대체됩니다.
  **이는 검증 실패가 아니라 가독성 문제입니다** — 그런 이름도 워크플로의 브랜치
  정규식을 통과합니다. 사람이 브랜치 목록에서 실험을 식별할 수 있도록 제목에
  ASCII slug 조각을 남기세요. CLI는 dry-run 보고에 경고를 출력합니다.

```text
[AR] views_per_day 비율 피처   → exp/501-views-per-day
[AR] 비율 피처 실험            → exp/501-issue-3f2a1c9d8b7e (읽기 어려움)
```

## 발행 CLI

초안 파일은 draft object의 **JSON 배열**입니다.

```json
[
  {
    "title": "[AR] views_per_day ratio feature",
    "fields": {
      "hypothesis": "비율 피처가 ROC-AUC를 높인다.",
      "change": "- 추가 피처: views_per_day = views / (days + 1)",
      "primary_metric_name": "roc_auc",
      "primary_metric_direction": "higher_is_better",
      "minimum_primary_delta": "0.002",
      "guardrail_metric_name": "없음",
      "guardrail_metric_direction": "not_applicable",
      "maximum_guardrail_regression": "없음",
      "secondary_metrics": "pr_auc",
      "comparison": "동일 조건 baseline 재학습 (권장)",
      "dataset_snapshot": "bq://autoresearch/train@2026-07-31",
      "random_seeds": "42, 43, 44",
      "split_seed": "20260731",
      "test_size": "0.2",
      "validation_size": "0.2",
      "training_config_ref": "configs/train/lgbm-v1.yaml@abc1234",
      "dataset": "- 데이터셋 / 경로: data/train.csv\n- 기간 (KST YYYY-MM-DD ~ YYYY-MM-DD): 2026-07-01 ~ 2026-07-31",
      "snapshot_reuse": "허용 (진행하되 실제로 쓴 데이터를 결과에 명시)",
      "result": "- 판정 (지지/기각):"
    },
    "allowed_scope": []
  }
]
```

```bash
# 1) dry-run (기본값) — 본문과 두 식별자를 사람이 확인한다. 토큰을 읽지 않는다.
uv run python -m tools.auto_research_issue_publish --drafts-file drafts.json

# 2) 실제 발행 — 명시적 플래그가 필요하다.
AUTO_RESEARCH_ISSUE_TOKEN=... uv run python -m tools.auto_research_issue_publish \
  --drafts-file drafts.json --publish --repository SKYAHO/Autoresearch
```

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--drafts-file` | (필수) | draft object의 JSON 배열 파일 |
| `--publish` | 꺼짐 | **없으면 dry-run.** 실제 발행에 필요 |
| `--repository` | 없음 | `owner/name`. `--publish`에 필요 |
| `--max-issues` | `1` | 1회 실행당 발행 상한 |

폭주 방지 세 겹:

1. **dry-run이 기본값**입니다.
2. **1회 실행당 발행 상한**을 넘으면 한 건도 발행하지 않습니다.
3. **재발행 차단 키** — `연구 가설`과 `변경할 피처 · 모델`만 묶은 SHA-256
   (`hypothesis_dedupe_key`)이 같은 열린 이슈가 이미 있으면 거부합니다. 같은 배치
   안의 중복도 거부합니다.

차단 키에 `criteria_id`·`reproducibility_id`를 쓰지 않는 이유가 있습니다. 두
식별자에는 `연구 가설`도 `변경할 피처 · 모델`도 들어가지 않습니다 — `criteria_id`는
주 지표 6필드, `reproducibility_id`는 dataset/seed/split/config 6필드만 묶습니다.
따라서 두 식별자를 차단 키로 쓰면 **같은 스냅샷·시드·`roc_auc` 기준 위에서 피처만
바꿔 반복하는 정상 사용 패턴이 전부 중복으로 거부**되고, 반대로 가설을 그대로 둔 채
시드 하나만 바꾸면 차단이 우회됩니다. 차단 키는 발행 도구의 것이며 `IssueInput`이나
marker 봉인 계약에는 들어가지 않습니다.

발행 시 `auto-research`와 `experiment` label을 **반드시 함께** 부여합니다. 워크플로
job은 두 label을 동시에 가질 때만 실행됩니다.

## 토큰

- 필요한 권한은 **`issues: write` 하나뿐**입니다. exp 브랜치는 워크플로가 자신의
  `contents: write`로 만들므로 이 토큰에 `contents` 권한을 주지 마십시오.
- 재발행 차단 키를 확인하려면 열린 이슈 목록과 각 본문을 **읽어야** 하지만,
  fine-grained PAT의 Issues 권한은 read/write가 한 쌍이라 `issues: write`가 읽기까지
  커버합니다. 별도 read 권한을 찾지 마십시오.
- 값은 `AUTO_RESEARCH_ISSUE_TOKEN` 환경 변수로만 주입합니다
  (`.env.example` 참조). 로그·에러 메시지·PR 본문·테스트 fixture 어디에도 값을
  남기지 않습니다. 실패 보고에는 작업 이름과 정제된 경로, HTTP 상태만 담습니다.

## 발행 후

- 발행 후 다시 계산해야 하는 값은 **`issue_branch` 하나뿐**입니다.
  `criteria_id`와 `reproducibility_id`는 이슈 번호에 의존하지 않아 발행 전에
  확정되며, 이슈 번호를 섞어 재계산하면 marker 봉인 재검증이 깨집니다.
- 검수용으로 발행한 이슈는 **close하되 exp 브랜치는 남깁니다.** 브랜치를 지우면
  워크플로가 `recorded issue branch ref is missing`으로 fail-closed하고 재생성
  경로가 없어 해당 이슈가 영구 차단됩니다.
