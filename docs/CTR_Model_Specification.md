# CTR 모델 명세

## 📌 Modeling

- **Target** :  특정 user_id가 특정 video_id를 노출받았을 때 클릭할 확률 예측
- **Input**
    - API Server : user_id
    - Model : features(user, video, interaction)
    서버가 `user_id`를 받아 Feature Store에서 값을 조립한 뒤, 조립된 features만 모델에 전달
- **Output** : 영상별 클릭 확률 (추천 리스트 X)
- **Post-processing** : click_probability 기준 정렬 후 Top-N 추출, 필요하면 exploration 아이템 일부 섞기

## 📌 Data Generation Contract

Training Dataset은 **Agent Simulator Event Log Specification**에서 생성된 Event Log를 입력으로 사용한다.

[Autoresearch/docs/AGENT_SIMULATOR_SPEC.md at main · SKYAHO/Autoresearch](https://github.com/SKYAHO/Autoresearch/blob/main/docs/AGENT_SIMULATOR_SPEC.md)

- Event Log 생성 규칙(노출 정의, `clicked` 생성 정책, Phase 1/2 차이)은 해당 문서를 따르며, 본 문서에서 재정의하지 않는다.
- Event Log 상의 노출(Impression, Phase 2 기준)은 **추천 서버가 Candidate Pool(예: Trending 상위 200개)을 재랭킹한 뒤 반환한 Top-N 추천 리스트**를 기준으로 생성된다. 따라서 Candidate Pool 전체가 아니라, 서버가 실제 반환한 Top-N만 Event Log의 row로 기록된다.
- 본 문서에서는 이 Event Log를 어떻게 **Feature와 Label로 활용해 Training Dataset을 구성하고 모델을 학습하는지**만 다룬다.

> Single Source of Truth 원칙: 노출/라벨 생성 방식이 바뀌면 [Event Log Specification](https://github.com/SKYAHO/Autoresearch/blob/main/docs/AGENT_SIMULATOR_SPEC.md)만 수정하며, 본 문서는 그 변경을 그대로 참조하므로 별도 수정이 필요 없다.
> 

## 📌 Feature Engineering

> Raw Data 기준으로 모델이 사용할 Feature (어떤 컬럼을 어떻게 만들 것인지 정의)
> 

<aside>

Rule

- 스칼라(Category/Numeric/Binary/Float)가 아닌 산출물(List, Vector 등)은 모델에 직접 입력하지 않고, Similarity Feature를 생성하기 위한 **Intermediate Artifact**로만 사용한다.
- Event Log 기반 User Feature는 반드시 **label timestamp 이전 이벤트만** 사용하여 생성한다.
- Interaction Feature는 **Training과 Serving에서 동일한 로직**으로 생성하여 Training-Serving Skew를 방지한다.
- Similarity 계열 Feature는 특정 구현 방식에 고정하지 않고, **Score 단위로 추상화**하여 정의한다. Baseline 구현은 명시하되, Auto Research를 통한 대체 방식 실험(Cosine, BM25, Cross-Encoder 등)이 가능하도록 문서 구조를 열어둔다.
- `preferred_topics`와 `video_topic`은 동일한 **Topic Vocabulary**를 기준으로 생성한다.
</aside>

- Raw Data
    - **Youtube Data API**
        - video_id
        - title
        - description
        - channelTitle
        - publishedAt
        - categoryId
        - tags
        - viewCount
        - likeCount
        - commentCount
        - duration
        
        ```json
        {
          "id": "...",
          "snippet": {
            "title": "...",
            "description": "...",
            "channelTitle": "...",
            "publishedAt": "...",
            "categoryId": "24",
            "tags": [...]
          },
          "statistics": {
            "viewCount": "...",
            "likeCount": "...",
            "commentCount": "..."
          },
          "contentDetails": {
            "duration": "PT13M27S"
          }
        }
        ```
        
    - **Persona**
        - Text Columns
            - 이 컬럼들에서 **어떤 Feature를 만들어야 CTR이 올라갈지**를 Agent가 자동으로 탐색하는 것이 Auto Research의 핵심 역할이 될 수 있음
        
        | Raw 컬럼 | 내용 | 타입 | Feature Engineering |
        | --- | --- | --- | --- |
        | uuid |  | ID | `user_id`로 매핑 |
        | professional_persona | 직업 | Text | 직업 관련 키워드 추출, 임베딩 |
        | sports_persona | 스포츠 | Text | 스포츠 선호도 추출 |
        | arts_persona | 예술 | Text | 예술 관심도 추출 |
        | travel_persona | 여행 | Text | 여행 관심도 추출 |
        | culinary_persona | 음식 | Text | 음식 관심도 추출 |
        | family_persona | 가족 | Text | 가족 중심 성향 추출 |
        | persona | 전체 페르소나 | Text | 전체 Persona 임베딩, 키워드 추출 |
        | cultural_background | 문화적 배경 | Text | * |
        | skills_and_expertise | 기술/전문성 | Text | 기술 스택/전문성 추출 |
        | skills_and_expertise_list | 기술 개수
        기술 카테고리 | List |  |
        | hobbies_and_interests |  | Text | 관심사 키워드 추출 |
        | hobbies_and_interests_list | 선호 주제 생성 | List |  |
        | sex | 성별 | Category |  |
        | age | 나이 | Numeric | `age_group` 생성 |
        | occupation | 직업 | Category |  |
        | district | 지역 | Category | 지역 그룹화 |
        | province | 시·도 | Category |  |
        | country | 국가 | Category |  |
    - **Event log**
        
        event_id, event_timestamp, user_id, video_id, clicked, watch_time_sec, liked, search_keyword, source, rank, exposure_type
        

### 📁 Video Feature

| Feature | Type | Feature Store | 사용되는 원본 컬럼 및 생성 방법 |
| --- | --- | --- | --- |
| `video_id` | ID | Offline | YouTube 영상 ID (Entity Key) |
| `category_id` | Category | Offline | `categoryId`를 카테고리명(문자)으로 매핑하여 저장 |
| `duration_sec` | Numeric | Offline | `duration` → 초 단위 변환 |
| `view_count` | Numeric | Offline | `viewCount` 원본 사용 |
| `like_ratio` | Float | Offline | `likeCount` / `viewCount` |
| `comment_ratio` | Float | Offline | `commentCount` / `viewCount` |
| `days_since_upload` | Numeric | Offline | 학습 기준일(또는 노출 시점) - `publishedAt` |

### 📁 User Feature

| Feature | Type | Feature Store | 사용되는 원본 컬럼 및 생성 방법 |
| --- | --- | --- | --- |
| `user_id` | ID | Offline | 사용자 ID (Entity Key) |
| `age_group` | Category | Offline | `age` 그룹화 |
| `occupation` | Category | Offline | `occupation` 원본 사용 |
| `historical_category_affinity` | Category | Online | Label Timestamp 이전 Event Log에서 가장 자주 클릭한 Category |
| `recent_click_count_7d` | Numeric | Online | Label Timestamp 이전 최근 7일간 클릭 수 |
| `recent_watch_time_7d` | Numeric | Online | Label Timestamp 이전 최근 7일간 총 시청 시간 |
| `recent_like_count_7d` | Numeric | Online | Label Timestamp 이전 최근 7일간 좋아요 수 |

> **Cold-start Policy** : `historical_category_affinity`는 유저의 과거 클릭 이력(Event Log)을 기반으로 생성되므로, 아직 클릭 이력이 없는 신규 유저(또는 Label Timestamp 이전 클릭 이력이 없는 경우)는 이 값이 존재하지 않는다. 이 경우 "unknown"으로 채운다. Interaction Feature의 `category_match`는 `historical_category_affinity`가 "unknown"이면 무조건 `0`으로 강제한다 (비교 불가 상태와 실제 불일치 상태를 혼동하지 않기 위함).
> 

### 📊 중간 Artifact

모델 입력 Feature가 아닌  Similarity 계산을 위한 중간 산출물

| Artifact | Type | 생성 방법 | 사용 목적 |
| --- | --- | --- | --- |
| `preferred_topics` | List[str] | `sports_persona`, `arts_persona`, `travel_persona`, `culinary_persona`, `family_persona`, `hobbies_and_interests` → Topic Vocabulary 기반 관심 주제 추출 | `topic_similarity` 계산 |
| `video_topic` | List[str] | `title`, `description`, `tags` → Topic Vocabulary 기반 영상 주제 추출 | `topic_similarity` 계산 |
| `user_text` | Text | `persona`, `professional_persona`, `hobbies_and_interests`, `career_goals_and_ambitions` 결합 | 사용자 의미 표현 |
| `video_text` | Text | `title`, `description`, `tags`, `channelTitle` 결합 | 영상 의미 표현 |
| `user_embedding` | Vector | `user_text` → Sentence Transformer 임베딩 | Similarity 계산 |
| `video_embedding` | Vector | `video_text` → Sentence Transformer 임베딩 | Similarity 계산 |

### Topic Vocabulary (예시)

```json
[
  "music", "sports", "gaming", "travel", "food",
  "education", "technology", "beauty", "news",
  "entertainment", "family", "finance",
  "health", "movie", "fashion"
]
```

### 📁 Event Log - Interaction Feature

**⚠️ 주의**

- Event Log 자체는 **최종 모델 입력 Feature 자체가 아님**
    - Training Dataset을 만들기 위한 역할
    - **언제, 누가, 어떤 영상에, 클릭했는지**를 알려주는 join key + label 소스
- Interaction Feature는 User와 Video Feature를 Joing할 때 생성됨
    - 생성 시점 : Training Dataset 생성 시, Serving 시 추론 직전
    - Interaction Feature는 **조회된 결과 두 개(**User/Video Feature)**를 갖고 그 자리에서(on-the-fly) 계산**되는 것이지, 별도 테이블에 미리 저장해두는 게 아님

| Feature | Type | Feature Store | 생성 방법 |
| --- | --- | --- | --- |
| `category_match` | Binary | N/A (Derived) | `historical_category_affinity == category_id`이면 1, 아니면 0 |
| `topic_similarity` | Float | N/A (Derived) | **Topic Similarity Score.** User `preferred_topics`와 Video `video_topic` 간 주제 유사도를 나타내는 추상화된 지표. Baseline 구현: Jaccard Similarity. (Future Work: Auto Research를 통해 Cosine Similarity, BM25, Cross-Encoder 등 대체 산출 방식을 실험 가능하도록 구현체 교체를 열어둔다) |
| `user_video_embedding_similarity` | Float | N/A (Derived) | `user_embedding`과 `video_embedding`의 Cosine Similarity 계산 |

> **Note:** `topic_similarity`는 다른 두 Interaction Feature와 달리 계산 방식을 특정 알고리즘에 고정하지 않는다. 이는 본 프로젝트의 Auto Research 컨셉(어떤 Feature/방법이 CTR 개선에 유효한지 자동 탐색)과 맞물려, 이후 similarity 산출 방식이 바뀌더라도 스펙 문서 자체는 수정할 필요가 없도록 하기 위함이다.
> 

## 📌 Feature Store Contract

각 Feature 그룹의 **저장 위치, 갱신 주기, 책임 주체**를 명시한다.

| 구분 | 저장 내용 | 갱신 주기 | 생성/갱신 주체 |
| --- | --- | --- | --- |
| **Offline** | Video Feature 전체, User Static Feature(`age_group`, `occupation` 등) | Batch — 1일 1회 | Batch Pipeline |
| **Online** | User Behavior Feature(`historical_category_affinity`, `recent_click_count_7d` 등) | Event-driven — 이벤트 발생 시 즉시 갱신 | Streaming Pipeline (Event Log 수신 시) |
| Persona Derived Artifact | `preferred_topics`, `user_embedding` 등 Persona 파생 값 | 사용자 생성 시 1회 | 초기 적재 배치 |
| **Training Snapshot** | Label Timestamp 이전 이벤트만 반영된 User Feature 스냅샷 | Training Dataset 생성 시점마다 | 학습 파이프라인 |
| **Derived (Interaction)** | `category_match`, `topic_similarity`, `user_video_embedding_similarity` | 저장하지 않음 — on-the-fly 계산 | Training Dataset 생성 로직 / Serving 로직 (동일 코드 공유) |

**현재 상태(7.1 기준) vs 목표**: 현재 MVP 단계에서는 위 Feature들을 CSV(`data/raw`, `data/processed`)로 관리하며, Offline/Online 구분은 논리적으로만 존재한다. 목표 아키텍처는 Offline은 Feast 기반 Feature Store(BigQuery/GCS 연동), Online은 Redis/Valkey이며, 이 마이그레이션은 별도 작업으로 진행한다.

## 📌 Feast 매핑 대응표

본 섹션은 위 Feature Engineering / Feature Store Contract에서 정의한 Feature가 Feast FeatureView로 어떻게 매핑되는지를 명시한다. Feast 구현 스키마가 변경될 경우 본 표를 함께 갱신한다.

### User Feature 매핑

| 스펙 Feature | Feast FeatureView | 집계 방식 | 상태 |
| --- | --- | --- | --- |
| `age_group` | `user_features` | Persona 원본 파생 | 미구현 |
| `occupation` | `user_features` | Persona 원본 사용 | 미구현 |
| `historical_category_affinity` | `user_features` | Label Timestamp 이전 Event Log 집계, cold-start 시 `"unknown"` | 미구현 |
| `recent_click_count_7d` | `user_features` | 7일 sliding window 집계 (누적 카운트 아님) | 미구현 |
| `recent_watch_time_7d` | `user_features` | 7일 sliding window 합계 (평균 아님) | 미구현 |
| `recent_like_count_7d` | `user_features` | 7일 sliding window 집계 (누적 카운트 아님) | 미구현 |

> User Behavior Feature(`recent_*_7d`, `historical_category_affinity`)는 7일 윈도우 기준 recency 신호를 반영해야 하므로, 단순 누적 카운터(increment)가 아닌 sliding window 집계 로직으로 구현한다. Feast의 `ttl`은 조회 값의 유효기간을 의미할 뿐 집계 윈도우가 아니므로, 윈도우 처리는 소스 데이터(Event Log → Online Feature) 생성 단계에서 이루어져야 한다.

### Video Feature 매핑

| 스펙 Feature | Feast FeatureView | 생성 방법 | 상태 |
| --- | --- | --- | --- |
| `category_id` | `video_features` | YouTube API `categoryId`를 카테고리명으로 매핑 | 미구현 |
| `duration_sec` | `video_features` | `duration` → 초 단위 변환 | 미구현 |
| `view_count` | `video_features` | `viewCount` 원본 | 미구현 |
| `like_ratio` | `video_features` | `likeCount` / `viewCount` | 미구현 |
| `comment_ratio` | `video_features` | `commentCount` / `viewCount` | 미구현 |
| `days_since_upload` | `video_features` | 노출 시점 - `publishedAt` | 미구현 |

> `dislike_count`는 본 스펙의 Raw Data 정의(YouTube Data API)에 포함되지 않는 필드다. YouTube Data API는 2021년 이후 공개 dislike 수를 제공하지 않으므로, 원본 데이터 소스 확보 가능 여부를 확인한 뒤 스키마 포함 여부를 결정한다.

### Interaction Feature 매핑

| 스펙 Feature | Feast 구현 방식 | 상태 |
| --- | --- | --- |
| `category_match` | On-Demand Feature View (저장 없음) | 미구현 |
| `topic_similarity` | On-Demand Feature View (저장 없음) | 미구현 |
| `user_video_embedding_similarity` | On-Demand Feature View (저장 없음) | 미구현 |

> Interaction Feature는 `user_id × video_id` cross product 특성상 배치 사전 계산 대상이 아니며, 일반 FeatureView(BigQuery 소스 기반 배치 적재)로 구현하지 않는다. User Feature와 Video Feature 조회 결과를 입력으로 받아 즉시 계산하는 **Feast On-Demand Feature View(ODFV)**로 구현하며, Training Dataset 생성 로직과 Serving 로직이 동일한 계산 함수를 공유해야 한다 (Training-Serving Skew 방지 원칙, Feature Engineering 섹션 참고).

### Cold-start Fallback 처리 위치

`historical_category_affinity`가 `"unknown"`인 경우의 fallback 처리 위치(Feast FeatureView 기본값 vs Serving 코드)는 아직 결정되지 않았다. 결정되는 대로 본 섹션에 반영한다.

## 📌 Training Dataset

```json
Event Log 한 행   (user_id=u1, video_id=v10, timestamp=T, clicked=1)
        │
        ├─→  User Feature Store에서
        │    Label Timestamp(T) 이전 기준으로 생성된
        │    User Feature 조회 (user_id=u1)
        │
        ├─→  Video Feature Store에서 v10의 피처 조회
        │
        └─→  두 결과를 join
        
			                       ↓

                Interaction Feature 계산
                
			                       ↓
			                       
    [user_id | video_id | User Feature | Video Feature | Interaction Feature | clicked]
```

- **최종 Model Input Columns**
    
    > `user_id`와 `video_id`는 Training Dataset의 row key로 저장되나, 모델의 학습/추론 입력에는 포함되지 않는다 (모델은 user와 video의 feature만 사용).
    
    | Column | Type | 설명 |
    | --- | --- | --- |
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
    | `category_match` | Binary | 사용자 선호 카테고리와 영상 카테고리 일치 여부 |
    | `topic_similarity` | Float | 사용자 관심 주제와 영상 주제의 Similarity Score |
    | `user_video_embedding_similarity` | Float | 사용자-영상 임베딩 Cosine Similarity |
    | `clicked` | Binary | Label |

## 📌 Model / Evaluation

| Model | 목적 |
| --- | --- |
| Logistic Regression | Baseline |
| LightGBM | Main Model |
| XGBoost | 비교 실험 |
| CatBoost | Future |
| DeepFM | Future |
- CTR
    - ROC-AUC
    - Log Loss
    - PR-AUC
- 추천
    
    > Training Dataset의 한 행은 `(user_id, video_id, clicked)` pointwise 구조이므로,
    > 
    > 
    > Recall@10 / NDCG@10을 계산하려면 별도의 **랭킹 변환 단계**가 필요함
    > 
    > 1. 평가 시점에 한 유저에 대해 후보 영상 집합(예: 그 시점 Trending 상위 200개)에 대한 `click_probability`를 모두 계산
    > 2. 확률 기준으로 재랭킹
    > 3. 실제 클릭한 영상이 Top-10 안에 포함되는지(Recall@10) / 어느 순위에 위치하는지(NDCG@10)를 평가
    > 
    > 즉 pointwise 예측값을 유저 단위로 그룹핑한 뒤 랭킹 지표로 변환하는 로직이 별도로 필요
    > 

## 📌 Training / Inference Flow

- Training Flow
    
    ```json
    Raw -> Feature Engineering -> Feature Store -> Training Dataset
    -> Train -> Evaluate -> MLflow -> Registry
    ```
    
- Inference Flow
    
    ```json
    user_id
        ↓
    Online Feature Store  (User Feature 조회)
        ↓
    Offline Feature Store  (Trending 후보 영상 N개, 예: 200개의 Video Feature 조회)
        ↓
    후보 영상 N개 × User Feature → Interaction Feature 계산
        ↓
    CTR Model → 후보 영상별 click_probability
        ↓
    Post-processing: Top-N 추출 (+ Exploration 아이템 일부 혼합)
    ```
    
    > 
    > 
    > 
    > Inference 시점에는 영상 1개가 아니라 **Candidate Pool(Trending Top-N) 전체**에 대해 유저 1명 기준으로 `click_probability`를 병렬 계산한 뒤, 그 결과를 정렬해서 Top-N을 잘라낸다. 후보군을 선정하는 별도 로직(candidate generation 모델)은 본 프로젝트 범위에 포함하지 않으며, Candidate Pool은 수집된 Trending 영상 중 최근 기준 Top-N(예: 200개)으로 고정한다.
    >