"""리랭킹 서빙 부하테스트용 결정론적 fixture 패키지.

[파이프라인] 수집 → 웨어하우스 적재 → 피처 → 학습 → 일일 추천 → 노출 조립 →
LLM 판정 → action log → 재학습 흐름 중, 리랭킹 서빙 부하측정 전에 BigQuery
offline feature source의 loadtest fixture를 준비하는 구간을 담당한다.

[기능] fixture/provisioning CLI가 소비할 고정 feature 행과 source table에 한정된
BigQuery DML renderer를 제공한다.

[비책임] Feast materialize와 Redis online store 갱신은 feature_repo/ 및 Airflow가,
HTTP 리랭킹 요청은 src/serving/이 담당한다.
"""
