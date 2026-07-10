# Data Lake

YouTube CTR 학습 파이프라인이 사용하는 Raw Data 카탈로그입니다. 이 문서는
논리 데이터셋 이름과 현재 저장소에 구현된 Parquet 계약·GCS 경로를 함께
정리합니다.

> 기준일: 2026-07-10

## 구현 상태

| 데이터셋 | 현재 상태 | 구현된 생산 경로 |
| --- | --- | --- |
| `data_lake/action_log` | 스키마, 일일 생성기, Airflow DAG, GCS Parquet 적재가 구현되어 있습니다. | `youtube_action_log_daily` DAG가 같은 날짜의 Video 파티션과 User Parquet을 읽어 생성합니다. |
| `data_lake/youtube_trending_kr` | 스키마, API 일일 수집, 과거 데이터 백필, GCS Parquet 적재가 구현되어 있습니다. | `youtube_trending_kr_daily`와 `youtube_backfill_kr` DAG가 생성합니다. |
| `asset/virtual_user/vu_1000` | Virtual User 생성과 로컬 Parquet 쓰기가 구현되어 있습니다. 운영 Action Log DAG는 GCS의 단일 Parquet 파일을 입력으로 가정합니다. | 저장소에는 생성 결과를 GCS 운영 경로로 자동 업로드하는 DAG가 없습니다. 별도 업로드 또는 배포 절차가 필요합니다. |

위 상태는 저장소 코드 기준입니다. 실제 GCS 객체의 존재 여부와 DAG 배포·실행
상태는 이 저장소만으로 확인할 수 없습니다. 또한 위 논리 이름은 Raw Data
카탈로그 이름이며, BigQuery 테이블 등록이 현재 구현되었다는 의미는 아닙니다.

## 저장 형식과 파티션 공통 규칙

- 저장 형식은 Parquet입니다.
- 문서의 `VARCHAR`, `BIGINT`, `BOOLEAN`, `TIMESTAMP WITH TIME ZONE`,
  `VARCHAR[]`는 각각 Parquet/Arrow의 `string`, `int64`, `bool`,
  `timestamp[us, tz=UTC]`, `list<string>`에 대응합니다.
- Action Log와 Video의 `dt=YYYY-MM-DD`는 Hive 스타일 **경로 파티션**입니다.
  `dt`는 Parquet 파일의 물리 스키마에 저장되지 않으며, Hive-aware reader가
  경로에서 파생할 때만 컬럼처럼 조회할 수 있습니다.
- User는 날짜 파티션 없이 단일 `.parquet` 파일로 사용합니다.

## 목차

- [Action Log](#action-log)
- [Video](#video)
- [User](#user)

---

## Action Log

### 식별자와 물리 경로

- 논리 데이터셋: `data_lake/action_log`
- GCS 경로:
  `gs://<YOUTUBE_LAKE_BUCKET>/data_lake/action_log/dt=YYYY-MM-DD/part-0.parquet`
- 격리 로그 경로:
  `gs://<YOUTUBE_LAKE_BUCKET>/data_lake/action_log_quarantine/dt=YYYY-MM-DD/quarantine.jsonl`
- 파티션 기준: `dt`는 Action Log 실행의 `partition_date`입니다. 일일 실행은
  Asia/Seoul 날짜를 사용하며, 적재 전 모든 `event_timestamp`가 해당 KST 날짜에
  속하는지 검증합니다. 파일 안의 타임스탬프는 UTC로 저장됩니다.
- 쓰기 방식: 일일 최종 산출물은 날짜별 `part-0.parquet` 한 개입니다.

### 행 그레인과 중요 제약

- 한 행은 한 번의 `impression`, `click`, `view`, `like` 이벤트입니다.
- `(user_id, video_id)`는 고유 키가 아닙니다. 하나의 노출 흐름은 항상
  `impression` 한 행을 가지며, 클릭된 경우 `click`, `view`, 선택적으로 `like`
  행이 추가됩니다.
- `clicked` 라벨은 Raw Data에 저장하지 않습니다. 학습 데이터 생성 시
  `impression`과 이후 `click`을 조인해 파생합니다.
- `watch_time_sec`는 `view`일 때만 0 이상의 값이고, 다른 이벤트에서는
  `NULL`이어야 합니다.
- 현재 Phase 1 생성기의 `rank`는 항상 `NULL`, `source`는 `historical`입니다.
  스키마는 향후 `online_simulated` source도 허용합니다.
- 같은 노출 흐름의 시각은 `impression < click < view < like` 순입니다.
- 현재 생성기는 최종화하는 배치마다 `event_id`를 `evt_00000000`부터 다시
  부여합니다. 따라서 한 Parquet 산출물 안에서는 고유하지만, 여러 일자의
  파티션을 합쳤을 때 `event_id` 단독의 전역 고유성은 보장되지 않습니다.
  일일 레이크를 통합 조회할 때는 현 구현 기준으로 `(dt, event_id)`를 사용해야
  합니다. 일회성 대규모 생성 스크립트는 병합 시 `event_id` offset을 별도로
  적용할 수 있지만, 일일 DAG에는 전역 offset이 없습니다.

### 스키마

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `event_id` | `VARCHAR` | 이벤트 ID. 현재 구현에서는 생성 배치 또는 날짜 파티션 범위에서 고유합니다. |
| `event_timestamp` | `TIMESTAMP WITH TIME ZONE` | 이벤트 발생 시각. Parquet에는 UTC로 저장됩니다. |
| `user_id` | `VARCHAR` | 사용자 ID |
| `event_type` | `VARCHAR` | 이벤트 종류: `impression`, `click`, `view`, `like` |
| `video_id` | `VARCHAR` | 영상 ID |
| `watch_time_sec` | `BIGINT` | 시청 시간(초). `view` 이벤트에만 값이 있습니다. |
| `rank` | `BIGINT` | 추천/노출 순위. 현재 Phase 1 생성에서는 `NULL`입니다. |
| `source` | `VARCHAR` | 데이터 출처. 현재 Phase 1은 `historical`입니다. |
| `schema_version` | `VARCHAR` | 스키마 버전 |
| `prompt_version` | `VARCHAR` | 프롬프트 버전 |
| `llm_model` | `VARCHAR` | 생성에 사용된 LLM 또는 규칙 기반 생성기 모델명 |
| `generated_at` | `VARCHAR` | 배치 생성 시각을 나타내는 ISO 8601 문자열 |

---

## Video

### 식별자와 물리 경로

- 논리 데이터셋: `data_lake/youtube_trending_kr`
- GCS 경로:
  `gs://<YOUTUBE_LAKE_BUCKET>/data_lake/youtube_trending_kr/dt=YYYY-MM-DD/part-0.parquet`
- 일일 수집 파티션 기준: `collected_at`의 Asia/Seoul 날짜
- 백필 파티션 기준: 정규화된 `video_trending_date.date()`
- 쓰기 방식: 동일 `dt`를 다시 쓰면 해당 날짜의 `part-0.parquet`을
  덮어씁니다. 날짜 간에는 스냅샷을 유지하지만, 같은 날짜 안에서는 마지막
  쓰기 결과가 남습니다.

`dt`는 물리 29개 컬럼에 포함되지 않습니다. 일일 수집과 백필은 같은
`TrendingVideo` 계약과 같은 Parquet 스키마로 적재되지만, 위와 같이 `dt`를
선택하는 기준 필드가 다릅니다.

`video_trending_date`는 스냅샷의 유효 시각입니다. 일일 API 수집에서는
`video_trending_date = collected_at`이지만, 백필에서는 과거
`video_trending_date`를 유지하고 모든 행의 `collected_at`에는 백필 실행 시각을
기록합니다. 따라서 과거 point-in-time feature를 재구성할 때는
`video_trending_date`를 사용하고, `collected_at`은 적재·freshness 메타데이터로
해석해야 합니다.

### 행 그레인과 중요 제약

- 한 행은 특정 수집/트렌딩 시점의 영상 한 개에 대한 스냅샷입니다.
- 같은 `video_id`가 여러 날짜에 반복될 수 있으며, 조회수·좋아요 수·댓글 수와
  채널 통계는 시점별 누적값입니다. 날짜 간 행을 `video_id`만으로 중복 제거하면
  변화량 정보를 잃습니다.
- 현재 파이프라인은 한국(`KR`) 트렌딩만 허용합니다.
- 영상 및 채널 count는 0 이상입니다.
- `channel_published_at`은 원천 결측을 허용하고,
  `channel_subscriber_count`는 구독자 수를 숨긴 채널에서 `NULL`일 수 있습니다.
- `video_duration`은 ISO 8601 duration 문자열이고, `video_tags`는 문자열
  배열입니다.
- API category ID가 category map에 없으면 `video_category`가 빈 문자열일 수
  있습니다. downstream category key는 빈 문자열을 `unknown`으로 정규화해야
  합니다.
- 코드에는 `youtube_trending_kr_v1` 스키마 버전 상수가 있지만, 버전 값은
  현재 Parquet 행의 별도 컬럼으로 저장되지 않습니다.

### 영상 정보 스키마

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `video_id` | `VARCHAR` | 영상 ID |
| `video_published_at` | `TIMESTAMP WITH TIME ZONE` | 영상 게시 시각 |
| `video_trending_date` | `TIMESTAMP WITH TIME ZONE` | 트렌딩 기준 날짜/시각 |
| `video_trending_country` | `VARCHAR` | 트렌딩 국가. 현재 계약은 `KR`입니다. |
| `video_title` | `VARCHAR` | 영상 제목 |
| `video_description` | `VARCHAR` | 영상 설명 |
| `video_default_thumbnail` | `VARCHAR` | 기본 썸네일 URL |
| `video_category` | `VARCHAR` | 영상 카테고리 이름. 매핑되지 않은 API category ID는 빈 문자열일 수 있습니다. |
| `video_tags` | `VARCHAR[]` | 영상 태그 배열 |
| `video_duration` | `VARCHAR` | ISO 8601 형식의 영상 길이 |
| `video_dimension` | `VARCHAR` | `2d`, `3d` 등 영상 차원 |
| `video_definition` | `VARCHAR` | `hd`, `sd` 등 화질 |
| `video_licensed_content` | `BOOLEAN` | 라이선스 콘텐츠 여부 |
| `video_view_count` | `BIGINT` | 조회수 누적값 |
| `video_like_count` | `BIGINT` | 좋아요 수 누적값 |
| `video_comment_count` | `BIGINT` | 댓글 수 누적값 |

### 채널 정보 스키마

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `channel_id` | `VARCHAR` | 채널 ID |
| `channel_title` | `VARCHAR` | 채널명 |
| `channel_description` | `VARCHAR` | 채널 설명 |
| `channel_custom_url` | `VARCHAR` | 채널 커스텀 URL |
| `channel_published_at` | `TIMESTAMP WITH TIME ZONE` | 채널 개설 시각. 일부 원천 행은 `NULL`일 수 있습니다. |
| `channel_country` | `VARCHAR` | 채널 국가 |
| `channel_view_count` | `BIGINT` | 채널 누적 조회수 |
| `channel_subscriber_count` | `BIGINT` | 채널 구독자 수. 숨김 채널은 `NULL`일 수 있습니다. |
| `channel_have_hidden_subscribers` | `BOOLEAN` | 구독자 수 숨김 여부 |
| `channel_video_count` | `BIGINT` | 채널 영상 수 |
| `channel_localized_title` | `VARCHAR` | 현지화 채널명 |
| `channel_localized_description` | `VARCHAR` | 현지화 채널 설명 |

### 수집 정보 스키마

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `collected_at` | `TIMESTAMP WITH TIME ZONE` | 데이터 적재/수집 시각. 백필 행은 백필 실행 시각을 공유하며, Parquet에는 UTC로 저장됩니다. |

---

## User

### 식별자와 물리 경로

- 논리 데이터셋: `asset/virtual_user/vu_1000`
- Action Log 운영 입력 기본 경로:
  `gs://<YOUTUBE_LAKE_BUCKET>/asset/virtual_user/vu_1000.parquet`
- 파티션: 없음. 단일 Parquet 파일입니다.
- 생성기 기본 로컬 출력은
  `asset/virtual_user/virtual_users_20s_100.parquet`이며, 대규모 생성 과정에서는
  `asset/virtual_user/vu_1000.parquet` 이름의 로컬 산출물을 만들었습니다.
  운영 GCS 경로는 Action Log DAG의 입력 계약이고, Virtual User 생성기가 해당
  GCS 경로로 자동 업로드하지는 않습니다.

`vu_1000`은 현재 파일 이름/데이터셋 식별자이며 정확히 1,000행이라는 보장은
없습니다. 저장소의 대규모 생성 리포트에 기록된 같은 이름의 로컬 산출물은
6,983행입니다.

### 행 그레인과 중요 제약

- 한 행은 한 명의 가상 사용자 프로필/페르소나입니다.
- Pydantic 모델의 `virtual_user_id`가 Parquet에서는 `user_id`로 평탄화됩니다.
- `source_persona_json`은 중첩 객체가 아니라 JSON 직렬화된 문자열로 저장됩니다.
- keyword와 category 컬럼은 문자열 배열입니다.
- 현재 생성 계약에서 `sex`는 `male` 또는 `female`, `country` 기본값은 `KR`,
  `locale` 기본값은 `ko-KR`입니다.
- `primary_categories`는 허용된 YouTube 카테고리 1~5개이며,
  `watch_time_band`는 `morning`, `afternoon`, `evening`, `night`, `mixed` 중
  하나입니다.
- `generated_at`은 timestamp 타입이 아니라 ISO 8601 문자열입니다.

### 스키마

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `user_id` | `VARCHAR` | 사용자 ID |
| `source_uuid` | `VARCHAR` | 원천 Persona UUID |
| `source_dataset` | `VARCHAR` | 원천 데이터셋 |
| `source_hash` | `VARCHAR` | 원천 데이터 해시 |
| `age` | `BIGINT` | 나이 |
| `sex` | `VARCHAR` | 성별. 현재 계약은 `male` 또는 `female`입니다. |
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
| `source_persona_json` | `VARCHAR` | JSON 직렬화된 원본 페르소나 |
| `schema_version` | `VARCHAR` | 스키마 버전 |
| `prompt_version` | `VARCHAR` | 프롬프트 버전 |
| `llm_model` | `VARCHAR` | 생성에 사용된 LLM 모델 |
| `generated_at` | `VARCHAR` | 생성 시각을 나타내는 ISO 8601 문자열 |
