# CTR Model Spec 검증용 Example 파이프라인

## 목적

`CTR_Model_Specification.md` + `AGENT_SIMULATOR_SPEC.md`가 실제로 앞뒤가 맞물려 동작하는지 검증하기 위한 **mock 파이프라인**.

- 스펙 문서의 Feature 정의가 실제 데이터에서 구현 가능한지 확인
- Cold-start Policy(historical_category_affinity NULL 처리) 동작 검증
- 팀원들이 "스펙 → 구현 예시" 흐름을 한 PR로 확인 가능

## ⚠️ 주의: 실제 구현이 아닙니다

- Raw Data 수집 아님: YouTube API 호출 X, Persona 데이터셋 실제 다운로드 X
- Agent Simulator 아님: Event Log 생성 규칙을 단순화한 mock 구현
- Feature Store 아님: CSV 기반 로컬 저장소 (실제는 Feast + BigQuery/Redis)

**각 담당자가 실제 구현을 진행할 때 이 파이프라인을 교체하세요.**

## 실행 순서

### 1️⃣ `01_generate_mock_raw_data.py`

YouTube Video + Persona Raw 데이터 생성
- `video_raw.csv`: 30개 Mock 영상 (스펙의 YouTube API 스키마 준수)
- `persona_raw.csv`: 50명 Mock 사용자 (스펙의 Persona 스키마 준수)

```bash
python 01_generate_mock_raw_data.py
```

**검증**: `data/video_raw.csv`, `data/persona_raw.csv` 생성 확인 ✓

### 2️⃣ `02_generate_event_log.py`

Event Log 시뮬레이션 (Phase 1 historical)
- `event_log.csv`: 30,000건 노출 + 클릭 레이블 (3-way split 시 test에도 충분한 양성 샘플이
  남도록 6,000건에서 증량)
- `video_topic.csv`: 영상 주제 추출 (Topic Vocabulary 키워드 매칭, QA/참고용 — 학습 피처
  계산에는 사용되지 않음)
- `user_preferred_topics.csv`: 사용자 관심사 추출 (QA/참고용)

```bash
python 02_generate_event_log.py
```

**검증**: 
- `data/event_log.csv` 생성 ✓
- `clicked=1` 비율: ~2.8% (목표: 약 2% 내외) ✓

### 3️⃣ `03_build_features_and_training_dataset.py`

Feature Engineering → Training Dataset 생성

#### 실행

```bash
pip install duckdb pandas numpy scikit-learn
python 03_build_features_and_training_dataset.py
```

#### 산출물

| 파일 | 설명 |
|---|---|
| `data/video_feature.csv` | Video Feature (category_id, duration_sec, view_count, like_ratio 등) |
| `data/user_feature_offline.csv` | User Static Feature (age_group, occupation) |
| `data/training_dataset.csv` | 최종 Model Input: User Feature + Video Feature + Interaction Feature + clicked label |

#### 검증

```bash
# training_dataset.csv 확인
- 행 수: 30,000건
- 컬럼 수: 16개 (age_group, occupation, ... , clicked)
- NULL 체크: historical_category_affinity는 'unknown'으로 모두 채워짐 (NULL 없음) ✓
- clicked ratio: ~2.8%
```

### 핵심 검증 결과

#### Cold-start Policy 검증 ✓

**발견**: Mock 데이터셋에서도 신규 유저(초기 클릭 이력 없음)의 `historical_category_affinity`가 약 30% 수준에서 NULL 발생

**조치**: 스펙에 Cold-start Policy 추가
- `historical_category_affinity` NULL → `'unknown'` (COALESCE)
- `category_match`: historical_category_affinity = 'unknown' → 무조건 0 강제

**결과**: training_dataset.csv의 `historical_category_affinity`에 NULL 없음 ✓

#### 모델 성능 검증 ✓

Train/Val/Test 3-way split (test는 학습에 전혀 노출되지 않음, `src/pipeline/train.py`,
`src/pipeline/evaluate.py` 참고):
- Val ROC-AUC: 0.75, Test(held-out) ROC-AUC: 0.76 (baseline 0.61 상회, val/test 격차 없음
  → data leakage 없음 확인)
- 클릭 라벨은 `topic_similarity`(spec 그대로: user_keyword_embeddings ↔
  category_description_embedding cosine 유사도, max-pool)에 비례하도록 생성됨 —
  라벨 생성 로직과 학습 피처 로직이 동일한 함수(`src.features.feature_builder`)를 공유해야
  mock 데이터에서 모델이 유의미한 신호를 학습할 수 있다는 점이 핵심 (아래 Placeholder 2 참고).

---

## Placeholder 목록 (실제 구현 시 교체 필요)

### 1. Topic 추출 (02_generate_event_log.py)

**현재**: Keyword matching (Topic Vocabulary에 텍스트가 포함되는지 단순 판정)

```python
def extract_topics_from_text(text: str, k_max=4):
    text_lower = text.lower()
    found = [t for t in TOPIC_VOCAB if t in text_lower]
    return found[:k_max] if found else [random.choice(TOPIC_VOCAB)]
```

**교체**: LLM 기반 (예: GPT API, Gemini, 또는 경량 분류기)

---

### 2. Similarity 계산 (02_generate_event_log.py)

**현재**: `src.features.feature_builder.compute_topic_similarity`를 그대로 재사용
(user_keyword_embeddings ↔ category_description_embedding cosine 유사도, max-pool).
클릭 라벨 생성 로직과 학습 피처 계산 로직이 반드시 같은 함수를 가리켜야 한다 — 예전에는
라벨은 Jaccard, 피처는 embedding 기반으로 서로 다른 값을 썼다가 mock 데이터에서 두 신호가
무상관이 되어 모델이 아무것도 학습하지 못하는 문제가 있었다 (이슈 #73).

**남은 placeholder**: `compute_topic_similarity`가 사용하는 `embed_text`(해시 기반
pseudo-embedding, `src/features/embeddings.py`)는 실제 semantic 정보를 담지 못한다.
실제 Sentence Transformer로 교체 시 라벨 생성 쪽 `base_rate`/`boost_coef` 튜닝
(`02_generate_event_log.py`)도 새 임베딩 분포에 맞춰 재조정이 필요할 수 있다.

**교체**: Sentence Transformer, BERT, 또는 도메인 특화 임베딩 모델로 `embed_text` 교체

---

### 3. User-Video Embedding Similarity (03_build_features_and_training_dataset.py)

**현재**: Pseudo-embedding (텍스트 해시 기반 결정론적 벡터)

```python
def pseudo_embedding(text: str, dim: int = 32) -> np.ndarray:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
    v = rng.normal(size=dim)
    return v / np.linalg.norm(v)
```

**이유**: 이 환경에서 HuggingFace 모델 다운로드 불가 → 실제 구현 시 Sentence Transformer로 교체 필수

**교체**: Sentence Transformer, BERT, 또는 도메인 특화 임베딩 모델

---

## 파일 구조

```
examples/ctr_pipeline_scaffold/
├── README.md (이 파일)
├── 01_generate_mock_raw_data.py
├── 02_generate_event_log.py
├── 03_build_features_and_training_dataset.py
└── data/
    ├── .gitkeep
    ├── video_raw.csv (생성)
    ├── persona_raw.csv (생성)
    ├── video_topic.csv (생성)
    ├── user_preferred_topics.csv (생성)
    ├── event_log.csv (생성)
    ├── video_feature.csv (생성)
    ├── user_feature_offline.csv (생성)
    └── training_dataset.csv (생성)
```

**CSV는 git에 포함하지 않습니다** (`.gitignore`에 `examples/ctr_pipeline_scaffold/data/*.csv` 추가)

---

## 다음 단계

### 실제 구현 담당자별 작업 흐름

| 담당 | 작업 | 입력 | 출력 |
|---|---|---|---|
| **Raw Data 수집** | 01 스크립트 교체 | - | YouTube API + Persona 데이터 |
| **Agent Simulator** | 02 스크립트 교체 | Raw Data | Event Log (AGENT_SIMULATOR_SPEC.md 기준) |
| **Feature Store 적재** | 03 스크립트 수정 | Event Log | Feast Feature Store (BigQuery + Redis) |
| **Model Training** | 새로 작성 | Training Dataset | LightGBM 모델 |

### Auto Research

이 파이프라인의 Placeholder들 (Topic 추출, Similarity 계산, Embedding 모델)은 Auto Research 후보입니다.
- 스펙은 구현 방식에 구애받지 않음 (추상화된 Score 단위)
- 다양한 조합을 실험한 후 최적 조합 선택 가능
