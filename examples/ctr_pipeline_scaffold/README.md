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
- `event_log.csv`: 6,000건 노출 + 클릭 레이블
- `video_topic.csv`: 영상 주제 추출 (Topic Vocabulary 키워드 매칭)
- `user_preferred_topics.csv`: 사용자 관심사 추출

```bash
python 02_generate_event_log.py
```

**검증**: 
- `data/event_log.csv` 생성 ✓
- `clicked=1` 비율: ~2.45% (목표: 약 2% 내외) ✓

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
- 행 수: 6,000건
- 컬럼 수: 16개 (age_group, occupation, ... , clicked)
- NULL 체크: historical_category_affinity는 'unknown'으로 모두 채워짐 (NULL 없음) ✓
- clicked ratio: ~2.45%
```

### 핵심 검증 결과

#### Cold-start Policy 검증 ✓

**발견**: Mock 데이터셋에서도 신규 유저(초기 클릭 이력 없음)의 `historical_category_affinity`가 약 30% 수준에서 NULL 발생

**조치**: 스펙에 Cold-start Policy 추가
- `historical_category_affinity` NULL → `'unknown'` (COALESCE)
- `category_match`: historical_category_affinity = 'unknown' → 무조건 0 강제

**결과**: training_dataset.csv의 `historical_category_affinity`에 NULL 없음 ✓

#### 모델 성능 검증 ✓

Baseline Logistic Regression:
- ROC-AUC: 0.61 (랜덤 0.5 대비 유의미)
- Feature Importance Top 3:
  1. `topic_similarity` (계수: 4.41) ← 가장 강한 신호
  2. `recent_click_count_7d` (계수: 0.18)
  3. `category_match` (계수: 0.08)

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

**현재**: Jaccard Similarity (Set 교집합/합집합)

```python
def jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)
```

**교체**: 다른 알고리즘 실험 가능
- Cosine Similarity (임베딩 기반)
- BM25 (텍스트 매칭)
- Cross-Encoder 점수

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
