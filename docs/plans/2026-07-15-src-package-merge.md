# `src/` → `autoresearch/` 패키지 통합 Plan

- 날짜: 2026-07-15
- Spec: [`docs/specs/2026-07-15-repo-restructure.md`](../specs/2026-07-15-repo-restructure.md)
- 상태: **팀 합의 대기** — Model Training / Feast Features 도메인 소유자
  (waieiches, hyochangsung)의 승인 후 착수
- 실행 시점: 진행 중인 학습 파이프라인 브랜치가 머지된 직후 (충돌 최소화)

## 사전 확인 사항 (착수 전)

- [ ] waieiches, hyochangsung 승인
- [ ] 열린 PR·브랜치 중 `src/`를 건드리는 것이 없는지 확인
- [ ] 이슈 발행 후 이슈 브랜치에서 작업

## 이동 매핑

| 현재 | 이동 후 | 비고 |
|---|---|---|
| `src/features/` | `autoresearch/features/` | |
| `src/models/` | `autoresearch/models/` | |
| `src/pipeline/` | `autoresearch/training/` | 역할이 드러나도록 개명, `config.yaml` 포함 |
| `src/tracking/` | `autoresearch/tracking/` | |
| `src/utils/` | `autoresearch/utils/` | |
| `src/cli.py` | `autoresearch/training/cli.py` | 공개 batch 승격 여부는 별도 논의 |

## 작업 순서

구조 변경만 수행하고 동작 변경은 하지 않는다. 전 과정을 하나의 PR로 만든다.

1. **`git mv`로 디렉토리 이동** — 위 매핑대로. 각 패키지에 `__init__.py`
   존재 확인 (`src/features/`, `src/pipeline/`에는 현재 `__init__.py`가 없음 —
   이동하면서 추가).
2. **import 경로 일괄 치환**
   - `from src.pipeline` / `import src.pipeline` → `autoresearch.training`
   - 그 외 `src.<mod>` → `autoresearch.<mod>`
   - 대상: `src/` 내부 상호 import, `tests/test_feature_builder.py`,
     `examples/`, `scripts/` 전수 grep
3. **경로 하드코딩 수정**
   - `train.py`·`evaluate.py`·`cli.py`의
     `os.path.join(project_root, "src", "pipeline", "config.yaml")` 류 기본
     config 경로를 새 위치로 갱신 (가능하면 `importlib.resources` 또는 모듈
     상대 경로로 전환)
   - `sys.path` 조작 코드(`# noqa: E402` 딸린 상단 블록)가 남아 있으면 제거
     가능한지 확인 — 단일 패키지가 되면 불필요할 수 있음
4. **빌드·설정 갱신**
   - `pyproject.toml` `[tool.uv] package = false` 주석의 "Phase 2 src 레이아웃
     전환" 문구를 실제 결정에 맞게 갱신 (이 통합이 곧 Phase 2에 해당)
   - `Dockerfile.app`은 `COPY autoresearch ./autoresearch`라 변경 불필요 —
     단, 학습 코드가 앱 이미지에 포함되는 것을 팀이 인지해야 함 (런타임
     의존성 lightgbm·mlflow는 이미 포함되어 있어 이미지 크기 영향 없음)
   - `.github/workflows/`에서 `src/` 경로 참조 여부 grep
5. **문서 갱신**
   - `README.md`, `.claude/docs/agent-project-reference.md`(Project Layout·
     Team Ownership 경로), `CLAUDE.md`의 `src/models/` 등 경로 언급
   - `docs/guides/ctr-model-specification.md` 등에서 `src/` 경로 언급 grep

## 검증

- [ ] `uv run python -m pytest -v` 전체 통과
- [ ] `grep -rn 'from src\.\|import src\.' .` 결과 0건 (`.venv` 제외)
- [ ] `uv run python -m autoresearch.training.cli --help` (또는 동등 진입점) 동작
- [ ] `docker build -f Dockerfile.app -t autoresearch:ci .` 성공 후
      컨테이너에서 `import autoresearch.training` 확인
- [ ] `examples/ctr_pipeline_scaffold/` 스크립트 1개 이상 실행 확인

## 리스크

- **브랜치 충돌**: 이동 후 기존 `src/` 브랜치는 rebase 시 충돌한다. 실행
  시점을 팀 브랜치 머지 직후로 고정하는 것이 유일한 완화책.
- **경로 하드코딩 누락**: config·모델 저장 경로가 문자열로 조립되는 곳이
  더 있을 수 있음 — `grep -rn '"src"' .`로 전수 확인.
