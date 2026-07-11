# Data Lake

Raw Data Catalog

---

## Table of Contents

- [Action Log](#action-log)
- [Video](#video)
- [User](#user)

---

## Action Log

**Table**

- `data_lake/action_log`

### Schema

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `event_id` | `VARCHAR` | 이벤트 고유 ID |
| `event_timestamp` | `TIMESTAMP WITH TIME ZONE` | 이벤트 발생 시각 |
| `user_id` | `VARCHAR` | 사용자 ID |
| `event_type` | `VARCHAR` | 이벤트 종류 |
| `video_id` | `VARCHAR` | 영상 ID |
| `watch_time_sec` | `BIGINT` | 시청 시간, 초 단위 |
| `rank` | `BIGINT` | 추천/노출 순위 또는 이벤트 순위 |
| `source` | `VARCHAR` | 데이터 출처 |
| `schema_version` | `VARCHAR` | 스키마 버전 |
| `prompt_version` | `VARCHAR` | 프롬프트 버전 |
| `llm_model` | `VARCHAR` | 생성에 사용된 LLM 모델 |
| `generated_at` | `VARCHAR` | 생성 시각 문자열 |

---

## Video

### Table

- `data_lake/youtube_trending_kr`

### Video Information

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `video_id` | `VARCHAR` | 영상 ID |
| `video_published_at` | `TIMESTAMP WITH TIME ZONE` | 영상 게시 시각 |
| `video_trending_date` | `TIMESTAMP WITH TIME ZONE` | 트렌딩 기준 날짜 |
| `video_trending_country` | `VARCHAR` | 트렌딩 국가 |
| `video_title` | `VARCHAR` | 영상 제목 |
| `video_description` | `VARCHAR` | 영상 설명 |
| `video_default_thumbnail` | `VARCHAR` | 기본 썸네일 URL |
| `video_category` | `VARCHAR` | 영상 카테고리 |
| `video_tags` | `VARCHAR[]` | 영상 태그 배열 |
| `video_duration` | `VARCHAR` | 영상 길이 |
| `video_dimension` | `VARCHAR` | 2D/3D 등 영상 차원 |
| `video_definition` | `VARCHAR` | HD/SD 등 화질 |
| `video_licensed_content` | `BOOLEAN` | 라이선스 콘텐츠 여부 |
| `video_view_count` | `BIGINT` | 조회수 |
| `video_like_count` | `BIGINT` | 좋아요 수 |
| `video_comment_count` | `BIGINT` | 댓글 수 |

### Channel Information

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `channel_id` | `VARCHAR` | 채널 ID |
| `channel_title` | `VARCHAR` | 채널명 |
| `channel_description` | `VARCHAR` | 채널 설명 |
| `channel_custom_url` | `VARCHAR` | 채널 커스텀 URL |
| `channel_published_at` | `TIMESTAMP WITH TIME ZONE` | 채널 개설 시각 |
| `channel_country` | `VARCHAR` | 채널 국가 |
| `channel_view_count` | `BIGINT` | 채널 누적 조회수 |
| `channel_subscriber_count` | `BIGINT` | 채널 구독자 수 |
| `channel_have_hidden_subscribers` | `BOOLEAN` | 구독자 수 숨김 여부 |
| `channel_video_count` | `BIGINT` | 채널 영상 수 |
| `channel_localized_title` | `VARCHAR` | 현지화 채널명 |
| `channel_localized_description` | `VARCHAR` | 현지화 채널 설명 |

### Collection Information

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `collected_at` | `TIMESTAMP WITH TIME ZONE` | 데이터 수집 시각 |

---

## User

### Table

- `asset/virtual_user/vu_1000`

### Schema

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `user_id` | `VARCHAR` | 사용자 ID |
| `source_uuid` | `VARCHAR` | 원천 UUID |
| `source_dataset` | `VARCHAR` | 원천 데이터셋 |
| `source_hash` | `VARCHAR` | 원천 해시 |
| `age` | `BIGINT` | 나이 |
| `sex` | `VARCHAR` | 성별 |
| `age_bucket` | `VARCHAR` | 연령대 |
| `marital_status` | `VARCHAR` | 혼인 상태 |
| `military_status` | `VARCHAR` | 병역 상태 |
| `family_type` | `VARCHAR` | 가족 형태 |
| `housing_type` | `VARCHAR` | 주거 형태 |
| `education_level` | `VARCHAR` | 교육 수준 |
| `bachelors_field` | `VARCHAR` | 전공 분야 |
| `occupation` | `VARCHAR` | 직업 |
| `province` | `VARCHAR` | 시/도 |
| `district` | `VARCHAR` | 시/군/구 |
| `country` | `VARCHAR` | 국가 |
| `locale` | `VARCHAR` | 로케일 |
| `persona_summary` | `VARCHAR` | 페르소나 요약 |
| `hobby_keywords` | `VARCHAR[]` | 취미 키워드 |
| `interest_keywords` | `VARCHAR[]` | 관심사 키워드 |
| `lifestyle_keywords` | `VARCHAR[]` | 라이프스타일 키워드 |
| `food_keywords` | `VARCHAR[]` | 음식 관련 키워드 |
| `travel_keywords` | `VARCHAR[]` | 여행 관련 키워드 |
| `career_keywords` | `VARCHAR[]` | 커리어 관련 키워드 |
| `family_context_keywords` | `VARCHAR[]` | 가족 맥락 키워드 |
| `primary_categories` | `VARCHAR[]` | 주요 관심 카테고리 |
| `watch_time_band` | `VARCHAR` | 시청 시간대/시청 성향 구간 |
| `source_persona_json` | `VARCHAR` | 원본 페르소나 JSON |
| `schema_version` | `VARCHAR` | 스키마 버전 |
| `prompt_version` | `VARCHAR` | 프롬프트 버전 |
| `llm_model` | `VARCHAR` | 생성에 사용된 LLM 모델 |
| `generated_at` | `VARCHAR` | 생성 시각 문자열 |