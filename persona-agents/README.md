# Persona Agents — Nemotron-Personas(ko_KR) 기반 가상 유저 행동 시뮬레이터

NVIDIA [Nemotron-Personas (ko_KR)](https://catalog.ngc.nvidia.com/orgs/nvidia/nemotron-personas/resources/nemotron-personas-dataset-ko_kr)
의 한국어 합성 페르소나를 LLM 에이전트(가상 유저)로 구동해, **유튜브 영상 목록을 보고 클릭/시청하는 행동 데이터**를 생성한다.
이 상호작용 데이터로 추천 모델을 학습하는 것이 최종 목표.

## 전체 목표 파이프라인

```
① 영상 카탈로그           BigQuery <proj>.youtube.trending (기존 YouTube 파이프라인)
        │
② 행동 시뮬레이션         simulate_behavior.py  ← 지금 단계
        │                 페르소나 + 후보 영상 슬레이트 → 클릭/시청비율/좋아요 결정
        ▼
   BigQuery <proj>.persona.interactions  (학습용 상호작용 이벤트)
        │
③ 추천 모델 학습          (예정) BQML matrix_factorization → Vertex AI Two-Tower
        ▼
④ 추천 서빙               (예정) 신규 유저 행동 → 영상 랭킹/추천
```

현재 구현: **①(이미 있음) + ②(이 모듈)**. ③④는 다음 단계.

## 파일

| 파일 | 역할 |
|------|------|
| `common.py` | 페르소나→system prompt 빌더, Vertex Gemini 호출, JSON 파싱 |
| `ingest_personas.py` | ko_KR 데이터셋 → GCS + BigQuery(`personas`) 적재 |
| `simulate_behavior.py` | Cloud Run Job: 가상 유저가 영상 슬레이트에 행동 → `interactions` 적재 |
| `api.py` | (선택) Cloud Run Service: 페르소나 인터랙티브 대화 API |
| `Dockerfile` / `cloudbuild.yaml` / `deploy.sh` | 컨테이너 빌드·배포 |

## 행동 데이터 스키마 (`interactions`)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `run_id` | STRING | 시뮬레이션 실행 ID |
| `persona_id` | STRING | 가상 유저(페르소나) ID |
| `video_id` | STRING | 노출된 영상 ID |
| `rank` | INT64 | 슬레이트 내 노출 순위 |
| `clicked` | BOOL | 클릭 여부 |
| `watch_ratio` | FLOAT64 | 시청 지속 비율(0~1, 클릭 시) |
| `liked` | BOOL | 좋아요 여부 |
| `created_at` | TIMESTAMP | 생성 시각 |

## 사전 준비 / 배포

`gcloud` 설치·인증·프로젝트는 [`../gcp/README.md`](../gcp/README.md) "사전 준비"와 동일. Vertex AI(aiplatform) API는 deploy.sh가 활성화한다.
페르소나 데이터셋은 NGC/HuggingFace에서 라이선스 동의 후 `PERSONA_HF_REPO`(예: `nvidia/Nemotron-Personas-Korea`)로 지정.

```bash
# deploy.sh 상단 변수 확인 후
bash persona-agents/deploy.sh
```

배포 후: ① `ingest_personas.py`로 페르소나 적재 → ② `gcloud run jobs execute persona-sim`으로 행동 데이터 생성 → BigQuery `interactions` 확인.

## 비용/주의

- Gemini 호출량 = 가상 유저 수(`SAMPLE_USERS`) × 1회(슬레이트 일괄 평가). `SLATE_SIZE`로 슬레이트 크기 조절.
- 합성 데이터이며 실제 개인이 아니다. 데이터셋 라이선스/이용약관을 준수해 사용한다.
