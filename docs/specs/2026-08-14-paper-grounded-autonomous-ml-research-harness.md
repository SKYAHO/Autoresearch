# 논문 기반 자율 ML 연구 Harness — YouTube 리랭킹 MVP

> 2026-08-14 | 상태: 제품 방향·상위 설계·구현 계획 확정, 구현 미착수
>
> 이 문서는 기존 executor의 다음 단계를 정의한다. 현재의 단일 가설 실행 계약을
> 폐기하지 않으며, 논문 발견부터 반복 실험과 최종 리포트까지 이어지는 자율 연구
> 제품의 목표 구조를 고정한다.

## 1. 목적

Autoresearch MVP의 목표는 CTR 도메인의 최고 성능 자체가 아니다. YouTube 트렌딩 기반
리랭킹을 구체적인 테스트베드로 삼아 다음 능력을 end-to-end로 증명하는 것이다.

> 사용자가 자연어 연구 목표와 실행 예산을 제출하면, 에이전트가 관련 논문을 스스로
> 발견하고 현재 저장소에서 검증 가능한 가설로 변환하여 코드 수정·학습·평가·실패
> 복구를 반복한 뒤, 모든 출처와 판단 근거가 연결된 최종 REPORT를 반환한다.

사용자가 CTR, 추천 지표, 모델 구조를 미리 알아야 하는 제품으로 만들지 않는다.
도메인별 기본 평가 방법과 안전 계약은 `ResearchDomain` 구현이 소유하며, 사용자가
확인해야 하는 것은 실행 도중의 승인 요청이 아니라 완주 후 REPORT뿐이다.

## 2. 배경과 현재 한계

현재 저장소는 기준 SHA와 데이터 스냅샷을 고정한 뒤 Codex가 한 번 코드를 수정하고,
baseline/candidate를 학습·평가해 리포트를 남기는 경로까지 도달했다. 이 경로는 다음
기반을 이미 제공한다.

- 고정된 `base_dev_sha`와 실험 브랜치
- content-addressed 학습 데이터 스냅샷
- baseline/candidate의 동일 조건 비교와 다중 seed 실행
- 데이터·분할 fingerprint, 지표 JSON, 에이전트 작성 리포트
- 실패한 Job 회수, 실험 상태와 Step 관측

그러나 현재 실행 모델은 본질적으로 한 번의 `코드 수정 → 검증 → 학습 → 평가 → 보고`다.
에이전트가 결과를 관찰해 다음 가설을 선택하는 반복 연구 루프가 없고, 논문 발견·출처
검증·실험 이력에 기반한 후속 전략도 없다. 따라서 executor만 놓고 보면 범용 coding
agent나 기존 goal 실행과 구별되는 ML 연구 방법론이 부족하다.

관련 현행 계약과 실측은 다음 문서가 소유한다.

- [Auto Research 최소 흐름의 제약](2026-07-29-auto-research-minimum-loop-gaps.md)
- [실험 실행 능력 활성화 계약](2026-08-07-experiment-execution-enablement.md)
- [에이전트 작성 실험 리포트 계약](2026-08-09-agent-authored-experiment-report.md)
- [리랭킹 지표 정합](2026-08-04-reranking-metric-alignment.md)

이 문서는 위 계약의 현재 구현 사실을 반복하지 않고, 그 위에 놓일 자율 연구 계층을
정의한다.

## 3. 제품 계약

### 3.1 입력

사용자는 다음 두 종류의 정보만 제공한다.

- 자연어 연구 목표: 예) `YouTube 추천 목록의 리랭킹 품질을 개선한다.`
- 실행 예산: 최대 시간, 최대 trial 수, CPU/GPU 등 허용 자원

논문 shortlist, 모델 종류, 피처, primary metric, 통계 기법을 사용자에게 선택시키지
않는다. YouTube MVP의 기본값은 `YouTubeCTRDomain`이 결정한다.

### 3.2 실행 중 상호작용

연구 run에는 사람의 승인 gate를 두지 않는다.

- 논문 선택 승인 없음
- 가설 승인 없음
- 코드 변경 승인 없음
- 실패 복구 승인 없음
- candidate 채택 승인 없음

사용자는 상태를 관찰할 수 있지만, 관찰이 실행의 전제 조건이 되어서는 안 된다.
운영 배포나 production champion 전환은 연구 run 바깥의 별도 책임이며, 자율 연구의
완주를 중단시키지 않는다.

### 3.3 출력

사람이 확인하는 주 산출물은 최종 `research-report.html` 또는 동일 내용을 가진
Markdown REPORT다. 다음 기계 판독 산출물도 함께 보존한다.

```text
research-report.html
paper-manifest.json
experiment-ledger.jsonl
artifacts/
```

모든 가설이 기각되어도 연구 run은 유효하게 완주할 수 있다. `개선 없음`은 실행 실패가
아니라 근거가 있는 연구 결과다.

## 4. 핵심 원칙

### 4.1 ML Harness가 연구 규칙을 소유한다

Codex는 논문 해석, 가설 구체화, 코드 변경을 담당한다. 데이터 고정, 실행 순서,
평가, 예산, 복구, 이력 보존은 결정론적인 Research Harness가 담당한다. LLM의 자연어
판단만으로 candidate를 승격하지 않는다.

### 4.2 연구 대상 전체는 수정 가능하다

에이전트는 제한된 피처 파일만 수정하는 것이 아니라 연구 대상 저장소 전체를 수정할 수
있다.

- raw 데이터 재조립과 파생 데이터셋
- 피처와 임베딩
- label과 auxiliary task
- sampling, weighting, loss, optimizer
- 모델 아키텍처와 학습·추론 코드
- 리랭킹과 calibration
- 의존성, lock 파일, 디렉터리 구조, CLI, 테스트

파일 allowlist를 연구 공간의 정의로 사용하지 않는다. Git commit 전체를 하나의
candidate genome으로 취급한다.

연구 공간 제한과 안전·저장소 위생 제한은 구분한다. 현행 verifier의 path allowlist와
dependency 변경 금지는 Research Harness에 승계하지 않지만, 시크릿·자격 증명이 candidate
commit에 들어가는 것은 계속 차단한다.

현행 verifier는 `.csv`, `.pkl`, `.parquet` 변경을 생성 데이터로 간주해 거부하므로, 위에서
허용한 raw 데이터 재조립과 파생 데이터셋 생성을 그대로 막는다. Harness는 확장자만으로
workspace 변경을 거부하지 않는다. 파생 데이터셋은 candidate commit이 아니라 disposable
workspace의 산출물로 생성·소비하고, 코드와 재현 계약만 candidate genome에 남겨 저장소
위생과 연구 자유를 함께 지킨다.

### 4.3 외부 심판은 수정할 수 없다

에이전트가 수정할 수 없는 최소 root of trust를 Research Harness와 Sealed Judge가
소유한다.

- 원본 데이터 snapshot과 fingerprint
- 숨겨진 evaluation slate와 정답
- 최종 지표 계산과 baseline/candidate 비교
- 시간·연산·저장공간 예산과 중단 조건
- 데이터 누수와 산출물 계약 검사
- Trial Ledger와 paper provenance
- 자격 증명과 네트워크 정책
- REPORT의 원본 증거

에이전트가 candidate workspace 안의 evaluator나 테스트를 수정하는 것은 허용할 수 있다.
Sealed Judge의 재평가 대상 artifact는 candidate가 라벨 없는 봉인 slate에 대해 산출한
**예측 점수 파일**이다. Judge는 이 파일과 숨긴 정답으로 채점만 하며 candidate 코드를
import하거나 실행하지 않고 모델 파일도 역직렬화하지 않는다. candidate 코드를 전혀
실행하지 않는 경계가 가장 깨끗하게 봉인되며 역직렬화 위험도 없기 때문이다. 실행 중인
Controller와 Judge는 고정 이미지·digest 또는 별도 프로세스로 봉인되므로, candidate
branch가 그 소스를 수정해도 현재 run의 판정에는 반영되지 않는다.

### 4.4 로컬 실행이 기본이다

장시간 실행과 중단 후 재개는 Kubernetes만의 기능이 아니다. MVP는 다음 조합으로
로컬에서 완주할 수 있어야 한다.

- disposable Git worktree
- 격리된 subprocess 또는 container
- 읽기 전용 데이터 snapshot
- 별도 Judge 프로세스
- 영속 Trial Ledger와 checkpoint

Kubernetes Job은 다중 사용자 격리, 원격 자원, GPU scheduling이 필요할 때 선택하는
`ExperimentRunner` 구현체다. Kubernetes 배포 자체를 제품 차별점으로 삼지 않는다.

## 5. 목표 아키텍처

```text
Research Request
       |
       v
+-----------------------+
| Research Controller   |  budget, checkpoint, state machine
+-----------+-----------+
            |
            v
+-----------------------+     +-----------------------+
| Paper Discovery       | --> | Capability Matcher    |
| search, resolve, cite |     | feasible hypotheses   |
+-----------------------+     +-----------+-----------+
                                            |
                                            v
                               +-----------------------+
                               | Research Agent        |
                               | full-repo mutation    |
                               +-----------+-----------+
                                           |
                               disposable candidate
                                           |
                                           v
                               +-----------------------+
                               | Experiment Runner     |
                               | local / Kubernetes    |
                               +-----------+-----------+
                                           |
                                           v
                               +-----------------------+
                               | Sealed Judge          |
                               | evaluate and compare  |
                               +-----------+-----------+
                                           |
                         keep / discard / revise
                                           |
                    +----------------------+------------------+
                    |                                         |
                    v                                         v
          next research iteration                    Trial Ledger
                                                              |
                                                              v
                                                    Final REPORT
```

### 5.1 주요 인터페이스

```python
class ResearchDomain(ABC):
    def describe_capabilities(self) -> DomainCapabilities: ...
    def build_evaluation_snapshot(self) -> EvaluationSnapshot: ...
    def validate_candidate(self, candidate: CandidateArtifact) -> ValidationResult: ...
    def evaluate(self, candidate: CandidateArtifact) -> DomainMetrics: ...
    def compare(self, champion: TrialResult, candidate: TrialResult) -> Decision: ...


class PaperSource(ABC):
    async def search(self, query: PaperQuery) -> list[PaperMetadata]: ...
    async def get(self, paper_id: PaperId) -> PaperDocument: ...
    async def citations(self, paper_id: PaperId) -> CitationGraph: ...


class ExperimentRunner(ABC):
    async def run(self, candidate: CandidateArtifact) -> TrialResult: ...


class LocalRunner(ExperimentRunner): ...
class KubernetesJobRunner(ExperimentRunner): ...
```

MVP에는 `YouTubeCTRDomain`과 `LocalRunner`만 실제 구현한다. 이커머스·뉴스피드는
`ResearchDomain` 계약의 확장 가능성을 설명하는 후속 사례이며 MVP 구현 범위가 아니다.
테스트에서는 작은 fixture용 fake domain을 사용할 수 있으나 두 번째 제품 domain으로
간주하지 않는다.

## 6. 논문 자동 발견

논문 발견은 서비스 내부의 첫 번째 연구 단계다. 사용자가 PDF나 논문 목록을 제공하는
흐름을 기본값으로 두지 않는다.

### 6.1 MVP source 구성

- OpenAlex: 여러 출판처를 포괄하는 검색, 필터, 인용 관계 탐색
- arXiv: 추천·CTR·representation learning 분야의 공개 preprint와 원문
- Crossref: DOI와 canonical publication metadata 해소 및 중복 제거
- Semantic Scholar: 후속 단계에서 관련 논문 추천과 citation expansion 보강

범용 웹 crawl 결과를 곧바로 논문 근거로 사용하지 않는다. 원문은 공개적으로 접근 가능한
문서만 수집하며, 원문을 확보하지 못한 경우 `abstract_only`로 표시한다. 유료 원문을
우회하지 않는다.

공식 API 문서:

- [OpenAlex Search](https://developers.openalex.org/guides/searching)
- [arXiv API User's Manual](https://github.com/arXiv/arxiv-docs/blob/develop/source/help/api/user-manual.md)
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
- [Semantic Scholar Academic Graph API](https://www.semanticscholar.org/product/api)

### 6.2 발견 절차

1. `ResearchDomain`이 현재 데이터, 모델, label, 실행 가능 범위를 설명한다.
2. Controller가 연구 목표와 capability를 조합해 검색 query family를 만든다.
3. 여러 source에서 후보를 수집하고 DOI, arXiv ID, 제목으로 중복을 제거한다.
4. 상위 후보의 인용·참고문헌 그래프를 제한된 깊이로 확장한다.
5. 관련성, 데이터 적합성, 구현 가능성, 검증 가능성, 비용, 누수 위험으로 점수화한다.
6. 동일 유형의 오래된 유명 논문만 남지 않도록 seminal, recent, domain-specific,
   debiasing 등 연구 축의 다양성을 유지한다.
7. 사람이 shortlist를 승인하지 않아도 예산 안에서 top candidate를 자동 선택한다.
8. 검색 query, provider, 조회 시점, 원문 범위, 선택·탈락 사유를 manifest에 보존한다.

인용 수만으로 논문을 정렬하지 않는다. 인용 수는 evidence quality의 일부일 뿐이며,
현재 프로젝트에서 실제로 반증 가능한 가설을 만들 수 있는지가 더 중요하다.

### 6.3 PaperCard

논문은 링크 목록이 아니라 다음 구조의 연구 입력으로 변환한다.

```yaml
paper_id: P03
title: "Up Next: Retrieval Methods for Large Scale Related Video Suggestion"
identifiers:
  doi: null
  arxiv_id: null
sources:
  canonical_url: "official publisher or research page"
  discovery_provider: openalex
  discovery_query: "video topical representation recommendation"
  retrieved_at: "RFC3339 timestamp"
evidence:
  scope: full_text
  document_sha256: "..."
  referenced_sections:
    - section: "4"
      claim: "topical representation과 관련된 검증 근거"
repository_fit:
  usable_fields: [title, description, tags]
  missing_requirements: []
hypothesis:
  change: "category 기반 유사도를 video text representation으로 확장"
  expected_metrics: [ndcg_at_10, recall_at_10]
falsification:
  - "동일 evaluation slate에서 champion 대비 개선되지 않음"
usage:
  status: implemented
  hypothesis_ids: [H-004]
  trial_ids: [T-004]
```

Google Research의 [Up Next](https://research.google/pubs/up-next-retrieval-methods-for-large-scale-related-video-suggestion/)
같이 현재 raw 필드로 검증 가능한 논문은 즉시 가설 후보가 된다. DIN처럼 시간순 사용자
행동 sequence가 필요한 논문은 에이전트가 action log에서 sequence dataset을 재조립해
검증할 수 있다. 예산이나 데이터가 부족하면 `infeasible_with_current_data`로 기록하고
다음 후보로 진행한다.

### 6.4 논문 사용 상태

REPORT는 논문을 다음 상태로 구분한다.

- `discovered`: 검색 결과에서 발견
- `screened_out`: 적합성 검토 후 제외
- `reviewed`: 초록 또는 원문 분석
- `hypothesis_source`: 가설 생성의 근거로 사용
- `implemented`: 실제 candidate로 구현
- `promoted` 또는 `rejected`: 실험 판정 완료

존재 여부와 canonical identifier를 검증하지 못한 논문은 구현 근거로 사용할 수 없다.

## 7. 자율 연구 루프

```python
while budget.remaining:
    papers = discovery.find_candidates(research_state)
    hypothesis = planner.select_and_compile(papers, trial_history)
    candidate = agent.implement(hypothesis, disposable_workspace)

    validation = harness.validate(candidate)
    if not validation.runnable:
        ledger.reject(candidate, validation.reason)
        continue

    result = runner.run(candidate, fidelity="screening")
    decision = judge.decide(result, champion, guardrails)

    if decision == "promote":
        confirmation = runner.run(candidate, fidelity="multi_seed")
        champion = judge.promote_if_valid(confirmation, champion)

    ledger.record(hypothesis, candidate, result, decision)
```

각 iteration은 현재 champion에서 disposable worktree를 만들고, 에이전트가 전체 저장소를
수정한 뒤 candidate artifact를 생성한다. 저비용 screening에서 가능성을 확인한 후보만
다중 seed 확인 실험으로 올린다. 실패하거나 성능이 나빠진 변경은 자동 폐기하고 champion
상태로 되돌아간다.

단일 scalar leaderboard만 최적화하지 않는다. `ResearchDomain.compare()`는 primary
metric 개선과 guardrail 충족을 분리하며, 어떤 지표를 선택했는지는 REPORT에 설명한다.
승격 임계값은 고정 비율이 아니라 baseline을 seed만 바꿔 반복했을 때의 노이즈 표준편차
`σ`의 배수로 정의한다.

| 판정 | 조건 |
| --- | --- |
| `promote` | primary 평균이 baseline보다 `2σ` 이상 개선되고 모든 guardrail 델타가 `-1σ` 이상 |
| `revise` | primary 평균이 `2σ` 이상 개선됐지만 하나 이상의 guardrail 델타가 `-1σ` 미만 |
| `discard` | 그 외 |

고정 비율은 그 값이 실제 seed 잡음보다 큰지 알려 주지 않는다. 자율 루프가 수십 trial을
반복하면 우연히 좋아 보이는 결과가 누적되므로, baseline에서 실측한 잡음에 상대적인
임계값을 사용한다.

### 7.1 실패 처리

실행 중 실패는 사용자 질문으로 전환하지 않는다.

- 논문 원문 없음: `abstract_only`로 낮추거나 다음 논문 선택
- 현재 데이터로 검증 불가: 필요 capability를 기록하고 다음 후보 선택
- 코드 실행 실패: 제한 횟수만큼 오류를 관찰해 수정
- lint/test 실패: trial 예산 안에서 수정 후 재검증
- timeout/OOM: fidelity 또는 자원 설정을 축소해 제한 재시도
- 데이터 계약·누수 실패: candidate 즉시 차단
- 지표 악화: 변경 폐기 후 다음 가설 선택
- 전체 후보 기각: `개선 없음` REPORT 생성

각 단계는 idempotent checkpoint를 가져 프로세스 종료 후 마지막 완료 단계부터 재개할 수
있어야 한다. Job 전체를 처음부터 다시 실행하는 방식은 LLM 호출과 학습 결과를 불필요하게
잃으므로 기본 재시도 단위로 사용하지 않는다.

## 8. YouTube 리랭킹 Domain Adapter

YouTube MVP의 목표는 CTR 확률 숫자만 낮은 오차로 맞히는 것이 아니라 사용자별 후보
영상의 순서를 개선하는 것이다. 사용자가 세부 지표를 선택하지 않아도 adapter가 다음
기본값을 제공한다.

- primary: `NDCG@10`
- ranking guardrail: `Recall@10`, `NDCG@24`, grouped ROC-AUC
- probability guardrail: LogLoss, Brier Score, PR-AUC
- robustness: 사용자·카테고리 그룹별 성능, seed 간 평균·분산, 평가 coverage

현재 action log에는 유저별 노출 묶음과 선택 필드인 `rank`·`exposure_source`가 있지만
명시적인 `slate_id`는 없다. 최종 학습 CSV에는 평가 전용 `user_id`만 보존되고
`video_id`와 slate 경계는 남지 않으며, NDCG·Recall 판정 경로도 아직 없다. 따라서 자율
연구 loop의 선행 기반으로 고정된 `EvaluationSlateSnapshot`을 만들어야 한다.

```text
EvaluationSlateItem
  evaluation_id
  slate_id
  user_id
  video_id
  event_timestamp
  clicked
  candidate_source
  original_rank

CandidateRanking
  evaluation_id
  slate_id
  video_id
  score
```

`EvaluationSlateItem.slate_id`는 유저별 후보 노출 묶음이 확정되는 action log 생성 시점에
부여한다. 사후에 timestamp나 rank로 추론하지 않으며, `slate_id`가 없는 과거 파티션은
평가 대상에서 제외한다. slate 경계는 노출 시점에만 존재하는 사실이고 사후 추론은
근사에 불과하며, NDCG가 slate 단위로 계산되기 때문에 잘못된 경계는 오류 없이 지표를
왜곡한다.

Judge는 같은 slate와 같은 label로 baseline과 candidate를 평가한다. candidate가 자체
split이나 evaluator를 바꾸어도 최종 평가에는 영향을 주지 못한다. 더 전문적인
counterfactual label이나 graded relevance는 MVP 완주 이후 Domain Adapter의 버전된
평가 계약으로 검토한다.

## 9. Trial Ledger와 재현성

Trial Ledger는 REPORT의 근거이자 다음 iteration의 memory다. 최소한 다음을 기록한다.

- research request와 budget
- 기준 commit, candidate commit, 전체 diff fingerprint
- 선택한 PaperCard와 탈락한 후보
- 논문 claim에서 변환된 가설과 falsification 조건
- 데이터 snapshot, 파생 데이터 lineage, split과 evaluation fingerprint
- 실행 환경, dependency lock, seed, 소요 시간과 자원 사용량
- stdout/stderr 요약과 실패 reason code
- 모든 지표와 Judge의 `keep/discard/revise` 근거
- champion lineage와 checkpoint

외부 데이터를 허용하는 후속 버전에서는 URL, 조회 시점, 라이선스, checksum을 함께
기록한다. 출처와 생성 과정을 재현할 수 없는 데이터는 최종 candidate 근거로 사용할 수
없다.

## 10. 최종 REPORT 계약

REPORT는 최고 점수만 보여주는 결과 페이지가 아니라 논문에서 candidate까지의 감사 가능한
research lineage다.

1. Executive Summary
   - champion 대비 최종 결과
   - 실행 시간, trial 수, 사용 자원
   - 개선·개선 없음·판정 불가 중 최종 결론
2. Research Brief
   - 사용자가 제출한 목표와 Domain Adapter가 선택한 평가 기준
3. Paper Discovery
   - 검색 source와 query
   - 발견·선택·탈락 논문 및 사유
4. Experiment Lineage
   - 논문 → claim → 가설 → 코드 변경 → 결과 → 판정
5. Metric Comparison
   - primary, guardrail, seed별 값과 변동성
6. Safety and Validity
   - 데이터 누수, split, snapshot, Judge 검증
7. Negative Findings
   - 실패한 가설, 실패 원인, 다시 시도할 조건
8. Final Candidate
   - 최종 diff, artifact, 재현 명령, champion lineage
9. Reference Ledger
   - 공식 출처와 실험별 인용 관계

각 실험 설명에는 `[P03]` 같은 인라인 인용을 붙인다. Reference Ledger에는 제목, 저자,
연도, DOI/arXiv ID, 공식 URL, 검토 범위, 조회 시점, 연결된 hypothesis/trial, 최종 판정을
포함한다. 논문의 아이디어를 그대로 재현한 것인지 프로젝트 조건에 맞게 변형한 것인지도
명시한다.

원문 전체를 REPORT에 복제하지 않는다. 근거 위치와 짧은 요약만 제공하며,
`abstract_only` 논문을 원문 검토 논문처럼 서술하지 않는다.

## 11. MVP 범위

### 포함

- 추상 `ResearchDomain`과 YouTube 구현 1개
- OpenAlex/arXiv 기반 논문 자동 발견과 Crossref 식별자 해소
- PaperCard, capability matching, 출처 provenance
- 논문에서 검증 가능한 ExperimentCard 생성
- 전체 연구 저장소를 수정할 수 있는 coding agent
- 현행 verifier의 정적 allowlist 사전 검문을 Harness의 봉인된 사후 판정으로 대체
- LocalRunner 기반 반복 실험
- screening과 확인 실험
- keep/discard/revise, checkpoint, 실패 복구
- 외부 Sealed Judge와 YouTube 리랭킹 평가 snapshot
- Trial Ledger와 출처가 연결된 최종 REPORT
- 기존 웹 서비스에서 research request 제출과 최종 REPORT 열람

### 제외

- 실제 이커머스·뉴스피드 adapter
- Kubernetes를 필수 실행 경로로 만드는 작업
- 대규모 분산 학습과 multi-GPU scheduling
- production 자동 배포·무인 champion 전환
- 실제 사용자 온라인 A/B 테스트
- 유료 논문 원문 우회 수집
- 범용 웹 검색 결과를 검증 없이 연구 근거로 사용하는 기능
- Research Harness나 실행 중인 Sealed Judge를 에이전트가 자기 수정하는 기능

## 12. MVP 완료 조건

- [ ] 자연어 목표와 예산만으로 research run을 시작한다.
- [ ] 서비스가 논문을 자동 발견하고 선택·탈락 이유를 기록한다.
- [ ] 선택한 논문 하나 이상을 코드 변경이 있는 검증 가능한 가설로 변환한다.
- [ ] 에이전트가 저장소 전체 범위에서 candidate를 만들 수 있다.
- [ ] 이전 결과를 관찰해 서로 다른 trial을 순차 실행한다.
- [ ] 의도적으로 깨진 candidate에서 자동 복구하고 다음 trial을 계속한다.
- [ ] 동일한 Sealed Judge로 baseline과 candidate를 비교한다.
- [ ] 프로세스를 중단한 뒤 마지막 checkpoint부터 재개한다.
- [ ] 개선 여부와 무관하게 최종 REPORT를 생성한다.
- [ ] REPORT의 논문, 수치, commit, snapshot이 원본 manifest·ledger와 일치한다.
- [ ] 사람의 중간 승인 없이 시작부터 REPORT까지 완주한다.

## 13. 오픈소스 차별점

오픈소스 가치의 중심은 웹에서 작업을 제출하거나 자리를 비워도 실행이 계속되는 기능이
아니다. 다음 연구 방법론과 재현 가능한 reference implementation이 차별점이다.

- paper-grounded hypothesis generation
- repository capability-aware paper selection
- full-repository mutation with an external sealed evaluator
- iterative experiment control loop and failure recovery
- negative-result-aware trial memory
- paper-to-code-to-metric provenance
- local-first runner와 교체 가능한 원격 backend
- 한 도메인에 최적화되면서도 adapter로 확장 가능한 구조

## 14. 이력서·데모 포지셔닝

프로젝트는 CTR 연구 전문성을 과장하기보다 자율 에이전트 시스템과 재현 가능한 ML 실험
설계를 강조한다.

> 논문을 자동 발견하고 코드 변경·모델 학습·반복 평가·실패 복구를 수행한 뒤, 실험
> 계보와 참고문헌을 포함한 보고서를 생성하는 자율 ML 연구 에이전트를 설계·구현했다.

가장 중요한 데모는 논문 수나 단일 최고 점수가 아니다. 서비스가 관련 논문을 발견하고,
현재 데이터로 검증 가능한 가설을 선택하고, 저장소를 수정하고, 실패를 복구하며 여러
trial을 수행한 뒤, 채택 또는 기각 근거와 출처가 연결된 REPORT를 생성하는 한 번의 완전한
실행이다.

## 15. 구현 순서 원칙

구현은 [로컬 Research Harness MVP 계획](../plans/2026-08-15-local-research-harness-mvp.md)의
의존 순서를 따른다. 로컬 Harness를 신규로 만들고 현행 Kubernetes executor는 이번
범위에서 수정하지 않으며, 이후 `ExperimentRunner` 구현체로 흡수한다.

1. Sealed Judge와 `EvaluationSlateSnapshot`
2. Trial Ledger와 checkpoint 가능한 Research Controller
3. `ResearchDomain`과 YouTube adapter
4. LocalRunner와 disposable candidate workspace
5. 반복 `keep/discard/revise` loop
6. Paper Discovery와 PaperCard
7. Capability Matcher와 ExperimentCard
8. 출처가 연결된 최종 REPORT
9. 기존 웹 workbench 연결
10. 필요가 실측된 뒤 KubernetesJobRunner 연결

구현 착수 전 기존 관련 spec과의 대체·확장 관계를 이슈에서 확정하고, 저장소 workflow에
따라 이슈에서 생성한 브랜치와 구현 plan을 사용한다.
