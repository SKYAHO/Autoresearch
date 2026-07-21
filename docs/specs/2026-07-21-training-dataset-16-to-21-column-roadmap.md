# training_dataset 16→21컬럼 전환 로드맵 (#175 결정안)

> Status: Draft — 이슈 #175 완료 조건(SSOT 명확화 + 전환 로드맵 합의) 충족을 위한 결정안.
>
> **갱신(#206)**: 아래 Phase 3의 "Sentence Transformer"는 이 문서 작성 시점의 초기
> 가정이었다. 실제로는 self-host(Sentence Transformer) 대신 Vertex AI
> `gemini-embedding-001`(output_dimensionality=768)을 파이썬 배치 job에서 호출하는
> 방식으로 확정·구현됐다 — torch 의존성 없이 `google-cloud-aiplatform` SDK 하나로
> 처리 가능하고, 기존 `Dockerfile.app` 계열에 그대로 얹을 수 있어서다. 이슈 #206
> 코멘트 참고.

## 배경

`#172`(events BigQuery 전환) 작업 중 `docs/guides/training-dataset.md` /
`docs/guides/data-warehouse.md`가 설계한 목표 스키마(21 Model Input 컬럼,
Feast `get_historical_features()` 경유)와 실제 구현
(`src/pipeline/build_training_dataset.py`, `src/features/assembly.py`, 16
컬럼)이 어긋나 있는 것이 확인되었다.

코드 조사 결과, 목표 설계는 이미 상당히 구체화되어 있다.

- `feature_repo/feature_definitions.py`: `UserStaticView`/`UserDynamicView`/
  `VideoFeatureView`/`UserCategorySimilarityView` 4개 FeatureView가 21컬럼
  스키마 기준으로 이미 정의되어 있다.
- `docs/guides/data-warehouse.md`: `user_static_feature`,
  `user_dynamic_feature`, `video_feature` 3개 중간 테이블의 BigQuery DDL/SQL이
  컬럼 단위로 완성되어 있다 (cold-start default, point-in-time 규칙 포함).
- 원천 데이터: `data_lake_youtube_trending_kr`에 `channel_subscriber_count`,
  `channel_view_count`, `channel_video_count`가 이미 존재한다
  (`autoresearch/youtube_collection/schema.py`).

반면 실제로 비어 있는 부분은:

- 위 4개 중간 테이블을 만드는 배치 ETL 코드가 레포에 없다.
- `get_historical_features()` 호출이 0건 — Feast는 온라인 서빙(Redis
  materialize)에만 쓰이고 오프라인 학습 데이터 조립에는 전혀 관여하지 않는다.
- `preferred_category`는 키워드 15개 하드코딩 매핑(`derive_preferred_category`,
  `src/features/assembly.py`)으로 대체되어 있다 — "TEMPORARY MOCK" 명시.
- `topic_similarity` 계산용 임베딩(`src/features/embeddings.py`의
  `embed_text`)은 해시 시드 기반 pseudo-embedding이다 — "PLACEHOLDER" 명시.
- `docs/guides/ctr-model-specification.md`의 "최종 Model Input Columns" 표만
  16컬럼 기준으로 남아 있어 다른 두 문서와 어긋난다.

## 결정 1 — SSOT

`docs/guides/training-dataset.md` + `docs/guides/data-warehouse.md`를 21컬럼
목표 설계의 SSOT로 채택한다. 두 문서는 이미 서로 정합적이며 컬럼 단위 SQL까지
합의되어 있다.

`docs/guides/ctr-model-specification.md`의 "📌 Training Dataset → 최종 Model
Input Columns" 표(16컬럼)는 **현재 구현 스냅샷**이라는 것을 문서 상단에 명시하고,
목표는 `training-dataset.md`를 참조하도록 갱신한다. 21컬럼 전환이 완료되면 이
표를 21컬럼 기준으로 갱신하거나 제거하고 `training-dataset.md`로 단일화한다.

## 결정 2 — 전환 로드맵 (Phase 분리)

한 번에 Feast 전체 경로로 전환하는 대신, 리스크가 다른 두 축(① 컬럼 확장 로직,
② 서빙 경로 자체를 Feast로 바꾸는 것)을 분리한다.

| Phase | 내용 | 의존성 | 후속 이슈 |
| --- | --- | --- | --- |
| **Phase 1** | 16→21컬럼 확장 — 기존 DuckDB 기반 파이프라인(`assembly.py`)에 `watch_time_band`, `recent_view_count_7d`, `total_event_count_7d`, `channel_subscriber_count`, `channel_view_count`, `channel_video_count` 6개 컬럼 추가. `training-dataset.md`가 이미 명시한 "MVP용 BigQuery Join Fallback" 패턴을 그대로 따름 — Feast 미경유, 신규 인프라 불필요 | 없음 (레포 내 완결) | 이번 세션에서 구현 |
| **Phase 2** | `preferred_category`를 mock 키워드 매핑에서 실제 로직으로 전환 | User Feature Specification (persona 생성 단계에서 LLM이 카테고리 1~3개 직접 선택) — virtual_users 파이프라인 담당 영역, 별도 설계 필요 | 별도 이슈 (구현 보류) |
| **Phase 3** | `topic_similarity` pseudo-embedding → 실제 Sentence Transformer 전환 | 신규 의존성(`sentence-transformers`) 추가, 모델 다운로드/캐싱 전략, CI 네트워크 접근 여부 결정 필요 | 별도 이슈 (구현 보류) |
| **Phase 4** | `user_static_feature`/`user_dynamic_feature`/`video_feature`/`user_category_similarity` 4개 BigQuery 테이블 실제 생성 + `get_historical_features()` 경유로 학습 파이프라인 전환 | 실제 BigQuery 배포, Feast apply, Feast 팀(Feast Features 도메인) 작업과 순서 조율 | 별도 이슈 (구현 보류) |

Phase 1을 먼저 진행하는 이유: `data-warehouse.md`가 이미 "(현재 16컬럼 파이프라인
전용) watch_time_sec/liked 파생 규칙" 절에서 현재 DuckDB 어댑터가 Phase 4(Feast
전환) 전까지의 임시 경로임을 명시하고 있다. Phase 1은 이 임시 경로 위에서 안전하게
확장 가능한 범위이고, Phase 2~4는 각각 별도 팀/의존성 결정이 필요해 한 PR에
묶기에는 리스크가 다르다.

## 결정 3 — Feast 작업(Feast Features 도메인)과의 역할 분담 제안

- Phase 1은 Feast를 전혀 경유하지 않으므로 Feast 담당자 조율이 필요 없다.
- Phase 4(4개 BigQuery 테이블 실체화 + Feast apply)는 `feature_repo/`
  FeatureView 정의가 이미 21컬럼 기준으로 맞춰져 있어 스키마 자체는 재정의할
  필요가 없다. 다만 실제 BigQuery 테이블 생성/적재 배치(ETL)를 Model
  Training과 Feast Features 중 어느 쪽이 만들지, 그리고 `channel_*` 3종 통계·
  `watch_time_band`가 `VideoFeatureView`/`UserStaticView`에 이미 등록되어 있는
  스키마와 실제 소스 데이터가 어긋나지 않는지 최종 검증을 누가 맡을지는 이슈
  코멘트에서 별도로 확인이 필요하다 — 이 문서에서는 제안만 남기고 확정하지
  않는다.
- 제안: Phase 4 이슈 발행 시 Model Training과 Feast Features 담당자를
  co-assignee로 지정하고, 4개 테이블의 ETL 코드는 Model Training이 작성하되
  Feast apply/online sync는 기존 Feast 작업 패턴(Redis materialize)을 따라
  Feast Features 담당이 검증한다.

## 결정 4 — pseudo-embedding → 실 임베딩 전환 시점

Phase 3으로 별도 분리한다 (위 표 참고). `sentence-transformers` 같은 무거운
의존성 추가와 모델 다운로드가 필요해 CI/런타임 환경에 대한 별도 결정
(모델 캐싱 전략, 오프라인 사전 계산 방식 채택 여부)이 선행되어야 하므로 이번
세션에서는 설계만 남기고 구현하지 않는다.

## 후속 이슈

이 문서 확정 후 아래 이슈를 발행한다 (Phase 1은 이번 세션에서 구현까지 진행):

1. **(Phase 1, 구현)** training_dataset 16→21컬럼 확장 — DuckDB fallback 경로
2. **(Phase 2)** `preferred_category` mock 제거 — User Feature Specification 기반 실제 로직 전환
3. **(Phase 3)** `topic_similarity` 실제 임베딩 전환 (Sentence Transformer)
4. **(Phase 4)** 4개 BigQuery 중간 테이블 실체화 + Feast `get_historical_features()` 경유 전환
