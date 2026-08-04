### 연구 가설
비율 피처가 ROC-AUC를 높인다.

### 변경할 피처 · 모델
- 추가 피처: views_per_day = views / (days + 1)

### 주 지표 이름
roc_auc

### 주 지표 방향
higher_is_better

### 최소 주 지표 개선폭
0.002

### Guardrail 지표 이름
없음

### Guardrail 지표 방향
not_applicable

### 최대 Guardrail 악화폭
없음

### 보조 관측 지표
pr_auc

### 비교 대상
동일 조건 baseline 재학습 (권장)

### 데이터셋 스냅샷
bq://autoresearch/train@2026-07-31

### 랜덤 시드 목록
42, 43, 44

### Split 시드
20260731

### Test 비율
0.2

### Validation 비율
0.2

### 학습 설정 참조
configs/train/lgbm-v1.yaml@abc1234

### 대상 데이터 · 기간
- 데이터셋 / 경로: data/train.csv
- 기간 (KST YYYY-MM-DD ~ YYYY-MM-DD): 2026-07-01 ~ 2026-07-31

### 스냅샷 재사용
허용 (진행하되 실제로 쓴 데이터를 결과에 명시)

### 허용 범위
- [ ] prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다

### 결과 (에이전트가 채웁니다)
- 판정 (지지/기각):
