# Architecture Overview

> Last Updated: 2026-07-06

4개 도메인 아키텍처의 조감도, 핵심 설계 결정, 도메인 간 상호작용을
정리한 문서입니다. 현재 구현된 부분과 계획(별도 브랜치 진행 중)을
구분해서 읽어야 합니다.

## When To Use This Doc

- 4개 도메인이 어떻게 상호작용하는지 파악해야 할 때
- 도메인을 가로지르는 변경을 하며 제약을 이해해야 할 때
- 특정 설계 결정(예: ODFV vs FeatureView)의 이유를 알아야 할 때
- 프로젝트에 온보딩하며 전체 그림이 필요할 때

## Four Domains

```
┌─────────────────────────────────────────────────────────┐
│                  Autoresearch Project                   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Model Training      Feast Features       Airflow       │
│  (waieiches,         (waieiches,          (bbungjun)    │
│   hyochangsung)      hyochangsung)                      │
│                                                         │
│  LightGBM (계획)     ODFV FeatureView    DAG 스케줄러   │
│  CTR 예측            피처 변환 (계획)    데이터 수집    │
│                                                         │
└─────────────────────────────────────────────────────────┘
                            ↑
                 GCP Infrastructure
                    (hyeongyu-data)
              [GCS, BigQuery, 시크릿, CI/CD]
```

## Current Data Flow (구현됨)

```
YouTube Data API
    → client.py (복원력 래퍼: 재시도/Key롤링/IP밴시그니처/Cloud Run 프록시)
    → fetch.py (수집 로직)
    → transform.py (pydantic 스키마 검증)
    → load.py (GCS 데이터 레이크, parquet)

Airflow (Astro Runtime):
    dags/youtube_trending_kr_daily.py  # 일간 수집
    dags/youtube_backfill_kr.py        # 과거 데이터 백필 (Kaggle 원천)

가상 유저 (실험):
    persona 원천 → autoresearch/virtual_users/pipeline.py
    → Gemini API (gemini_generator.py) → 가상 유저 데이터셋
```

## Domain 1: Model Training (waieiches, hyochangsung)

**책임:** CTR(클릭률) 모델 정의, 학습 오케스트레이션, 평가 지표.

**상태:** 별도 브랜치 진행 중 (Issue #33). 현재 main에는
`examples/ctr_pipeline_scaffold/` 예제만 있습니다.

**주요 파일 (계획):**
- `src/models/lightgbm_model.py` — LightGBM 모델 클래스
- `src/pipeline/train.py`, `evaluate.py`, `build_training_dataset.py`
- `src/pipeline/config.yaml` — 하이퍼파라미터·경로의 단일 출처
- `docs/CTR_Model_Specification.md` — CTR 모델링 스펙 (전체 상세)

**모델링 과업:**
- **목표:** user_id가 video_id를 봤을 때의 클릭 확률 예측
- **출력:** 영상별 클릭 확률 (추천 리스트가 아님). 후처리로 확률 순
  정렬, Top-N 추출, 탐색 아이템 혼합
- **핵심 규칙** (상세는 `CTR_Model_Specification.md`):
  - 스칼라 피처만 직접 입력. 벡터/리스트는 유사도 계산에만 사용
  - 유저 피처는 라벨 시점 **이전** 이벤트로만 생성 (누수 금지)
  - Interaction 피처는 학습과 서빙에서 동일하게 계산 (skew 금지)
  - Cold-start: 이력 없는 값은 "unknown" 처리 (대치 금지)

## Domain 2: Feast Features (waieiches, hyochangsung)

**책임:** 피처 정의, 피처 스토어 구축, 피처 엔지니어링 변환.

**상태:** 도입됨 — `feature_repo/`에 Entity(`user`, `video`)와 더미 스키마
FeatureView 3개 정의 (실데이터 스키마로 교체 예정). Feast 0.64, BigQuery
offline store + Redis online store.

### Feast 핵심 설계 결정 (확정)

**1. ODFV(On-Demand Feature View) 필수**
- 실시간 변환(정규화, 버킷팅 등)은 ODFV로 구현합니다.
- 변환 목적으로 일반 `FeatureView`를 쓰지 않습니다. 모든 변형을
  사전 물질화해야 하는 안티패턴입니다.

**2. TTL ≠ 윈도우 집계**
- TTL은 피처 신선도 요구(예: 1시간마다 갱신), 윈도우는 계산
  범위(예: 최근 7일 합)입니다. 서로 독립적으로 설정합니다.

```yaml
ttl: 3600            # 신선도: 1시간
window_size: 604800  # 계산 범위: 7일
```

**3. Cold-start는 명시적 null 또는 "unknown"**
- 이력이 없는 엔티티는 0이나 평균으로 대치하지 않습니다. 명시적
  결측을 반환해 모델이 학습 시 본 희소성을 그대로 보게 합니다.

**4. Training-Serving 일관성**
- Interaction 피처는 학습 데이터셋 생성 로직과 Feast 변환 로직이
  동일해야 합니다. 리뷰에서 양쪽 로직 일치를 확인합니다.

## Domain 3: Airflow Orchestration (bbungjun)

**책임:** DAG 정의, 스케줄링, 파이프라인 오케스트레이션.

**상태:** 구현됨. 수집 DAG 2개가 운영 중입니다.

**주요 파일:**
- `dags/youtube_trending_kr_daily.py` — 일간 트렌딩 수집
- `dags/youtube_backfill_kr.py` — 과거 데이터 백필
- `airflow_settings.yaml` — 로컬 Airflow 변수
  (`YOUTUBE_LAKE_BUCKET`, `YOUTUBE_BACKFILL_SOURCE`)

**설계 제약:**
- DAG은 `src/autoresearch/` 모듈을 호출만 하고, 피처·모델 로직을
  중복 구현하지 않습니다.
- DAG은 정식 설치된 `autoresearch` 패키지를 import 합니다 (로컬은 uv
  editable 설치, 컨테이너는 `Dockerfile`의 `pip install --no-deps .`).
- 학습·평가 스크립트는 추후 DAG operator로 감싸 호출합니다(계획).

## Domain 4: GCP Infrastructure (hyeongyu-data)

**책임:** 클라우드 배포, 환경 구성, 시크릿 관리.

**주요 영역:**
- GitHub Actions CI/CD (`.github/workflows/ci.yml`, `claude.yml`)
- GCS 데이터 레이크 버킷, 서비스 계정과 IAM
- 시크릿 관리 (로컬 `.env`, CI는 GitHub Secrets)
- BigQuery, Cloud Composer (프로덕션 계획)

## Cross-Domain Interactions

- **Airflow → 수집 모듈:** DAG task가 `autoresearch.youtube_collection`
  함수를 호출합니다. 실패는 명확히 예외로 전파합니다.
- **Model Training ← Feast (계획):** 학습 스크립트가 Feast 클라이언트로
  피처를 조회합니다. 직접 SQL 조회는 금지합니다.
- **모든 도메인 ← GCP:** 자격 증명은 환경 변수 또는 GitHub Secrets로만
  전달합니다. 하드코딩을 금지합니다.

## Key Architecture Rules

1. **도메인 간 강결합 금지.** 다른 도메인의 내부 구현에 의존하지
   않습니다.
2. **설정은 단일 출처.** 파이프라인 파라미터는 설정 파일과 환경
   변수로 관리하고 코드에 하드코딩하지 않습니다.
3. **데이터 계약은 pydantic 스키마.** 모듈 간 데이터는 `schema.py`
   모델로 검증합니다.
4. **시크릿은 환경 변수.** 자격 증명, API 키, 버킷 이름을 코드에
   넣지 않습니다.
5. **데이터 스펙의 단일 출처.** Event Log는
   `docs/AGENT_SIMULATOR_SPEC.md`, 피처/라벨 정의는
   `docs/CTR_Model_Specification.md`를 따릅니다.

## Verification Checklist

- [ ] 새 코드가 올바른 도메인에 있다 (`agent-project-reference.md` 참조)
- [ ] 자격 증명·경로 하드코딩이 없다
- [ ] Feast 변환은 ODFV를 사용한다 (계획 작업 시)
- [ ] TTL과 윈도우 집계를 독립적으로 설정했다
- [ ] Cold-start 처리가 명시적이다 (null/"unknown", 대치 금지)
- [ ] 학습과 서빙의 피처 로직이 동일하다
- [ ] 스펙 문서가 데이터 정의의 단일 출처로 유지된다
