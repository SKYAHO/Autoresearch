# 액션 로그 Hourly 계약 구현 계획

1. EventGenerationRequest에 명시적인 시작 시각을 추가합니다.
2. 날짜 파티션 helper를 선택적 hour 파티션으로 확장합니다.
3. 한 시간 구간 검증과 결정적 Persona 선택을 추가합니다.
4. Single, Shard, Merge 경로에 동일한 interval 계약을 전달합니다.
5. Airflow 비종속 CLI를 추가합니다.
6. Daily 회귀 테스트와 Hourly 파티션·시간·선택 테스트를 실행합니다.
7. 후속 Airflow PR에서 data interval과 KPO 인자를 연결합니다.
