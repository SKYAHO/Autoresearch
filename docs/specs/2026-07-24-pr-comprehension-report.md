# PR 이해 리포트 자동 생성 (3단계 이해 체계)

> Status: 구현 (#313) | Last Updated: 2026-07-24

## 배경·목표

팀 전원이 에이전트(Claude/Codex CLI)로 코드를 생성하면서 PR 단위 코드
이해가 병목이 되었습니다. 모든 PR에 다음 3단계 이해 체계를 자동
제공합니다.

1. **시각화** — 전체 파이프라인에서 이 PR의 위치(as-is → to-be), 변경
   이유, 기대효과
2. **중요도순 diff** — 함수/모듈 단위 재구성, core > contract > config >
   test > docs 순 정렬
3. **이해도 확인 Q&A** — 기존 `claude.yml` 리뷰 봇의 인라인
   "이해도 확인:" 질문(답변 후 스레드 resolve)

## 설계 결정

- **에이전트는 HTML을 작성하지 않습니다.** 시각화 품질을 고정하기 위해
  HTML/CSS 템플릿(`.github/pr-report/template.html`)을 저장소에 커밋해
  두고, 에이전트는 `report.json` 데이터만 출력합니다.
- **파이프라인 노드는 정본 카탈로그에서만 가져옵니다.**
  `.github/pr-report/pipeline-nodes.json`이 노드/에지의 단일 출처이며,
  에이전트는 각 노드의 status(unchanged/modified/added/removed)만
  판정합니다. 파이프라인 구조 변경 시 `docs/guides/pipeline-overview.md`
  갱신과 같은 PR에서 카탈로그도 갱신합니다.
- **배포는 gh-pages `pr/<번호>/`.** 저장소가 public이므로 GitHub Pages를
  사용합니다. 리포트 URL: `https://skyaho.github.io/Autoresearch/pr/<n>/`
- **자동 트리거는 opened / ready_for_review만.** 에이전트 PR은 push가
  잦아 synchronize 자동 재생성은 비용·소음이 큽니다. 갱신은 PR에
  `/claude-report` 코멘트로 수동 트리거합니다.
- **soft-fail.** 스키마 위반·생성 실패 시 안내 코멘트만 남기고 job은
  성공 종료합니다. required check로 등록하지 않으며 머지를 막지
  않습니다.

## 동작 계약

### report.json

계약 정본은 `.github/pr-report/report.schema.json`입니다
(`additionalProperties: false`). 요지:

| 키 | 계약 |
| --- | --- |
| `summary_ko` | 정확히 3줄, 각 120자 이하 |
| `motivation_ko` / `expected_effects_ko` | as-is 문제 서술 / 기대효과 1~5개 |
| `pipeline.nodes[]` | 카탈로그 전체 복사 + status 판정, 변경 노드는 `as_is_ko`/`to_be_ko` |
| `pipeline.focus` | 중심 노드 id 1~3개 |
| `changes[]` | 20개 이하, rank·importance·module·symbol·file·explanation_ko 필수 |
| `changes[].importance` | `core` \| `contract` \| `config` \| `test` \| `docs` |
| `qa_note_ko` | 3단계(인라인 질문 답변·resolve) 안내 |

### 워크플로우 (`.github/workflows/pr-report.yml`)

- **analyze job** (PR별 concurrency, cancel-in-progress):
  `generate_report.py`가 PR 메타·diff·파이프라인 정본 문서를 결정적으로
  수집해 **OpenRouter chat completions API**(#319)로 `report.json` 생성
  (스키마 검증 실패 시 오류 피드백 1회 재시도) → `check-jsonschema` 검증 →
  `inject.py`로 `site/pr/<n>/index.html` 빌드 → artifact 업로드. 실패 시
  안내 코멘트. 모델은 워크플로 상단 `PR_REPORT_MODEL` 변수로 교체
  (기본 `google/gemini-3.6-flash`), 인증은 `OPENROUTER_API_KEY` 시크릿
  (팀 공용 종량제 — 개인 Claude 구독과 분리). diff는 생성 파일 제외,
  파일당 20K자·전체 150K자 제한.
- **publish job** (저장소 전역 concurrency — gh-pages push race 방지):
  `peaceiris/actions-gh-pages@v4` + `keep_files: true`로 배포 → 마커
  `<!-- pr-comprehension-report -->` 기반 sticky 코멘트 upsert (요약 3줄,
  중요 변경 top-3, 리포트 링크, 3단계 안내).

### 기존 리뷰 봇과의 관계

`claude.yml`(인라인 이해도 확인 질문)과 `pr-report.yml`은 별도
워크플로우로 병렬 실행됩니다. 리뷰 요약 코멘트가 리포트 URL을
안내합니다. 트리거 문구 `/claude-review`와 `/claude-report`는 substring
관계가 아니므로 충돌하지 않습니다. 인증도 분리되어 있습니다 —
`claude.yml`은 `CLAUDE_CODE_OAUTH_TOKEN`(Claude 구독), `pr-report.yml`은
`OPENROUTER_API_KEY`(종량제)를 사용합니다.

## 운영

- 1회 설정: 첫 배포로 `gh-pages` 브랜치가 생성된 뒤 Settings → Pages →
  "Deploy from a branch" → `gh-pages` / root 지정.
- fork PR에서는 시크릿이 주입되지 않으므로 리포트가 생성되지 않습니다
  (팀은 같은 저장소 브랜치로 작업하므로 영향 없음).
- 로컬 검증: 샘플 report.json으로
  `python .github/pr-report/inject.py .github/pr-report/template.html report.json > index.html`
  후 브라우저 확인.

## 남은 작업 (phase 2)

- PR close 시 gh-pages의 `pr/<n>/` 정리 워크플로우
- 검증 후 `Autoresearch-infra`(카탈로그를 인프라 노드로 교체),
  `Autoresearch-airflow`(OAuth secret 등록 선행, DAG 관점 카탈로그)로
  동일 세트 롤아웃
