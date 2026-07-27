# 계획: DuckDB ↔ offline store 피처 계약 정렬 (#357, Phase 1)

- Issue: #357 (부모 [EPIC] #299 학습 데이터셋 Feast PIT 조회 전환)
- 선행: #356(Phase 0) 완료 — spine 빌드 + 백필로 **정상 11일**(07-07, 07-12~21) 확보
- 다음: #358(FeatureService 조회 전환)

## 목표

학습 경로(`build_training_dataset.py`의 DuckDB 재계산)와 offline store 정의
(`feature_store_build`의 BigQuery SQL)가 **같은 피처를 다르게 계산하는 지점**을
실측 diff로 찾아내고, **offline store 정의를 정본으로 채택**한다(서빙이 읽는 값 =
학습이 배우는 값). 그리고 조회 전환(#358)이 딛고 설 설계 결정 3가지를 확정한다.

이 단계는 값 대조·설계 결정이 산출물이고, 실제 조회 코드 교체는 #358이다.

## 왜 지금 diff가 필요한가

두 경로가 "같은 규칙"이라도 구현이 달라 값이 어긋날 수 있다. 전환 후 조용히
달라지면 학습 지표가 원인 불명으로 흔들린다. `topic_similarity` BQ 조회(#242,
`build_training_dataset.py`의 as-of join)에서 BQ에 없는 유저가 COALESCE로 조용히
0이 된 전례가 있다.

## 작업

### 1. 값 diff 하네스 + 6개 지점 검증

같은 (날짜, 유저/영상)에 대해 두 경로로 피처를 뽑아 컬럼별로 비교하는 재현 가능한
스크립트(`scripts/`)를 만든다. 정상 11일로 돌린다. bbungjun 분석(#299 코멘트)의
체크리스트를 그대로 검증:

- [ ] **timezone 경계** — KST 23:30(UTC 14:30) 이벤트가 두 구현에서 같은 날짜 버킷에 드는가
- [ ] **윈도우 포함/미포함** — `[T-7d, T)` 경계 정각 이벤트의 off-by-one
- [ ] **집계 정의** — `COALESCE(watch_time,0)`, 카테고리 dedup(`video_latest` QUALIFY 순서) 등이 affinity에 주는 차이
- [ ] **무활동 유저 기본값** — offline은 0-채움 행 제공 vs DuckDB의 drop/NULL
- [ ] **스냅샷 결손 시 동작** — ttl 부재 stale fallback (#356에서 실증)
- [ ] **차이 발견 시 offline 정의를 정본 채택**하고 원인 기록

산출: 컬럼별 일치/불일치 표 + 불일치 원인.

### 2. 설계 결정 — 파생 2종 (ODFV vs 후처리)

`preferred_category_match`, `historical_category_match`는 raw 피처가 아니라 조회
후 (user 피처 vs 영상 category) 비교로 나오는 파생값이다. Feast On-Demand
FeatureView(ODFV)로 정의할지 vs 조회 후 파이썬/ SQL 후처리로 계산할지 결정한다.
판단 기준: 학습·서빙 공통 경로로 두어 skew를 없앨 수 있는가, 유지보수 비용.

### 3. 설계 결정 — cross-entity 조인 (topic_similarity)

`topic_similarity`는 (user, **영상의 category_id**) 키다. 영상 category는 이벤트
시점 video 스냅샷에서 나오므로 닭-달걀이다. **staged 조회**(1차 video PIT로
category 확정 → 2차 similarity 조인)를 설계로 확정한다. Feast
`get_historical_features`로 한 번에 될지, 2단계로 나눌지 포함.

### 4. 설계 결정 — ttl 정책

전 FeatureView `ttl` 부재 → 결손일 stale fallback(#356 실증). ttl을 넣어 결손일을
`null`로 만들지 vs 백필로만 메울지 결정한다. **서빙 online 조회에도 영향**하므로
학습·서빙 공통으로 정한다. #356의 폐루프 구멍(#365) 현실을 반영.

### 5. 구/신 데이터셋 diff → ROC-AUC 영향 정량화

1의 diff로 재구성한 신 데이터셋과 기존 DuckDB 데이터셋으로 각각 학습해
Val/Test ROC-AUC 차이를 잰다. 정상 11일 표본 한계를 명시한다.

## 검증

- diff 하네스는 재현 가능한 스크립트로 남긴다(값을 말로만 적지 않는다).
- 6개 지점 판정 + 3개 설계 결정 + 지표 영향 수치를 `docs/specs/`에 문서화.
- `ruff` / 관련 `pytest`.

## 산출물

- `docs/specs/2026-XX-XX-feature-contract-alignment.md` — diff 결과 + 정본 채택 + 3개 설계 결정
- diff 하네스 스크립트

## 비범위

- 실제 조회 경로 교체(`--assembly-source feast`) = #358
- FeatureService 정의 = #358
- 폐루프 데이터 신뢰성(#365) — Phase 1은 정상 11일로 진행, 데이터 양은 그 제약을 받음

## 리스크

- **표본 제약**: 정상 데이터가 11일뿐(#365 폐루프 구멍). diff·ROC-AUC 정량화의
  통계적 힘이 약할 수 있음 — 결론에 표본 한계를 반드시 병기.
- **stale 오판**: ttl 부재로 결손일에 stale 값이 붙어 diff가 "일치"로 보일 위험
  (#356 함정). diff는 결손 없는 날 기준으로 볼 것.

## 결정 (열린 질문 해소)

- **diff 하네스 = 별도 스크립트** (`scripts/`, `verify_offline_coverage.py`와 동일
  스타일). `build_training_dataset`에 비교 모드를 넣으면 감사용 코드가 운영
  파이프라인에 영구히 얹혀 #359(DuckDB 제거) 때 "지워도 되나?" 하는 짐이 됨.
- **ROC-AUC 정량화는 #357에 포함**하되, 결과 문서 제목·서두에 **"n=11일, 참고용,
  결론 확정 아님"**을 강하게 박음. 별도 이슈로 빼도 얻을 게 없음 — #358 착수 여부는
  ROC-AUC 수치가 아니라 6개 지점 diff 판정에 달려 있음.
