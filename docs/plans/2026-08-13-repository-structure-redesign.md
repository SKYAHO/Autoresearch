# 저장소 구조 재배치 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 최상위 폴더 이름만 보고 내용을 예상할 수 있도록 파이프라인 단계 축으로 재배치하되, 동작은 한 줄도 바꾸지 않는다.

**Architecture:** `src/`를 없애고 그 내용을 `autoresearch/` 아래 단계별 패키지로 흡수한다. 배포되는 서비스(서빙 API, 에이전트 플랫폼, proxy)는 `applications/` 층으로 분리한다. 순환 의존과 `feature_repo/`는 손대지 않는다.

**Tech Stack:** Python 3.11/3.12, uv, pytest, ruff, typer, Docker

**Spec:** [`docs/specs/2026-08-13-repository-structure-redesign.md`](../specs/2026-08-13-repository-structure-redesign.md)

**Issue:** #754

## Global Constraints

- **동작 변경 금지.** 이 계획의 모든 작업은 이동·리네임·임포트 치환뿐이다. 함수 시그니처, 로직, 로그 문구, 파일 포맷을 바꾸지 않는다. 리팩터링 충동이 들면 별도 이슈로 분리한다.
- **순환 의존 해소 금지.** `autoresearch/jobs/action_log.py`의 함수 내부 지연 import 4곳(`from src.pipeline.rerank_api`, `build_training_dataset` ×2, `model_exposure_provider`)은 경로만 갱신하고 **모듈 최상단으로 올리지 않는다.** 올리면 순환 import로 실패한다.
- **`feature_repo/` 이동 금지.** 최상위에 그대로 둔다. `feature_store.yaml`, `Dockerfile.feast`, `feast-apply.yml`의 `feature_repo` 경로를 건드리지 않는다.
- **`sys.path` 블록 유지.** `pyproject.toml`의 `[tool.uv] package = false`는 그대로다. 이동 대상 파일은 전부 깊이가 보존되므로 `PROJECT_ROOT = os.path.dirname(...)` 줄은 **수정하지 않는다.**
- **`docs/archive/` 갱신 금지.** `docs/README.md` 규칙상 아카이브 문서는 역사적 기록이다.
- **파일 이동과 임포트 치환은 별도 커밋.** git rename 감지를 살려야 리뷰가 가능하다.
- 커밋 메시지는 `<type>: 한국어 설명` + 본문 + `Refs #754` + `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## 착수 전제 (Task 0 이전에 확인)

- [ ] Model Training / Feast Features 도메인 소유자(waieiches, hyochangsung) 합의
- [ ] `gh pr list --state open --json number,files` 로 `src/`를 건드리는 열린 PR이 0건인지 확인
- [ ] 실험 발행 일시 중단 (에이전트 CronJob 정지 또는 가설 제출 보류) — spec 9-1절

전제가 안 되면 Task 1 이후를 착수하지 않는다. Task 0은 전제와 무관하게 가능하다.

## File Structure

| 새 위치 | 책임 |
| --- | --- |
| `autoresearch/cli.py` | 학습·평가·승격 typer CLI 진입점 |
| `autoresearch/jobs/` | Airflow가 소비하는 공개 batch CLI (변경 없음) |
| `autoresearch/data_collection/` | YouTube 트렌딩 수집 |
| `autoresearch/virtual_user_generation/` | 가상 유저 생성 + 파이프라인 어댑터 |
| `autoresearch/action_log_generation/` | action log 생성·shard·품질 계약 |
| `autoresearch/feature_engineering/` | 피처 조립·임베딩·Feast 조회 |
| `autoresearch/model_training/` | 모델 정의, 학습, 학습 데이터셋, provenance, 스냅샷, 아티팩트 I/O |
| `autoresearch/model_evaluation/` | 평가, 열화 측정, paired 비교, seed sweep, 승격 근거 |
| `autoresearch/recommendation/` | 일일 추천, 정책 라운드 시뮬, 노출 provider, 리랭킹 클라이언트 |
| `autoresearch/model_registry/` | MLflow tracking·registry·승격 |
| `autoresearch/reporting/` | HTML 리포트, 실험 결과 리포트 전송 |
| `applications/reranking_api/` | FastAPI 리랭킹 서빙 앱 + k6 부하 테스트 |
| `applications/experiment_platform/` | 실험 에이전트 (api/workbench/runner/launcher/executor/shared) |
| `applications/youtube_api_proxy/` | Cloud Run dumb forwarder |
| `deployment/` | 배포 산출물 + `Dockerfile.*` |

---

### Task 0: 루트 청소

착수 전제와 무관하게 먼저 수행할 수 있다. 다른 Task와 충돌하지 않는다.

**Files:**
- Modify: `.gitignore`
- Delete: `after-submit.png`, `before-submit.png`, `hypotheses-filled.png` (미추적)
- Decide: `agent.md`

- [ ] **Step 1: 미추적 최상위 잔재 확인**

```bash
git status --porcelain --untracked-files=all | grep '^??' | cut -d/ -f1 | sort -u
```

기대: `dags/`, `data/`, `artifacts/`, `asset/`, `output/`, `mlruns/`, `Nemotron-Personas-Korea/`, `.codex-tmp/`, `.omo/`, `.gjc/`, `.playwright-mcp/`, `.superpowers/`, 루트 PNG 3개가 보인다.

- [ ] **Step 2: `.gitignore`에 잔재 추가**

`.gitignore` 끝에 아래 블록을 추가한다.

```gitignore

# 로컬 실행 산출물 — 저장소 구조를 읽을 때의 노이즈를 줄인다 (#754)
/artifacts/
/asset/
/data/
/mlruns/
/output/
/Nemotron-Personas-Korea/
/.codex-tmp/
/.omo/
/.gjc/
/.playwright-mcp/
/.superpowers/
```

`dags/`는 이미 `.gitignore`에 있는지 확인하고 없으면 함께 추가한다.

```bash
grep -n "dags" .gitignore || echo "/dags/" >> .gitignore
```

- [ ] **Step 3: 루트 PNG 삭제**

세 파일 모두 미추적 스크린샷이다. 필요하면 `docs/reports/`로 옮기고, 아니면 지운다.

```bash
rm -f after-submit.png before-submit.png hypotheses-filled.png
```

- [ ] **Step 4: `agent.md` 처리 결정**

`agent.md`는 추적 파일이 아니다(`git ls-files agent.md`가 비어 있음). `AGENTS.md`·`CLAUDE.md`와 별개인 11KB 문서다. 내용을 열어 보고 `AGENTS.md`에 흡수됐으면 삭제, 아니면 `docs/guides/`로 옮긴다.

```bash
git ls-files agent.md   # 비어 있으면 미추적
head -30 agent.md
```

- [ ] **Step 5: 검증**

```bash
git status --porcelain --untracked-files=all | grep '^??'
```

기대: `docs/` 아래 의도한 신규 문서 외에 최상위 잔재가 보이지 않는다.

- [ ] **Step 6: 커밋**

```bash
git add .gitignore
git commit -m "$(cat <<'EOF'
chore: 최상위 로컬 잔재를 gitignore로 걷어낸다

ls로 보이는 최상위 디렉토리 34개 중 추적되는 것은 14개뿐이라, 구조를
읽으려는 눈에 노이즈가 절반 넘게 섞여 있었다. 로컬 실행 산출물을
gitignore에 넣어 최상위가 저장소의 실제 구조를 보이게 한다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 1: `src/` → `autoresearch/` 이동

가장 큰 Task다. 이동 커밋과 치환 커밋을 분리한다.

**Files:**
- Move: `src/` 전체 50개 → `autoresearch/` 아래 (단 `src/serving/`은 Task 2에서 처리하므로 **여기서는 건드리지 않는다**)
- Create: `autoresearch/{feature_engineering,model_training,model_evaluation,recommendation,reporting}/__init__.py`
- Modify: 임포트를 가진 전 파일

**Interfaces:**
- Produces: 아래 모듈 경로. Task 2 이후의 모든 Task가 이 경로에 의존한다.

```
autoresearch.cli
autoresearch.feature_engineering.{assembly,category_reference,embeddings,feast_retrieval,feature_builder,model_contract}
autoresearch.model_training.{base,calibration,downsampling,lgbm_model,model_utils,train,build_training_dataset,training_provenance,training_snapshot_store}
autoresearch.model_evaluation.{evaluate,degradation_eval,experiment_evaluation,training_comparison,paired_experiment,seed_sweep,promotion_evidence}
autoresearch.model_evaluation.experiments.{context,promotion_gate}
autoresearch.recommendation.{daily_recommendations,simulate_policy_round,model_exposure_provider,policy_selector,rerank_api}
autoresearch.virtual_user_generation.adapter
autoresearch.reporting.{report_html,experiment_result_report}
autoresearch.model_registry.{client,logger,model_package,namespace,promote,promotion_result,registry}
autoresearch.data_collection.{backfill,client,fetch,load,schema,transform}
autoresearch.action_log_generation.{calibration,candidate,daily,llm_generator,observability,pipeline,schema,video_source}
```

- [ ] **Step 1: 기준선 기록**

치환 후 비교할 값을 먼저 남긴다.

```bash
uv run python -m pytest --collect-only -q 2>/dev/null | tail -1 | tee /tmp/baseline-collect.txt
uv run python -m pytest -q 2>&1 | tail -3 | tee /tmp/baseline-result.txt
```

기대: 전부 통과. 실패가 있으면 **여기서 멈춘다** — 재배치 전에 이미 깨진 것이므로 원인을 먼저 분리한다.

- [ ] **Step 2: 새 패키지 디렉토리와 `__init__.py` 생성**

```bash
cd /home/yjlee/Autoresearch
for p in feature_engineering model_training model_evaluation recommendation reporting; do
  mkdir -p "autoresearch/$p"
  touch "autoresearch/$p/__init__.py"
done
mkdir -p autoresearch/model_evaluation/experiments
```

`src/models/__init__.py`와 `src/utils/__init__.py`는 **둘 다 빈 파일**이므로 옮기지 않고 지운다(Step 3). 여기서 만든 빈 `__init__.py`가 그 자리를 대신한다. 반면 `src/tracking/__init__.py`는 14줄짜리 re-export가 있으므로 디렉토리째 옮겨 내용을 보존한다.

- [ ] **Step 3: `git mv`로 파일 이동**

```bash
cd /home/yjlee/Autoresearch

# 진입점
git mv src/cli.py autoresearch/cli.py

# feature_engineering ← src/features
for f in assembly category_reference embeddings feast_retrieval feature_builder model_contract; do
  git mv "src/features/$f.py" "autoresearch/feature_engineering/$f.py"
done

# model_training ← src/models + src/utils + 학습 4파일 + config.yaml
for f in base calibration downsampling lgbm_model; do
  git mv "src/models/$f.py" "autoresearch/model_training/$f.py"
done
git rm -q src/models/__init__.py          # 빈 파일 — Step 2의 새 __init__.py 로 대체
git mv src/utils/model_utils.py autoresearch/model_training/model_utils.py
git rm -q src/utils/__init__.py           # 빈 파일
for f in train build_training_dataset training_provenance training_snapshot_store; do
  git mv "src/pipeline/$f.py" "autoresearch/model_training/$f.py"
done
git mv src/pipeline/config.yaml autoresearch/model_training/config.yaml

# model_evaluation
for f in evaluate degradation_eval experiment_evaluation training_comparison \
         paired_experiment seed_sweep promotion_evidence; do
  git mv "src/pipeline/$f.py" "autoresearch/model_evaluation/$f.py"
done
git mv autoresearch/experiments/context.py       autoresearch/model_evaluation/experiments/context.py
git mv autoresearch/experiments/promotion_gate.py autoresearch/model_evaluation/experiments/promotion_gate.py
git mv autoresearch/experiments/__init__.py      autoresearch/model_evaluation/experiments/__init__.py

# recommendation
for f in daily_recommendations simulate_policy_round model_exposure_provider policy_selector rerank_api; do
  git mv "src/pipeline/$f.py" "autoresearch/recommendation/$f.py"
done

# virtual_user_generation ← autoresearch/virtual_users + adapter
git mv autoresearch/virtual_users autoresearch/virtual_user_generation
git mv src/pipeline/virtual_user_adapter.py autoresearch/virtual_user_generation/adapter.py

# reporting
git mv src/pipeline/report_html.py             autoresearch/reporting/report_html.py
git mv src/pipeline/experiment_result_report.py autoresearch/reporting/experiment_result_report.py

# model_registry ← src/tracking
git mv src/tracking autoresearch/model_registry

# 단순 리네임
git mv autoresearch/youtube_collection autoresearch/data_collection
git mv autoresearch/action_logs        autoresearch/action_log_generation
```

`src/`에는 `serving/`만 남아야 한다. 확인:

```bash
ls src/     # serving  (그리고 __pycache__)
```

- [ ] **Step 4: 이동만 커밋 (rename 감지 확보)**

이 시점에는 테스트가 **깨져 있는 것이 정상**이다. 임포트가 아직 옛 경로를 가리킨다.

```bash
git add -A
git status --short | head -20      # R (rename) 로 표시되는지 확인
git commit -m "$(cat <<'EOF'
refactor: src 트리를 autoresearch 단계별 패키지로 옮긴다

파일 이동만 수행한다. 임포트 치환은 다음 커밋에서 한다 — 두 변경을
한 커밋에 섞으면 git이 rename을 감지하지 못해 리뷰가 불가능해진다.
이 커밋 시점에는 테스트가 실패하는 것이 정상이다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: 갈라지는 임포트 1곳을 손으로 고친다**

`autoresearch/cli.py:47`의 다중행 임포트는 6개 모듈이 2개 패키지로 갈라지므로 스크립트로 처리할 수 없다. 아래로 교체한다.

기존:

```python
from src.pipeline import (  # noqa: E402
    build_training_dataset,
    degradation_eval,
    evaluate,
    paired_experiment,
    train,
    training_comparison,
)
```

교체 후:

```python
from autoresearch.model_training import (  # noqa: E402
    build_training_dataset,
    train,
)
from autoresearch.model_evaluation import (  # noqa: E402
    degradation_eval,
    evaluate,
    paired_experiment,
    training_comparison,
)
```

- [ ] **Step 6: 나머지 임포트를 스크립트로 치환**

아래를 스크래치패드에 저장해 실행한다. **커밋하지 않는 일회성 도구다.**

```python
# /tmp/rewrite_imports.py
import pathlib
import re

# 순서가 중요하다. 더 긴 경로를 먼저 치환해야 접두사 충돌이 없다.
DOTTED = [
    # src/pipeline → 4개 패키지로 분할
    ("src.pipeline.build_training_dataset",   "autoresearch.model_training.build_training_dataset"),
    ("src.pipeline.training_snapshot_store",  "autoresearch.model_training.training_snapshot_store"),
    ("src.pipeline.training_provenance",      "autoresearch.model_training.training_provenance"),
    ("src.pipeline.train",                    "autoresearch.model_training.train"),
    ("src.pipeline.experiment_result_report", "autoresearch.reporting.experiment_result_report"),
    ("src.pipeline.experiment_evaluation",    "autoresearch.model_evaluation.experiment_evaluation"),
    ("src.pipeline.training_comparison",      "autoresearch.model_evaluation.training_comparison"),
    ("src.pipeline.promotion_evidence",       "autoresearch.model_evaluation.promotion_evidence"),
    ("src.pipeline.paired_experiment",        "autoresearch.model_evaluation.paired_experiment"),
    ("src.pipeline.degradation_eval",         "autoresearch.model_evaluation.degradation_eval"),
    ("src.pipeline.seed_sweep",               "autoresearch.model_evaluation.seed_sweep"),
    ("src.pipeline.evaluate",                 "autoresearch.model_evaluation.evaluate"),
    ("src.pipeline.model_exposure_provider",  "autoresearch.recommendation.model_exposure_provider"),
    ("src.pipeline.simulate_policy_round",    "autoresearch.recommendation.simulate_policy_round"),
    ("src.pipeline.daily_recommendations",    "autoresearch.recommendation.daily_recommendations"),
    ("src.pipeline.policy_selector",          "autoresearch.recommendation.policy_selector"),
    ("src.pipeline.rerank_api",               "autoresearch.recommendation.rerank_api"),
    ("src.pipeline.virtual_user_adapter",     "autoresearch.virtual_user_generation.adapter"),
    ("src.pipeline.report_html",              "autoresearch.reporting.report_html"),
    # 단순 리네임 — model_utils 를 utils 보다 먼저
    ("src.utils.model_utils",                 "autoresearch.model_training.model_utils"),
    ("src.utils",                             "autoresearch.model_training"),
    ("src.features",                          "autoresearch.feature_engineering"),
    ("src.models",                            "autoresearch.model_training"),
    ("src.tracking",                          "autoresearch.model_registry"),
    ("src.cli",                               "autoresearch.cli"),
    ("autoresearch.youtube_collection",       "autoresearch.data_collection"),
    ("autoresearch.action_logs",              "autoresearch.action_log_generation"),
    ("autoresearch.virtual_users",            "autoresearch.virtual_user_generation"),
    ("autoresearch.experiments",              "autoresearch.model_evaluation.experiments"),
]

# `from <pkg> import <이름>` 형태. 값은 (심볼 → 새 패키지).
FROM_PKG = {
    "src.pipeline": {
        "build_training_dataset":  "autoresearch.model_training",
        "train":                   "autoresearch.model_training",
        "training_snapshot_store": "autoresearch.model_training",
        "evaluate":                "autoresearch.model_evaluation",
        "degradation_eval":        "autoresearch.model_evaluation",
        "paired_experiment":       "autoresearch.model_evaluation",
        "training_comparison":     "autoresearch.model_evaluation",
        "experiment_evaluation":   "autoresearch.model_evaluation",
        "simulate_policy_round":   "autoresearch.recommendation",
    },
    "src.features": {"*": "autoresearch.feature_engineering"},
    "src.tracking": {"*": "autoresearch.model_registry"},
    "src":          {"cli": "autoresearch"},
}

ROOTS = ["autoresearch", "applications", "src", "tests", "scripts", "examples",
         "tools", "feature_repo", "agent_orchestration", "proxy"]


def rewrite_from_pkg(text: str) -> str:
    def repl(m: re.Match) -> str:
        pkg, names = m.group(1), m.group(2)
        table = FROM_PKG.get(pkg)
        if table is None:
            return m.group(0)
        symbols = [n.strip() for n in names.split(",")]
        targets = {table.get(s.split(" as ")[0], table.get("*")) for s in symbols}
        if len(targets) != 1 or None in targets:
            raise SystemExit(
                f"수동 처리 필요 — 한 줄이 여러 패키지로 갈라진다: {m.group(0)!r}"
            )
        return f"from {targets.pop()} import {names}"

    return re.sub(
        r"from (src(?:\.[a-z_]+)*) import ([a-zA-Z_][\w, ]*)", repl, text
    )


changed = 0
for root in ROOTS:
    for path in pathlib.Path(root).rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        original = path.read_text(encoding="utf-8")
        text = rewrite_from_pkg(original)
        for old, new in DOTTED:
            text = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", new, text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed += 1
print(f"{changed} files rewritten")
```

```bash
uv run python /tmp/rewrite_imports.py
```

스크립트가 "수동 처리 필요"로 멈추면 그 줄을 손으로 고치고 다시 실행한다. Step 5를 먼저 했다면 멈추지 않아야 한다.

- [ ] **Step 7: 잔여 `src.` 참조 확인**

```bash
grep -rn "from src\.\|import src\.\|from src import" --include=*.py . | grep -v '\.venv\|\.worktrees'
```

기대: `src/serving/` 내부 참조만 남는다 (Task 2에서 처리). 그 외 0건.

- [ ] **Step 8: 하드코딩 config 경로 수정**

`autoresearch/model_training/train.py:590`, `autoresearch/model_evaluation/evaluate.py:311`:

```python
# 기존
config_path = os.path.join(project_root, "src", "pipeline", "config.yaml")
# 교체
config_path = os.path.join(project_root, "autoresearch", "model_training", "config.yaml")
```

`autoresearch/cli.py` 292·404·455·1219행의 typer help 문자열:

```python
# 기존
help="config.yaml 경로 (기본: src/pipeline/config.yaml)"
# 교체
help="config.yaml 경로 (기본: autoresearch/model_training/config.yaml)"
```

`src/serving/model_loader.py:40` 주석은 Task 2에서 처리한다.

```bash
grep -rn '"src"\|src/pipeline/config.yaml' --include=*.py autoresearch
```

기대: 0건.

- [ ] **Step 9: docstring 경로 참조 수정**

이동한 모듈의 최상단 docstring이 옛 경로를 서술한다. CLAUDE.md 규칙상 기능을 옮기는 같은 커밋에서 갱신한다.

```bash
grep -rn "src/pipeline\|src/features\|src/models\|src/tracking\|src/utils\|src\.pipeline\|src\.features" --include=*.py autoresearch
```

나오는 곳을 새 경로로 고친다. 특히 `autoresearch/feature_engineering/feature_builder.py:9-15`의 feast ODFV 계약 서술은 `feature_repo` 부분을 **그대로 두고** `src.features` 표기만 바꾼다.

CLAUDE.md 규칙상 모듈 docstring은 "전체 파이프라인 기준으로 어느 구간을 담당하는지"를 서술한다. `[비책임]` 절이 옛 경로로 인접 모듈을 가리키는 곳도 함께 고친다. 예: `autoresearch/loadtest/__init__.py`의 *"HTTP 리랭킹 요청은 src/serving/이 담당한다"* → Task 2에서 `applications/reranking_api/`로 갱신.

- [ ] **Step 9-1: 순환 회피 지연 import에 주석을 남긴다**

spec 7절의 남는 부채다. `autoresearch/jobs/action_log.py`의 함수 내부 지연 import 4곳(209, 233, 239, 265행)에 아래 취지의 주석을 각 블록 위에 붙인다.

```python
# 순환 import 회피 — action_log_generation ↔ recommendation 은 폐루프 구조상
# 서로를 참조한다. 모듈 최상단으로 올리면 import 가 실패한다 (#754).
```

주석만 추가하고 **import 위치를 바꾸지 않는다.**

- [ ] **Step 10: `examples/` 갱신**

```bash
grep -rn "src\." examples
```

- `examples/ctr_pipeline_scaffold/01_generate_mock_raw_data.py:24` → `autoresearch.feature_engineering.category_reference`
- `examples/ctr_pipeline_scaffold/02_generate_event_log.py:11,30,92` → `autoresearch.feature_engineering.feature_builder`
- `examples/ctr_pipeline_scaffold/sync_mock_data_to_pipeline.py:108` → `python -m autoresearch.cli build-features`
- `examples/ctr_pipeline_scaffold/README.md:99,123` → `autoresearch.feature_engineering.feature_builder`

- [ ] **Step 11: `Dockerfile.train` CMD 갱신**

```dockerfile
# 기존 (54행)
CMD ["python", "-m", "src.cli", "--help"]
# 교체
CMD ["python", "-m", "autoresearch.cli", "--help"]
```

- [ ] **Step 12: 테스트 실행**

```bash
uv run python -m pytest -q 2>&1 | tail -5
```

기대: Step 1의 `/tmp/baseline-result.txt`와 **동일한 통과 수**. `src/serving/` 관련 테스트가 아직 옛 경로를 쓰므로 통과해야 한다 (Task 1에서 serving을 안 건드렸으므로).

- [ ] **Step 13: lint**

```bash
uv run --no-sync ruff check autoresearch src tests tools scripts
```

- [ ] **Step 14: CLI 동작 확인**

```bash
uv run python -m autoresearch.cli --help
uv run python -m autoresearch.recommendation.daily_recommendations --help
```

기대: 정상 출력.

- [ ] **Step 15: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: src 임포트를 autoresearch 새 경로로 치환한다

앞 커밋의 파일 이동에 맞춰 임포트 101곳과 하드코딩된 config 경로를
갱신한다. cli.py의 다중행 임포트는 6개 모듈이 model_training과
model_evaluation 둘로 갈라지므로 두 문장으로 나눴다.

train.py·evaluate.py가 문자열로 조립하던 기본 config 경로와 cli.py의
typer help 문자열 4곳도 함께 고친다 — 임포트 치환으로는 잡히지 않는다.

examples 스캐폴드와 Dockerfile.train CMD도 새 경로를 가리키게 한다.

sys.path 블록은 건드리지 않았다. 이동 대상이 전부 깊이를 보존해
PROJECT_ROOT 계산이 그대로 유효하다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `applications/` 층 신설

**Files:**
- Create: `applications/__init__.py`, `applications/experiment_platform/shared/__init__.py`
- Move: `src/serving/` → `applications/reranking_api/`, `agent_orchestration/` → `applications/experiment_platform/`, `proxy/` → `applications/youtube_api_proxy/`, `loadtest/` + `autoresearch/loadtest/` → `applications/reranking_api/loadtest/`
- Modify: `deploy/serving/Dockerfile`, `deploy/agent_orchestration/*.Dockerfile`, `agent_orchestration/alembic.ini`, `docker-compose.yml`, 진입점 스크립트

- [ ] **Step 1: 디렉토리 생성과 이동**

```bash
cd /home/yjlee/Autoresearch
mkdir -p applications && touch applications/__init__.py

git mv src/serving applications/reranking_api
rmdir src/features src/models src/pipeline src/utils 2>/dev/null
rm -rf src/__pycache__ src/*/__pycache__ 2>/dev/null
rmdir src 2>/dev/null || ls src   # 비어야 한다

git mv proxy applications/youtube_api_proxy

git mv agent_orchestration applications/experiment_platform
cd applications/experiment_platform
git mv app api
git mv ui workbench
mkdir -p shared && touch shared/__init__.py
for f in codex contracts github_app github_pull_requests github_refs bootstrap_secrets; do
  git mv "$f.py" "shared/$f.py"
done
cd /home/yjlee/Autoresearch

mkdir -p applications/reranking_api/loadtest
git mv loadtest/rerank.js  applications/reranking_api/loadtest/rerank.js
git mv loadtest/README.md  applications/reranking_api/loadtest/README.md
rmdir loadtest 2>/dev/null
git mv autoresearch/loadtest/rerank_fixture.py applications/reranking_api/loadtest/rerank_fixture.py
git mv autoresearch/loadtest/__init__.py       applications/reranking_api/loadtest/__init__.py
rmdir autoresearch/loadtest 2>/dev/null
```

- [ ] **Step 2: 이동만 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: 배포되는 서비스를 applications 층으로 옮긴다

파일 이동만 수행한다. 임포트 치환은 다음 커밋에서 한다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: 임포트 치환**

Task 1의 스크립트를 재사용하되 `DOTTED`를 아래로 바꾼다.

```python
DOTTED = [
    ("src.serving",                    "applications.reranking_api"),
    ("autoresearch.loadtest",          "applications.reranking_api.loadtest"),
    ("agent_orchestration.app",        "applications.experiment_platform.api"),
    ("agent_orchestration.ui",         "applications.experiment_platform.workbench"),
    ("agent_orchestration.runner",     "applications.experiment_platform.runner"),
    ("agent_orchestration.launcher",   "applications.experiment_platform.launcher"),
    ("agent_orchestration.executor",   "applications.experiment_platform.executor"),
    ("agent_orchestration.codex",      "applications.experiment_platform.shared.codex"),
    ("agent_orchestration.contracts",  "applications.experiment_platform.shared.contracts"),
    ("agent_orchestration.github_app", "applications.experiment_platform.shared.github_app"),
    ("agent_orchestration.github_pull_requests", "applications.experiment_platform.shared.github_pull_requests"),
    ("agent_orchestration.github_refs","applications.experiment_platform.shared.github_refs"),
    ("agent_orchestration.bootstrap_secrets", "applications.experiment_platform.shared.bootstrap_secrets"),
]
FROM_PKG = {"src.serving": {"*": "applications.reranking_api"}}
```

```bash
uv run python /tmp/rewrite_imports.py
grep -rn "src\.serving\|agent_orchestration\.\|autoresearch\.loadtest" --include=*.py . | grep -v '\.venv\|\.worktrees'
```

기대: 0건.

- [ ] **Step 4: `src/serving/model_loader.py:40` 주석 수정**

```python
# 기존
# 학습 config(src/pipeline/config.yaml artifacts.*) 파일명이 바뀌면 함께 갱신한다.
# 교체
# 학습 config(autoresearch/model_training/config.yaml artifacts.*) 파일명이 바뀌면 함께 갱신한다.
```

- [ ] **Step 5: 배포 파일 경로 갱신**

```bash
grep -rn "agent_orchestration\|src/serving\|src\.serving\|^COPY proxy" deploy/ applications/experiment_platform/docker-compose.yml applications/experiment_platform/alembic.ini applications/experiment_platform/*.sh
```

- `deploy/serving/Dockerfile:36` → `CMD ["uvicorn", "applications.reranking_api.app:app", "--host", "0.0.0.0", "--port", "8000"]`
- `deploy/agent_orchestration/{api,runner,ui,launcher,executor}.Dockerfile` — 아래 Step 5-1 참조
- `applications/experiment_platform/alembic.ini` — `script_location` 이 상대 경로면 그대로, 절대/패키지 경로면 갱신
- `applications/experiment_platform/{entrypoint.sh,runner_entrypoint.sh}` — `python -m agent_orchestration.*` 갱신
- `applications/experiment_platform/docker-compose.yml` — build context·volume 경로 갱신

- [ ] **Step 5-1: 에이전트 이미지 5개의 COPY 허용 목록 갱신**

이 다섯 Dockerfile은 디렉토리 통째가 아니라 **모듈을 하나씩 골라 COPY하는 허용 목록** 방식이다(#701이 launcher에 `github_pull_requests`를 넣어 맞춘 그 목록). 공유 모듈이 `shared/`로 내려가므로 전부 바뀐다.

경로 접두사는 모든 파일에서 동일하게 바뀐다:

```
agent_orchestration/                    → applications/experiment_platform/
agent_orchestration/app                 → applications/experiment_platform/api
agent_orchestration/ui                  → applications/experiment_platform/workbench
agent_orchestration/<공유모듈>.py        → applications/experiment_platform/shared/<공유모듈>.py
```

**추가로 각 이미지에 `applications/__init__.py`와 `shared/__init__.py` COPY를 넣어야 한다.** 빠뜨리면 패키지 import가 런타임에 실패한다 — 이것이 이 Step의 주된 실패 모드다.

| 파일 | 바꿀 행 |
| --- | --- |
| `api.Dockerfile` | 42-48, 52, 53, 63 (`CMD`) |
| `executor.Dockerfile` | 46, 49, 50, 51, 62 (`CMD`) |
| `launcher.Dockerfile` | 23, 24, 27, 28, 29, 34, 41 (`CMD`) |
| `runner.Dockerfile` | 37, 38, 39, 40, 41, 51 (`CMD`) |
| `ui.Dockerfile` | 25, 28-32, 45 (`CMD`) |

예 — `api.Dockerfile`:

```dockerfile
COPY applications/__init__.py ./applications/
COPY applications/experiment_platform/__init__.py ./applications/experiment_platform/
COPY applications/experiment_platform/api ./applications/experiment_platform/api
COPY applications/experiment_platform/shared/__init__.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/contracts.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/bootstrap_secrets.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/github_app.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/github_refs.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/entrypoint.sh ./applications/experiment_platform/
COPY applications/experiment_platform/alembic.ini ./applications/experiment_platform/
COPY applications/experiment_platform/migrations ./applications/experiment_platform/migrations
...
CMD ["./applications/experiment_platform/entrypoint.sh"]
```

`CMD` 변경:

```dockerfile
# executor
CMD ["python", "-m", "applications.experiment_platform.executor.main"]
# launcher
CMD ["python", "-m", "applications.experiment_platform.launcher.main"]
# runner
CMD ["./applications/experiment_platform/runner_entrypoint.sh"]
# ui
CMD ["streamlit", "run", "applications/experiment_platform/workbench/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", \
     "--browser.gatherUsageStats=false", "--server.fileWatcherType=none"]
```

`ui.Dockerfile`은 `app/experiments/models.py`를 선별 COPY하므로 `api/experiments/models.py`로 바꾼다. `.streamlit` COPY는 그대로 둔다.

빌드 후 각 이미지에서 import가 되는지 확인한다:

```bash
docker build -f deploy/agent_orchestration/executor.Dockerfile -t ao-executor:ci .
docker run --rm ao-executor:ci python -c "import applications.experiment_platform.executor.main"
```

- [ ] **Step 6: alembic migration 경로 확인**

```bash
grep -n "script_location\|prepend_sys_path\|version_locations" applications/experiment_platform/alembic.ini
grep -rn "agent_orchestration" applications/experiment_platform/migrations/env.py
```

`env.py`가 모델을 import 하면 경로를 갱신한다. **revision 파일 자체는 수정하지 않는다** — 이미 적용된 마이그레이션 이력이다.

- [ ] **Step 7: 테스트·lint**

```bash
uv run python -m pytest -q 2>&1 | tail -5
uv run --no-sync ruff check autoresearch applications tests tools scripts
```

기대: Step 1 기준선과 동일한 통과 수.

- [ ] **Step 8: 이미지 빌드 검증**

```bash
docker build -f Dockerfile.app   -t autoresearch:ci .
docker build -f Dockerfile.train -t autoresearch-train:ci .
docker build -f deploy/serving/Dockerfile -t autoresearch-serving:ci .
for img in api runner ui launcher executor; do
  docker build -f "deploy/agent_orchestration/$img.Dockerfile" -t "ao-$img:ci" . || break
done
```

이미지 빌드 성공만으로는 부족하다. COPY 허용 목록이 빠진 모듈은 **런타임에야** 드러나므로 import까지 확인한다:

```bash
docker run --rm ao-api:ci      python -c "import applications.experiment_platform.api.main"
docker run --rm ao-executor:ci python -c "import applications.experiment_platform.executor.main"
docker run --rm ao-launcher:ci python -c "import applications.experiment_platform.launcher.main"
docker run --rm ao-runner:ci   python -c "import applications.experiment_platform.runner.app"
docker run --rm ao-ui:ci       python -c "import applications.experiment_platform.workbench.app"
```

- [ ] **Step 9: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: applications 층 임포트와 배포 경로를 갱신한다

서빙 API를 applications/reranking_api로, 에이전트 플랫폼을
applications/experiment_platform으로, proxy를 youtube_api_proxy로
옮긴 데 맞춰 임포트와 Dockerfile·compose·entrypoint 경로를 고친다.

experiment_platform 최상단에 흩어져 있던 공유 모듈 6개(866줄)를
shared/로 모은다 — api/workbench/runner/launcher/executor 다섯이
공유하는 것들이라 최상단에 두면 어느 서비스 소유인지 읽히지 않는다.

이름이 겹쳐 혼란을 주던 최상위 loadtest(k6)와 autoresearch/loadtest
(픽스처)를 reranking_api/loadtest 한 곳으로 합친다.

alembic revision 파일은 적용 이력이므로 수정하지 않았다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `tests/` 소스 구조 미러링

**Files:**
- Move: `tests/test_*.py` 다수 → `tests/<패키지명>/`
- Modify: `tests/conftest.py` (필요 시)

- [ ] **Step 1: 기준선 재확인**

```bash
uv run python -m pytest --collect-only -q 2>/dev/null | tail -1
```

이 숫자를 Task 3 종료 시 비교한다.

- [ ] **Step 2: 디렉토리 생성**

```bash
cd /home/yjlee/Autoresearch/tests
for p in feature_engineering model_training model_evaluation recommendation \
         model_registry reporting data_collection virtual_user_generation \
         jobs cli applications; do
  mkdir -p "$p" && touch "$p/__init__.py"
done
mkdir -p applications/reranking_api applications/experiment_platform
touch applications/reranking_api/__init__.py applications/experiment_platform/__init__.py
```

기존 `tests/action_logs/`는 `tests/action_log_generation/`으로 리네임한다.

```bash
git mv action_logs action_log_generation
```

- [ ] **Step 3: 테스트 파일을 대상 모듈 기준으로 이동**

각 테스트가 무엇을 import 하는지로 목적지를 정한다.

```bash
cd /home/yjlee/Autoresearch
for f in tests/test_*.py; do
  target=$(grep -ohE "(from|import) (autoresearch|applications)\.[a-z_]+(\.[a-z_]+)?" "$f" \
           | awk '{print $2}' | cut -d. -f1-2 | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
  echo "$f -> $target"
done
```

출력을 보고 이동한다. 예:

아래는 위 명령으로 산출한 결과를 목적지별로 묶은 것이다. **`<확인>` 표시는 휴리스틱이 애매한 파일**이므로 파일을 열어 실제 대상 모듈을 확인하고 확정한다.

**`tests/model_training/`**

```
test_pipeline_train.py → test_train.py
test_build_training_dataset.py
test_build_training_dataset_feast_path.py
test_build_training_dataset_env_check_feast.py
test_training_provenance.py
test_training_snapshot_store.py
test_spine_coverage_guard.py
test_downsampling.py
test_models_contract.py
test_model_utils.py
test_calibration.py            <확인> src/models/calibration 인지 action_logs/calibration 인지
```

**`tests/model_evaluation/`**

```
test_pipeline_evaluate.py → test_evaluate.py
test_degradation_eval.py
test_degradation_eval_hold.py
test_degradation_eval_dates.py
test_degradation_eval_detection.py
test_degradation_eval_staleness.py
test_paired_experiment.py
test_pipeline_seed_sweep.py → test_seed_sweep.py
test_training_comparison.py
test_pipeline_experiment_evaluation.py → test_experiment_evaluation.py
test_experiment_evaluation_temporal_signal.py
test_pipeline_promotion_evidence.py → test_promotion_evidence.py
```

**`tests/model_evaluation/experiments/`**

```
test_experiment_context.py
test_experiment_promotion_gate.py
```

**`tests/recommendation/`**

```
test_daily_recommendations.py
test_simulate_policy_round.py
test_model_exposure_provider.py
test_rerank_api.py
test_policy_selector.py
test_action_logs_schema_policy.py   <확인> action_log_generation 과 걸침
```

**`tests/feature_engineering/`**

```
test_embeddings.py
test_feature_builder.py
test_features_assembly.py
test_model_feature_contract.py
test_odfv_category_match_feast.py
test_build_pool_feature_frame_feast.py
test_feast_retrieval_integration_feast.py
test_verify_registry_portability_feast.py
```

**`tests/model_registry/`**

```
test_model_package.py
test_tracking_promote.py
test_tracking_registry.py
test_tracking_namespace.py
test_tracking_promotion_result.py
```

**`tests/reporting/`**

```
test_experiment_result_report.py
```

**`tests/data_collection/`**

```
test_youtube_client.py
test_youtube_collection_load.py
test_youtube_collection_fetch.py
test_youtube_collection_schema.py
test_youtube_collection_backfill.py
test_youtube_collection_transform.py
```

**`tests/virtual_user_generation/`**

```
test_virtual_users_schema.py
test_virtual_users_pipeline.py
test_virtual_users_categories.py
test_virtual_users_glm_generator.py
test_virtual_users_persona_source.py
test_virtual_user_adapter.py → test_adapter.py
```

**`tests/action_log_generation/`** (기존 `tests/action_logs/`를 리네임한 곳)

```
test_action_logs_daily.py
test_action_logs_pipeline.py
test_action_logs_llm_generator.py
test_action_logs_observability.py
test_click_threshold_calibration.py
test_click_threshold_calibrate_job.py
```

**`tests/jobs/`**

```
test_action_log_job.py
test_action_log_job_telemetry.py
test_action_log_quality_job.py
test_feast_materialize.py
test_feature_store_build.py
test_youtube_backfill_job.py
test_youtube_trending_job.py
```

**`tests/cli/`**

```
test_cli.py
```

**`tests/applications/reranking_api/`**

```
test_serving_api.py
test_serving_onnx.py
test_serving_schemas.py
test_serving_feast_reader.py
test_serving_feast_reader_feast.py
test_serving_online_features.py
test_serving_model_registry.py
test_serving_deployment.py
test_rerank_loadtest_fixture.py
```

**`tests/applications/experiment_platform/`**

```
test_agent_orchestration.py                  test_experiment_models.py
test_agent_orchestration_runner.py           test_experiment_router.py
test_agent_orchestration_bootstrap.py        test_experiment_service.py
test_agent_orchestration_container.py        test_experiment_postgres.py
test_agent_orchestration_ui_cost.py          test_experiment_cost_api.py
test_agent_orchestration_ui_step.py          test_experiment_report_api.py
test_agent_orchestration_ui_time.py          test_experiment_step_router.py
test_agent_orchestration_ui_write.py         test_experiment_step_service.py
test_agent_orchestration_ui_report.py        test_experiment_candidate_api.py
test_ui_board.py                             test_experiment_issue_endpoint.py
test_ui_submission_app.py                    test_experiment_issue_publication.py
test_ui_submission_form.py                   test_experiment_issue_migration.py
test_ui_visual_contract.py                   test_experiment_transition_service.py
test_github_app.py                           test_experiment_branch_baseline.py
test_github_refs.py                          test_experiment_branch_migration.py
test_github_issues.py                        test_experiment_candidate_migration.py
test_github_pull_requests.py                 test_experiment_executor.py
test_issue_authoring.py                      test_experiment_executor_router.py
test_executor_report.py                      test_experiment_executor_integration.py
test_executor_training.py                    test_experiment_workspace.py
test_executor_measurement.py                 test_experiment_codex_worker.py
test_executor_results_store.py               test_experiment_candidate_verifier.py
test_executor_command_output.py              test_experiment_candidate_finalizer.py
test_launcher_resident.py                    test_experiment_launcher.py
test_launcher_log_collector.py               test_experiment_pull_request.py
test_launcher_job_resources.py               test_experiment_pull_request_run.py
test_launcher_training_environment.py        test_experiment_pull_request_adapters.py
test_harness_resource_budget.py              test_auto_experiment_trigger_label.py
```

**`tests/applications/youtube_api_proxy/`**

```
test_proxy_app.py
test_proxy_docker.py
```

**`tests/` 루트에 남기는 것** — 여러 패키지에 걸치거나 저장소 자체를 검사하는 테스트다. 무리해서 나누지 않는다.

```
conftest.py                             test_logging_json.py
paired_experiment_fixtures.py           test_redis_iam.py
test_release_workflow.py                test_feature_repo_env.py
test_branch_protection_contract.py      test_feast_apply_workflow.py
test_auto_research_issue_branch.py      test_offline_retrieval_smoke_feast.py
test_build_static_features.py           test_odfv_registry_portability_feast.py
test_load_raw_to_bigquery.py            test_verify_serving_e2e.py
test_generate_action_logs_scale.py      test_degradation_curve_plot.py
test_rewrite_action_log_event_ids.py
test_pr_report_archive.py               test_pr_report_archive_rail.py
test_pr_report_archive_merge.py         test_pr_report_archive_search.py
test_pr_report_archive_category.py      test_pr_report_archive_workflow.py
test_pr_report_archive_card_isolation.py
```

이동은 `git mv`로 한다. 예:

```bash
git mv tests/test_pipeline_train.py tests/model_training/test_train.py
```

- [ ] **Step 4: 수집 개수 비교**

```bash
uv run python -m pytest --collect-only -q 2>/dev/null | tail -1
```

기대: Step 1과 **정확히 같은 숫자**. 다르면 파일이 유실됐거나 `__init__.py`가 빠져 수집이 안 되는 것이다.

- [ ] **Step 5: 이름 충돌 확인**

같은 basename의 테스트 파일이 서로 다른 디렉토리에 있으면 `__init__.py`가 없을 때 pytest가 충돌을 낸다. Step 2에서 전부 만들었는지 확인한다.

```bash
find tests -type d -not -path '*__pycache__*' -exec sh -c '[ -f "$1/__init__.py" ] || echo "missing __init__: $1"' _ {} \;
```

- [ ] **Step 6: 전체 실행**

```bash
uv run python -m pytest -q 2>&1 | tail -5
```

- [ ] **Step 7: CI 테스트 목록 갱신**

`.github/workflows/ci.yml`의 feast·postgres 그룹 job이 테스트 경로를 명시적으로 나열한다. 이동한 경로로 갱신한다.

```bash
grep -n "tests/" .github/workflows/ci.yml
```

- [ ] **Step 8: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: 테스트를 소스 구조에 맞춰 재배치한다

소스가 12개 패키지로 나뉘었는데 tests가 플랫이면 "이 모듈 테스트가
어디에 있나"가 새 문제가 된다. 소스와 같은 모양으로 미러링한다.

여러 패키지에 걸친 통합 테스트는 루트에 남겼다. 디렉토리마다
__init__.py를 넣어 같은 basename의 테스트 파일이 충돌하지 않게 한다.

수집 테스트 수가 재배치 전후로 동일함을 확인했다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `deployment/` 이동과 CI 경로 갱신

**Files:**
- Move: `deploy/` → `deployment/`, `Dockerfile.{app,train,feast}` → `deployment/`
- Modify: `.github/workflows/{ci,release,feast-apply,lint}.yml`, `.github/ISSUE_TEMPLATE/auto_research.yml`, `pyproject.toml`, `.dockerignore`

- [ ] **Step 1: 이동**

```bash
cd /home/yjlee/Autoresearch
git mv deploy deployment
git mv Dockerfile.app   deployment/Dockerfile.app
git mv Dockerfile.train deployment/Dockerfile.train
git mv Dockerfile.feast deployment/Dockerfile.feast
```

- [ ] **Step 2: 워크플로우 경로 갱신**

```bash
grep -rn "deploy/\|Dockerfile\.\|src/\*\*\|src\.cli\|src\.pipeline\|src\.serving" .github/workflows/
```

고칠 것:

| 위치 | 변경 |
| --- | --- |
| `ci.yml:48,56,69,79` | `paths` 필터 `'src/**'` → `'autoresearch/**'`, `'applications/**'` |
| `ci.yml:266`, `release.yml:215` | `src.pipeline.daily_recommendations` → `autoresearch.recommendation.daily_recommendations` |
| `ci.yml:311,312,313,378` | `python -m src.cli` → `python -m autoresearch.cli` |
| `ci.yml:404`, `release.yml:401` | `import ... src.serving.app` → `applications.reranking_api.app` |
| 전 워크플로우 | `-f Dockerfile.app` → `-f deployment/Dockerfile.app` (train·feast 동일) |
| 전 워크플로우 | `deploy/serving/Dockerfile` → `deployment/serving/Dockerfile` (agent_orchestration 이미지 5개 동일) |

`feature_repo` 관련 경로(`feast-apply.yml`의 path 필터, `ci.yml:391`의 `load_feature_store('/app/feature_repo')`)는 **건드리지 않는다.**

- [ ] **Step 3: 이슈 템플릿 갱신**

`.github/ISSUE_TEMPLATE/auto_research.yml:212`의 `python -m src.cli build-features` → `python -m autoresearch.cli build-features`.

이 문구는 실험 에이전트가 읽는 가설 템플릿이다. 갱신 직후부터 새 실험이 새 경로를 쓴다.

- [ ] **Step 4: `pyproject.toml` 주석 갱신**

121-123행:

```toml
# 기존
# Phase 1(이슈 #80): 의존성 관리만 uv 로 전환하고 패키지 배치 방식(sys.path)은
# 유지한다. Phase 2 에서 src 레이아웃 전환 시 package 빌드로 변경 예정.
# 교체
# 의존성 관리만 uv 로 전환하고 패키지 배치 방식(sys.path)은 유지한다(이슈 #80).
# #754의 디렉토리 재배치는 sys.path 방식을 그대로 두었다 — 설치형 패키지
# (build-system + [project.scripts]) 전환은 여전히 별도 과제다.
```

- [ ] **Step 5: `.dockerignore` 확인**

```bash
cat .dockerignore
```

`src/`나 `deploy/`를 명시하면 갱신한다.

- [ ] **Step 6: 로컬 이미지 빌드**

```bash
docker build -f deployment/Dockerfile.app   -t autoresearch:ci .
docker build -f deployment/Dockerfile.train -t autoresearch-train:ci .
docker build -f deployment/Dockerfile.feast -t autoresearch-feast:ci .
docker build -f deployment/serving/Dockerfile -t autoresearch-serving:ci .
```

- [ ] **Step 7: 워크플로우 문법 검사**

```bash
git diff --check
command -v actionlint >/dev/null && actionlint || echo "actionlint 없음 — 건너뜀"
```

- [ ] **Step 8: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: 배포 산출물을 deployment로 모으고 CI 경로를 갱신한다

Dockerfile 3개가 최상위에 흩어져 있던 것을 deploy/와 함께
deployment/로 모은다. CI·release 워크플로우의 실행 경로와 paths
필터, 이슈 템플릿의 build-features 명령을 새 모듈 경로로 고친다.

이슈 템플릿 문구는 실험 에이전트가 읽는 가설 계약이라, 이 커밋
시점부터 새 실험이 autoresearch.cli 경로를 쓴다.

pyproject의 "Phase 2 src 레이아웃 전환" 주석은 이번 재배치가 그
전환이 아님을 명시하도록 고쳤다 — 설치형 패키지 전환은 별도 과제다.

feature_repo 경로는 건드리지 않았다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: 문서 갱신

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `AGENTS.md`, `.claude/docs/agent-project-reference.md`, `.claude/docs/architecture-overview.md`, `docs/README.md`, 살아있는 `docs/specs/`·`docs/guides/`
- Modify: `docs/specs/2026-07-15-repo-restructure.md` (대체 표기)
- Move or rewrite: `docs/plans/2026-07-15-src-package-merge.md`

- [ ] **Step 1: 갱신 대상 목록 만들기**

```bash
grep -rln "src/\|src\.\|agent_orchestration\|^deploy/" \
  README.md CLAUDE.md AGENTS.md .claude/docs docs/README.md docs/specs docs/guides \
  2>/dev/null | sort
```

`docs/archive/`는 목록에 넣지 않는다.

- [ ] **Step 2: `README.md` 저장소 구조 절 교체**

26-52행의 구조 블록을 spec 4절의 최종 구조로 교체한다. 213행의 미해결 표기

> `src/serving/`(리랭킹 API)과 정책 라운드·일일 추천 폐루프의 도메인 소유는 아직 미지정입니다 — 저장소 구조 논의(#149)에서 확정 예정.

는 `applications/reranking_api/` 기준으로 다시 쓰거나, 도메인 소유가 정해졌으면 삭제한다.

54-67행 배포 이미지 표의 `Dockerfile.*` 경로를 `deployment/` 기준으로 고친다.

- [ ] **Step 3: `CLAUDE.md`·`AGENTS.md` 갱신**

두 파일은 내용이 동일하다(같은 크기·날짜). 한쪽을 고치고 복사한다.

"저장소 경계" 절의 경로 표기와 "Local Development"의 ruff 명령

```bash
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
```

을 새 경로로 고친다:

```bash
uv run --no-sync ruff check autoresearch applications tests tools
```

```bash
cp CLAUDE.md AGENTS.md   # 갱신 후 동기화
diff CLAUDE.md AGENTS.md && echo "동일"
```

- [ ] **Step 4: `.claude/docs/` 갱신**

`agent-project-reference.md`의 폴더 책임·소유 경계 표, `architecture-overview.md`의 경로를 새 구조로 고친다.

- [ ] **Step 5: 선행 문서에 대체 표기 추가**

`docs/specs/2026-07-15-repo-restructure.md`의 "결정 3" 절 머리에 추가:

```markdown
> **대체됨 (2026-08-13)** — 이 결정은
> [`docs/specs/2026-08-13-repository-structure-redesign.md`](2026-08-13-repository-structure-redesign.md)로
> 대체되었습니다. 아래 목표 구조는 채택되지 않았습니다.
```

`docs/plans/2026-07-15-src-package-merge.md`는 `docs/archive/plans/`로 옮긴다.

```bash
git mv docs/plans/2026-07-15-src-package-merge.md docs/archive/plans/
```

- [ ] **Step 6: `docs/README.md` 인덱스 갱신**

새 spec·plan을 인덱스에 추가하고, 아카이브로 옮긴 plan의 항목을 옮긴다.

- [ ] **Step 7: 최종 전수 확인**

```bash
grep -rn "from src\.\|import src\.\|python -m src\." --include=*.py --include=*.yml \
  --include=*.yaml --include=*.md --include=Dockerfile* . \
  | grep -v '\.venv\|\.worktrees\|docs/archive'
```

기대: **0건.**

- [ ] **Step 8: 전체 검증**

```bash
uv run python -m pytest -v 2>&1 | tail -5
uv run --no-sync ruff check autoresearch applications tests tools scripts
git diff --check
```

feast 그룹:

```bash
uv sync --only-group feast
# .github/workflows/ci.yml 의 pytest (feast group) job 테스트 목록을 실행
uv sync   # dev 환경 복구
```

- [ ] **Step 9: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
docs: 새 디렉토리 구조를 문서에 반영한다

README 구조 절과 배포 이미지 표, CLAUDE.md·AGENTS.md의 저장소 경계와
ruff 명령, .claude/docs의 폴더 책임 표를 새 경로로 갱신한다.

#149가 남긴 2026-07-15 spec의 결정 3에 대체 표기를 넣고, 짝이 되는
plan은 아카이브로 옮긴다. docs/archive/ 이하는 역사적 기록이므로
경로를 갱신하지 않았다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: 인접 저장소 갱신 (별도 저장소)

이 저장소의 PR이 머지되고 이미지가 배포된 **이후** 수행한다.

- [ ] **Step 1: `Autoresearch-airflow`에서 호출 경로 확인**

```bash
gh api repos/SKYAHO/Autoresearch-airflow/contents --jq '.[].name'
# 로컬 클론이 있으면
grep -rn "src\.cli\|src\.pipeline\|src\.serving\|python -m src" <airflow-repo-path>
```

- [ ] **Step 2: 이슈 발행 후 브랜치에서 경로 갱신, PR 생성**

`autoresearch.jobs.*` 호출은 변경 없음을 함께 확인한다.

- [ ] **Step 3: 실험 발행 재개**

Task 4에서 이슈 템플릿을 갱신했으므로, 재개 후 첫 실험이 완주하는지 확인한다.

---

## 검증 요약

| 시점 | 명령 | 기대 |
| --- | --- | --- |
| Task 1 이전 | `pytest --collect-only -q \| tail -1` | 기준선 기록 |
| 각 Task 끝 | `uv run python -m pytest -q` | 기준선과 동일한 통과 수 |
| Task 3 끝 | `pytest --collect-only -q \| tail -1` | Task 1 이전과 **정확히 동일** |
| Task 2·4 끝 | `docker build` 4종 | 성공 |
| Task 5 끝 | `grep -rn "from src\.\|import src\.\|python -m src\."` (archive 제외) | 0건 |
| Task 5 끝 | `uv run --no-sync ruff check autoresearch applications tests tools scripts` | 통과 |

## PR 전략

Task마다 별도 PR을 올린다. `main` 기준, `Closes #754`는 마지막 PR에만 넣고 나머지는 `Refs #754`를 쓴다.

| PR | Task | 성격 |
| --- | --- | --- |
| 1 | Task 0 | 잔재 정리 (독립, 먼저 머지 가능) |
| 2 | Task 1 | 최대 diff — 이동/치환 2커밋 |
| 3 | Task 2 | applications 층 |
| 4 | Task 3 | 테스트 재배치 |
| 5 | Task 4 | 배포·CI |
| 6 | Task 5 | 문서 |

PR 2~5는 순서 의존이므로 앞 PR이 머지된 뒤 rebase해 올린다.
