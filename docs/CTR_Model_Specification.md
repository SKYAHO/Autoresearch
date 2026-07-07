# CTR 모델 명세

## 📌 Modeling

- **Target**: 특정 user_id가 특정 video_id를 노출받았을 때 클릭할 확률 예측
- **Input**
  - API Server: user_id
  - Model: features(user, video, interaction)  
  서버가 `user_id`를 받아 Feature Store에서 값을 조립한 뒤, 조립된 features만 모델에 전달
- **Output**: 영상별 클릭 확률 (추천 리스트 X)
- **Post-processing**: click_probability 기준 정렬 후 Top-N 추출, 필요하면 exploration 아이템 일부 섞기

---

## 📌 Data Generation Contract

Training Dataset은 **Agent Simulator Specification**에서 생성된 **Raw Action Log**를 입력으로 사용한다.

[SKYAHO/Autoresearch/docs/AGENT_SIMULATOR_SPEC.md](https://github.com/SKYAHO/Autoresearch/blob/main/docs/AGENT_SIMULATOR_SPEC.md)

- Raw Action Log 생성 규칙(노출 정의, `clicked` 생성 정책, Phase 1/2 차이)은 해당 문서를 따르며, 본 문서에서 재정의하지 않는다.
- Raw Action Log 상의 노출(Impression, Phase 2 기준)은 **추천 서버가 Candidate Pool(예: Trending 상위 200개)을 재랭킹한 뒤 반환한 Top-N 추천 리스트**를 기준으로 생성된다. 따라서 Candidate Pool 전체가 아니라, 서버가 실제 반환한 Top-N만 Raw Action Log의 row로 기록된다.
- 본 문서에서는 이 Raw Action Log를 어떻게 **Feature와 Label로 활용해 Training Dataset을 구성하고 모델을 학습하는지**만 다룬다.

> **Single Source of Truth 원칙**: 노출/라벨 생성 방식이 바뀌면 [Agent Simulator Specification](https://github.com/SKYAHO/Autoresearch/blob/main/docs/AGENT_SIMULATOR_SPEC.md)만 수정하며, 본 문서는 그 변경을 그대로 참조하므로 별도 수정이 필요 없다.

---

## 📌 Feature Engineering

> Raw Data 기준으로 모델이 사용할 Feature를 정의한다 (어떤 컬럼을 어떻게 만들 것인지)

### Rule

- 스칼라(Category/Numeric/Binary/Float)가 아닌 산출물(List, Vector 등)은 모델에 직접 입력하지 않고, Similarity Feature를 생성하기 위한 **Intermediate Artifact**로만 사용한다.
- Raw Action Log 기반 User Feature는 반드시 **label timestamp 이전 이벤트만** 사용하여 생성한다.
- Interaction Feature는 **Training과 Serving에서 동일한 로직**으로 생성하여 Training-Serving Skew를 방지한다.
- Similarity 계열 Feature는 특정 구현 방식에 고정하지 않고, **Score 단위로 추상화**하여 정의한다. Baseline 구현은 명시하되, Auto Research를 통한 대체 방식 실험(Cosine, BM25, Cross-Encoder 등)이 가능하도록 문서 구조를 열어둔다.
- `preferred_topics`의 각 키워드가 축약어/은어/다의어인 경우, LLM 추출 단계에서 **disambiguation phrase를 병기**하여 저장한다 (예: `"롤"` → `"롤(리그오브레전드) 게임"`)
- LLM 호출, embedding 생성, 긴 text parsing, Cross-Encoder 계산과 같은 고비용 연산은 online serving 시점에 수행하지 않는다. 이러한 값은 offline/batch 단계에서 미리 생성되어 Feature Store 또는 BigQuery source table에 저장되어 있어야 한다.

### Raw Data

#### Video
- Kaggle Trending Dataset
- YouTube Data API
- `video_id`, `title`, `description`, `channelTitle`, `publishedAt`, `categoryId`, `tags`, `viewCount`, `likeCount`, `commentCount`, `duration`, `language`

#### User Static Feature
- **User Static Feature**: Agent Simulator / User Feature Specification에서 생성된 Virtual User 또는 User Static Feature 테이블을 입력으로 사용한다. 본 문서에서는 생성 방법을 정의하지 않는다.
- `preferred_category`는 persona를 기반으로 LLM이 YouTube 카테고리 15개 중 관심 순서 벨트 1~3개를 직접 선택해 생성한다. 생성 규칙은 본 문서에서 정의하지 않으며, User Feature Specification을 따른다.

#### Raw Action Log
- **Raw Action Log**: Agent Simulator Specification에서 생성된 Raw Action Log를 입력으로 사용한다. 본 문서에서는 로그 생성 방법과 세부 스키마를 정의하지 않는다.

> User Static Feature 생성, User Dynamic Feature 생성, Raw Action Log 생성/스키마 정의는 Agent Simulator / User Feature Specification을 참조한다.

---

### 📁 Video Feature

| Feature | Type | 사용되는 원본 컬럼 및 생성 방법 |
|---------|------|----------------------------|
| `category_id` | Category | `categoryId` 원본 |
| `duration_sec` | Numeric | `duration` → 초 단위 변환 |
| `view_count` | Numeric | `viewCount` 원본 |
| `like_ratio` | Float | `likeCount` / `viewCount` |
| `comment_ratio` | Float | `commentCount` / `viewCount` |
| `days_since_upload` | Numeric | 학습 기준일(또는 노출 시점) - `publishedAt` |

### 📁 User Feature

User Feature 세부 생성 규칙은 본 문서의 담당 범위가 아니므로 깊게 정의하지 않고, Feast의 Feature View 기준으로만 설명한다.

#### UserStaticView

- **Source**: `BigQuery.user_static_feature` (또는 `BigQuery.virtual_user`)
- **Entity**: `user_id`
- **주요 Feature**: `age_group`, `occupation`, `preferred_category`, `preferred_topics`, `user_keyword_embeddings`
- **생성/갱신**: persona 기반 초기 생성 또는 batch

#### UserDynamicView

- **Source**: `BigQuery.user_dynamic_feature`
- **Entity**: `user_id`
- **주요 Feature**: `recent_click_count_7d`, `recent_watch_time_7d`, `recent_like_count_7d`, `historical_category_affinity(가장 많이 클릭한 카테고리)`
- **생성/갱신**: Raw Action Log 기반 지속, event-driven 또는 batch
- **주의**: 반드시 **label timestamp 이전 이벤트만** 사용해야 한다.

#### Cold-start Policy

`historical_category_affinity(가장 많이 클릭한 카테고리)`는 사용자의 과거 클릭 이력(Raw Action Log)을 기반으로 생성되므로, 아직 클릭 이력이 없는 신규 사용자(또는 label timestamp 이전 클릭 이력이 없는 경우)는 이 값이 존재하지 않는다. 이 경우 `"unknown"`으로 채운다. Interaction Feature의 `historical_category_match`는 `historical_category_affinity`가 `"unknown"`이면 무조건 `0`으로 강제한다 (비교 불가 상태와 실제 불일치 상태를 혼동하지 않기 위함).

#### historical_category_affinity 집계 요구사항
`historical_category_affinity`는 point-in-time correctness(label timestamp 이전 이벤트만 사용)만 만족하면 되며, 특정 집계 윈도우(누적 전체 기간 vs N일 슬라이딩 윈도우)를 본 문서에서 강제하지 않는다. 다만 다음 두 가지 요구사항을 만족해야 한다.

- 안정성 하한: 너무 짧은 윈도우(예: 1~2일)로 집계할 경우 값이 자주 뒤집혀 historical_category_match가 노이즈성 신호가 될 수 있으므로, 최소 7일 이상의 관측 기간을 반영해야 한다.
- recent_click_count_7d와의 독립성: historical_category_affinity가 recent_click_count_7d와 동일한 윈도우·동일한 원본 집계에서 파생될 경우 두 피처가 사실상 같은 정보를 중복 인코딩하게 되므로, 가능하면 서로 다른 관측 기간(예: recent_click_count_7d는 7일, historical_category_affinity는 그보다 긴 누적 또는 30일 이상)을 사용해 신호의 독립성을 확보한다.



> ⚠️ UserDynamicFeature 생성 SQL이나 Raw Action Log aggregation 세부사항은 본 문서에서 정의하지 않는다.

### 📊 Intermediate Artifacts

모델 입력 Feature가 아닌 Similarity 계산을 위한 중간 산출물

| Artifact | Type | 생성 방법 | 사용 목적 |
|----------|------|---------|---------|
| `preferred_topics` | List[str] | persona 텍스트(`sports_persona`, `arts_persona`, `travel_persona`, `culinary_persona`, `family_persona`, `hobbies_and_interests` 등) → LLM 기반 관심 키워드 추출. 축약어/은어/다의어는 **disambiguation phrase 병기** | `topic_similarity` 계산 |
| `user_keyword_embeddings` | List[Vector] | `preferred_topics`의 각 키워드(phrase)를 **개별로** Sentence Transformer 인코딩 (리스트를 하나로 합쳐서 인코딩하지 않음) | `topic_similarity` 계산 (max-pool) |
| `category_description` | Text (15개 고정) | YouTube 카테고리 15개 각각에 대해 사람이 직접 작성하거나 LLM 이용해 작성한 설명 문장. 1회 작성 후 고정 사용 | 카테고리를 "설명문"으로 확장해 다의성/문맥 부여 |
| `category_description_embedding` | Vector (15개 고정) | `category_description` → Sentence Transformer 인코딩. 카테고리가 고정이므로 1회만 생성 후 저장 | `topic_similarity` 계산 |

#### preferred_topics Disambiguation 처리 기준

| 키워드 성격 | 처리 | 예시 |
|-----------|-----|------|
| 이미 맥락이 충분한 phrase | 그대로 사용 | `리액트 프로그래밍`, `무등산 등산 길 추천` |
| 축약어/은어/다의어 | 괄호로 원어·맥락 병기 | `롤` → `롤(리그오브레전드) 게임`, `배그` → `배그(배틀그라운드) 게임`, `먹방` → `먹방(음식 먹는 방송) 청청` |

#### 설계 근거

카테고리를 단어가 아닌 설명문으로 확장한 이유:

1. **다의성 문제**: `"게임"` 같은 짧은 단어만으로는 (1) 다의성 문제 (`"롤"`이 LOL/Role/롤케이크 중 무엇인지 모름) 발생
2. **상위-하위 개념 간극**: 모델이 "롤이 게임의 하위 개념"이라는 걸 스스로 보증해주지 않음

설명문에 구체적 인스턴스(게임명 등)를 미리 포함시키면 두 문제가 동시에 해결된다.

#### Category Description (15개 전체)

| category_id | 카테고리명 | 설명문 |
|-------------|-----------|------|
| 1 | Film & Animation | 이 카테고리는 영화·애니메이션 콘텐츠입니다. 영화 리뷰, 애니메이션, 단편영화, 영화 해금 토론을 다룹니다. |
| 2 | Autos & Vehicles | 이 카테고리는 자동차·오토바이 콘텐츠입니다. 차량 리뷰, 정비기, 튜닝, 드라이브 블로그를 다룹니다. |
| 10 | Music | 이 카테고리는 음악 콘텐츠입니다. 뮤직비디오, 커버곡, 라이브 공연, K-POP을 다룹니다. |
| 15 | Pets & Animals | 이 카테고리는 반려동물 콘텐츠입니다. 강아지, 고양이, 동물 블로그, 반려동물 훈련을 다룹니다. |
| 17 | Sports | 이 카테고리는 스포츠 콘텐츠입니다. 축구, 야구, 농구, 올림픽, 경기 하이라이트를 다룹니다. |
| 19 | Travel & Events | 이 카테고리는 여행·행사 콘텐츠입니다. 국내여행, 해외여행, 여행 블로그, 축제, 캠핑을 다룹니다. |
| 20 | Gaming | 이 카테고리는 게임 콘텐츠입니다. 롤(리그오브레전드), 배틀그라운드, 게임 공략, e스포츠, 게임 스트리밍을 다룹니다. |
| 22 | People & Blogs (Default) | 이 카테고리는 일상 블로그 콘텐츠입니다. 개인방송, 일상 공유, 일대기를 다룹니다. |
| 23 | Comedy | 이 카테고리는 코미디 콘텐츠입니다. 개그, 코윤, 패러디, 상황극, 몰래카메라를 다룹니다. |
| 24 | Entertainment | 이 카테고리는 예능 콘텐츠입니다. 오디션, 챌린지, 리액션, 버라이어티를 다룹니다. |
| 25 | News & Politics | 이 카테고리는 뉴스·정치 콘텐츠입니다. 뉴스, 정치 이슈, 시사 토론을 다룹니다. |
| 26 | Howto & Style | 이 카테고리는 뷰티·라이프스타일 콘텐츠입니다. 메이크업, 패션, DIY, 요리교법을 다룹니다. |
| 27 | Education | 이 카테고리는 교육 콘텐츠입니다. 강의, 학습법, 자격증 준비, 언어학습을 다룹니다. |
| 28 | Science & Technology | 이 카테고리는 과학·기술 콘텐츠입니다. IT, 프로그래밍, 과학실험, 전기술, 전자기기 리뷰를 다룹니다. |
| 29 | Nonprofits & Activism | 이 카테고리는 사회활동 콘텐츠입니다. 사회활동, 환경운동, 사회이슈 캠페인을 다룹니다. |

> `category_description`/`category_description_embedding`은 entity(`user_id`/`video_id`)에 종속되지 않는 **고정 참조 데이터**이므로, Feast Feature View가 아니라 별도 정적 참조 테이블(예: `BigQuery.category_reference`) 또는 코드 내 상수로 관리한다. (아래 Feature Store Contract 참고)

### 📊 Interaction Feature

| Feature | Type | Feature Store | 생성 방법 |
|---------|------|----------------|---------|
| `historical_category_match` | Binary | N/A (Derived) | `historical_category_affinity == category_id`이면 1, 아니면 0 (행동 이력 기반) |
| `preferred_category_match` | Binary | N/A (Derived) | `category_id ∈ preferred_category`이면 1, 아니면 0 (persona 선호 기반) |
| `topic_similarity` | Float | N/A (Derived) | **Topic Similarity Score.** 사용자 `user_keyword_embeddings`(키워드별 벡터 여러 개) 각각을 영상의 `category_id`에 해당하는 `category_description_embedding` 1개와 cosine 유사도 비교한 다음, **가장 높은 값 하나(max-pool)**를 최종 스코어로 사용한다. (아래 예시 참고) |

#### topic_similarity (max-pool) 예시

사용자 키워드 = `["롤(리그오브레전드) 게임", "마작 핑방", "먹방(음식 먹는 방송) 청청"]`, 영상 카테고리 = `Gaming`일 때:

| 사용자 키워드 | Gaming 카테고리에서의 유사도 |
|-------------|---------------------------|
| 롤(리그오브레전드) 게임 | 0.82 |
| 마작 핑방 | 0.11 |
| 먹방(음식 먹는 방송) 청청 | 0.15 |

→ 사용자 관심사가 여러 개이지만, 이 영상 카테고리에 **가장 관련 있는 단 하나의 키워드 하나만 받아**들이므로 최종 `topic_similarity` = **0.82** (최댓값 선택).

> **Note**: `topic_similarity`의 인코더 모델은 미확정(TBD)

#### ⚠️ 주의

- Raw Action Log 자체는 **최종 모델 입력 Feature 자체가 아님**
  - Training Dataset을 만들기 위한 역할
  - **언제, 누가, 어떤 영상에, 클릭했는지**를 알려주는 join key + label 소스
- **Interaction Feature는 User와 Video Feature를 Join할 때 생성됨**
  - 생성 시점: Training Dataset 생성 시, Serving 시 추론 직전
  - Interaction Feature는 조회된 결과 두 개(User/Video Feature)를 갖고 그 자리에서(on-the-fly) 계산되는 것이지, 별도 테이블에 미리 저장해두는 게 아님
- **"on-the-fly"의 의미**: 고비용 feature generation이 아니라, **이미 조회된 feature/artifact 값을 가벼운 조합 연산**(단순 비교, set similarity, vector dot product 수준)이다.
  - Baseline에서 serving 시 LLM 호출, embedding 생성, 긴 text parsing, Cross-Encoder 계산을 하지 않는다.
  - `category_description_embedding`은 사전 계산된 15개 고정 벡터, `user_keyword_embeddings`는 persona 공급 단계에서 미리 생성된 벡터이므로 serving 시에는 저장된 값을 조회해 cosine 계산만 수행

---

## 📌 Feature Store Contract

각 Feature 그룹의 **저장 위치, 갱신 주기, 책임 주체**를 명시한다.

### 세부 Contract

- **Feast Offline Store**: BigQuery를 source로 사용한다.
- **Feast Online Store**: Redis/Valkey를 사용한다.
- **Offline retrieval**: BigQuery 기반 historical feature retrieval을 수행한다.
- **Online serving**: Redis/Valkey에 materialize된 feature를 조회한다.
- **Raw Action Log 자체**: Feature View가 아니라, UserDynamicFeature 생성 및 Label 생성의 원본 데이터다.
- **카테고리 참조 데이터**: `category_description`, `category_description_embedding`는 `user_id`/`video_id` 같은 entity에 종속되지 않는 고정 15개 데이터이므로 Feast Feature View로 등록하지 않고, 별도 정적 테이블 또는 코드 내 상수로 관리한다.

### Feature 매핑 대응표

| BigQuery Source Table | Feast Feature View | Entity | 주요 Feature | 갱신 주기 | 책임 |
|----------------------|-------------------|--------|-----------|---------|-----|
| user_static_feature (또는 virtual_user) | UserStaticView | user_id | `age_group`, `occupation`, `preferred_category`, `preferred_topics`, `user_keyword_embeddings` | batch / initial load | User pipeline |
| user_dynamic_feature | UserDynamicView | user_id | `recent_click_count_7d`, `recent_watch_time_7d`, `recent_like_count_7d`, `historical_category_affinity` | event-driven or batch | User/action log pipeline |
| video_feature | VideoFeatureView | video_id | `category_id`, `duration_sec`, `view_count`, `like_ratio`, `comment_ratio`, `days_since_upload` | batch | Video pipeline |
| category_reference | 정적 참조 데이터, Feature View 아님 | (없음) | `category_description`, `category_description_embedding` (15개 고정) | 최초 작성 후 1회 | 모델링 담당 |

---

## 📌 Training Dataset

### Feast Historical Retrieval 기준 Training Dataset 생성 절차

1. Raw Action Log에서 impression event를 추출한다.
2. Impression event를 기준으로 entity dataframe을 구성한다.
   - `user_id`
   - `video_id`
   - `event_timestamp`
   - `clicked` label
3. `clicked` label을 동일 request/session/window 내 click event를 join하여 생성한다. (Raw Action Log 자체에 `clicked`가 저장되어 있지 않다)
4. Feast `get_historical_features()`를 사용해 `UserStaticView`, `UserDynamicView`, `VideoFeatureView`를 point-in-time join한다.
5. 조회된 User/Video Feature를 바탕으로 Interaction Feature를 계산한다.
6. 최종 Training Dataset을 생성한다: `[User Feature | Video Feature | Interaction Feature | clicked]`
7. 필요하면 결과를 parquet/csv로 저장해 오프라인 재현성을 보장한다.

> Training Dataset은 반드시 사전에 BigQuery 테이블로 존재해야 하는 것은 아니다. Feast historical retrieval의 결과로 생성되는 DataFrame이 Training Dataset이 된다. 다만 오프라인 재현성과 재사용성을 위해 생성 결과를 parquet 또는 BigQuery table로 저장할 수 있다.

### 최종 Model Input Columns

| Column | Type | 설명 |
|--------|------|------|
| `age_group` | Category | 사용자 연령대 |
| `occupation` | Category | 사용자 직업 |
| `historical_category_affinity` | Category | 과거 행동 기반 선호 카테고리 |
| `recent_click_count_7d` | Numeric | 최근 7일 클릭 수 |
| `recent_watch_time_7d` | Numeric | 최근 7일 총 시청 시간 |
| `recent_like_count_7d` | Numeric | 최근 7일 좋아요 수 |
| `category_id` | Category | 영상 카테고리 |
| `duration_sec` | Numeric | 영상 길이 |
| `view_count` | Numeric | 영상 조회수 |
| `like_ratio` | Float | 영상 좋아요 비율 |
| `comment_ratio` | Float | 영상 댓글 비율 |
| `days_since_upload` | Numeric | 업로드 후 경과일 |
| `historical_category_match` | Binary | **과거 행동 기반** 선호 카테고리(`historical_category_affinity`)과 영상 카테고리 일치 여부 |
| `preferred_category_match` | Binary | **persona 기반** 선호 카테고리(`preferred_category`)과 영상 카테고리 일치 여부 |
| `topic_similarity` | Float | 사용자 키워드별 임베딩과 영상 카테고리 설명 임베딩 간 cosine 유사도 중 최댓값(max-pool) |
| `clicked` | Binary | Label |

---

## 📌 Model / Evaluation

### Model

| Model | 목적 |
|-------|------|
| Logistic Regression | Baseline |
| LightGBM | Main Model |
| XGBoost | 비교 실험 |
| CatBoost | Future |
| DeepFM | Future |

### Evaluation Metric

**CTR**:
- ROC-AUC
- Log Loss
- PR-AUC

**추천 성능**:

Training Dataset의 한 행은 `(user_id, video_id, clicked)` pointwise 구조이므로, Recall@10 / NDCG@10을 계산하려면 별도의 **랭킹 변환 단계**가 필요함:

1. 평가 시점에 한 사용자에 대해 후보 영상 집합(예: 그 시점 Trending 상위 200개)에 대한 `click_probability`를 모두 계산
2. 확률 기준으로 재랭킹
3. 실제 클릭한 영상이 Top-10 안에 포함되는지(Recall@10) / 어느 순위에 위치하는지(NDCG@10)를 평가

즉 pointwise 예측값을 사용자 단위로 그룹핑한 뒤 랭킹 지표로 변환하는 로직이 별도로 필요함

---

## 📌 Training / Inference Flow

### Training Flow

```
Raw Data
  ↓
Feature Engineering
  ↓
BigQuery Source Tables
  ↓
Feast Feature Views
  ↓
Historical Feature Retrieval
  ↓
Interaction Feature Calculation
  ↓
Training Dataset
  ↓
Train
  ↓
Evaluate
  ↓
MLflow
  ↓
Registry
```

### Inference Flow

```
Request: user_id
  ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━
Candidate Pool 생성 또는 조회
  - Trending Top-N (예: 200개)
  - candidate generation 모델은 본 프로젝트 범위 밖
  ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━
Feast Online Store에서 user feature 조회
  - UserStaticView
  - UserDynamicView
  ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━
Feast/serving cache에서 video feature 조회
  - VideoFeatureView
  ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━
user 1명 × video (candidates) N개 행렬의 scoring dataframe 생성
  ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━
Interaction Feature 계산
  - historical_category_match
  - preferred_category_match
  - topic_similarity
  ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━
Model predict
  ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━
click_probability 기준 sort
  ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━
Top-N 반환 (+ 필요하면 exploration 아이템 일부 혼합)
```

#### 설계 원칙

고비용 feature generation은 offline/batch에서 수행하고, online serving에서는 precomputed feature를 조합해 lightweight interaction만 계산한다.

Inference 시점에는 영상 1개가 아니라 **Candidate Pool(Trending Top-N) 전체**에 대해 사용자 1명 기준으로 `click_probability`를 병렬 계산한 뒤, 그 결과를 정렬해서 Top-N을 잘라낸다. 후보군을 선정하는 별도 로직(candidate generation 모델)은 본 프로젝트 범위에 포함하지 않으며, Candidate Pool은 수집된 Trending 영상 중 최근 기준 Top-N(예: 200개)으로 고정한다.

---

## 📌 Out of Scope / Dependencies

본 문서는 CTR 모델 훈련/서빙 관점의 Feature 사용 계약을 정의한다. 아래 항목은 담당 범위 밖이며, 각 담당 문서(Agent Simulator / User Feature Specification)에서 관리한다.

- Raw Action Log 세부 스키마
- impression/click/view/like/search action 생성 규칙
- session_id/request_id/exposure_type 저장 여부
- User Dynamic Feature 집계 SQL 세부사항
- Virtual User 생성 방식
- Agent Simulator가 Persona/Video를 매칭해 Action을 생성하는 방식
