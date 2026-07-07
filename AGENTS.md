# Coding Guidelines for AI Coding Agents

> Version: 1.0.0 | Last Updated: 2026-07-06

이 문서는 Claude Code 등 AI 코딩 에이전트가 이 저장소에서 작업할 때의 기본
진입점입니다. 필수 규칙은 짧게 유지하고, 상세 가이드는 `.claude/docs/`를
참조합니다.

## Language Preference

에이전트 응답, PR 코멘트, 리뷰 요약, 구현 노트는 한국어 격식체를 사용합니다.
사용자가 명시적으로 요청하는 경우에만 다른 언어를 사용합니다. (추후 영어 전환
예정)

## Rule Priority

규칙이 충돌하면 다음 순서로 적용합니다:

1. 사용자의 명시적 요청
2. `CLAUDE.md` 및 `AGENTS.md`
3. `.claude/docs/` 하위 가이드
4. `README.md`, 소스 주석, 기타 저장소 문서

같은 수준이면 더 구체적이고 더 최근에 갱신된 규칙을 우선합니다.

## Documentation Navigation

비자명한 변경을 하기 전에 가장 관련 있는 가이드를 먼저 확인합니다:

| 요청 유형 | 먼저 볼 문서 | 다음 문서 |
| --- | --- | --- |
| 프로젝트 구조·소유권 | `.claude/docs/agent-project-reference.md` | `.claude/docs/architecture-overview.md` |
| Python 스타일, 타이핑, 로깅 | `.claude/docs/agent-python-reference.md` | `.claude/docs/coding-conventions.md` |
| 워크플로우, spec, plan, 커밋, PR | `.claude/docs/agent-workflow-reference.md` | `.claude/docs/agent-prohibitions.md` |
| 보안, 시크릿, 외부 입력 | `.claude/docs/agent-security-guidelines.md` | `.claude/docs/agent-prohibitions.md` |
| 에러 처리 | `.claude/docs/agent-error-handling-reference.md` | 관련 소스 파일 |
| 코드 리뷰 | `.claude/docs/agent-peer-review.md` | `.claude/docs/agent-workflow-reference.md` |
| 계획 리뷰 | `.claude/docs/agent-plan-review.md` | `.claude/docs/agent-peer-review.md` |

각 문서는 현재 구현과 계획(별도 브랜치 진행 중)을 구분해 표기합니다.

## Project Context

- Autoresearch: YouTube 트렌딩 데이터 기반 CTR 모델링 프로젝트
- 런타임 패키지는 `autoresearch/`:
  - `autoresearch/youtube_collection/` — YouTube 트렌딩 수집
    (fetch/transform/load/backfill/schema + client.py 복원력 레이어),
    GCS 데이터 레이크 적재
  - `proxy/` — Cloud Run dumb forwarder (YouTube API IP밴 대응 egress seam)
  - `autoresearch/virtual_users/` — Gemini 기반 가상 유저(페르소나) 생성
    파이프라인
- Airflow DAG은 `dags/` (Astro Runtime 13.8.0): `youtube_trending_kr_daily`,
  `youtube_backfill_kr`
- 테스트는 `tests/` (모듈별 `test_<module>.py` 플랫 구조)
- CTR 파이프라인 예제 스캐폴드는 `examples/ctr_pipeline_scaffold/`
- 의존성은 pip 기반: `requirements.txt`(런타임) /
  `requirements-dev.txt`(개발·테스트). `requirements.txt`는 Astro
  이미지(`Dockerfile`)와 CI 이미지(`Dockerfile.app`)가 공유하는 단일
  출처입니다.
- Python 3.12 (`.python-version`), CI는 3.11/3.12 매트릭스
- 팀 도메인 4개: Model Training (waieiches, hyochangsung), Feast Features
  (waieiches, hyochangsung — 도입 진행 중), Airflow Orchestration (bbungjun),
  GCP Infrastructure (hyeongyu-data)
- Feast 피처 스토어는 `feature_repo/`에 도입되어 있습니다 (Entity·FeatureView
  정의는 더미 스키마, 실데이터 스키마로 교체 예정).
- CTR 학습 파이프라인(`src/` 구조)은 별도 브랜치에서 진행 중이며 아직 main에
  없습니다.

## Core Rules

- 새 추상화보다 기존 저장소 패턴을 우선합니다.
- 구조 변경과 동작 변경은 분리합니다.
- Python 함수의 타입 힌트(반환 타입 포함)를 유지합니다.
- 요청된 변경에 필요하지 않은 광범위한 리팩터링은 피합니다.
- 시크릿, 로컬 데이터 경로, 생성된 데이터 파일, `.env`를 커밋하지 않습니다.
- 동작, 명령어, 설정, 운영 방식이 바뀌면 문서를 갱신합니다.

## Local Development

로컬 테스트와 개발:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

- Airflow DAG 로컬 실행은 Astro CLI를 사용합니다 (`astro dev start`).
- 필수 환경 변수는 `.env.example` 참조: `YOUTUBE_API_KEY`,
  `YOUTUBE_LAKE_BUCKET`, `YOUTUBE_BACKFILL_SOURCE`.
- 주의: `dags/*.py`는 sys.path 조작으로 `autoresearch` 패키지를 import 합니다.
  컨테이너 내 패키지 배치(`Dockerfile`)와 결합되어 있으므로 구조 변경 시 함께
  확인해야 합니다.

## Spec / Plan First

비자명한 변경(광범위한 동작 변경, 마이그레이션, 모듈 간 계약, 공개 API, 대규모
다중 파일 수정)은 구현 전에 계획을 작성합니다.

저장소 작업 문서 구조를 사용합니다:

- 요구사항, 설계 결정, 동작 계약, 아키텍처 노트 →
  `docs/specs/YYYY-MM-DD-<slug>.md`
- 구현 순서, 작업 분해, 검증 체크리스트 →
  `docs/plans/YYYY-MM-DD-<slug>.md`
- 새로 만들기보다 기존 관련 spec/plan 갱신을 우선합니다.

문서만 수정하거나 범위가 좁은 워크플로우 변경은 스레드 내 짧은 계획으로
진행할 수 있습니다.

## Verification

변경을 증명하는 가장 좁은 검증부터 수행하고, 공유 동작이나 사용자
워크플로우에 영향이 있으면 범위를 넓힙니다.

주요 명령어:

```bash
python -m pytest -v                                 # CI와 동일
docker build -f Dockerfile.app -t autoresearch:ci . # CI 이미지 빌드 검증
```

GitHub 워크플로우·문서 변경 시 추가로:

```bash
git diff --check
```

`actionlint`가 로컬에 있으면 함께 사용합니다.

## Review Guidance

PR 리뷰 시 심각도 순으로 구체적 발견 사항을 먼저 제시합니다. 중점 사항:

- 정확성 버그와 기존 동작의 의도치 않은 변경
- 시크릿·자격 증명 처리 위험
- 데이터 스키마/계약(pydantic 모델, parquet 스키마) 변경 위험
- 변경된 동작에 대한 테스트 누락·부실
- 타입 안정성 문제
- 전제 조건과 영향이 명확한 성능 문제

구체적 코드 이슈는 인라인 코멘트로, 요약 코멘트는 짧게 유지합니다.
