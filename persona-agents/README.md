# Persona Agents — Nemotron-Personas(ko_KR) 기반 가상 사용자

NVIDIA [Nemotron-Personas (ko_KR)](https://catalog.ngc.nvidia.com/orgs/nvidia/nemotron-personas/resources/nemotron-personas-dataset-ko_kr)
의 한국어 합성 페르소나를 각각의 LLM 에이전트로 구동해 **합성 대화/행동 데이터**를 생성한다.
모델은 Vertex AI Gemini, 실행은 배치(Cloud Run Job) + 인터랙티브(Cloud Run Service) 둘 다 지원.

## 구성

```
                         ┌────────── Vertex AI Gemini ──────────┐
                         │                                      │
GCS(원본 parquet)        ▼                                      ▼
   └─▶ BigQuery.personas ──▶ Cloud Run Job (generate_batch)  Cloud Run Service (api)
                              │  페르소나×시나리오 대량 생성      │  실시간 페르소나 대화
                              ▼                                  ▼
              BigQuery.persona_dialogues + GCS(JSONL)        HTTP /chat
   페르소나 데이터셋: HuggingFace/NGC → ingest_personas.py
```

## 파일

| 파일 | 역할 |
|------|------|
| `common.py` | 페르소나→system prompt 빌더, Vertex Gemini 호출 |
| `ingest_personas.py` | ko_KR 데이터셋 → GCS + BigQuery(personas) 적재 |
| `generate_batch.py` | Cloud Run Job: 페르소나×시나리오 합성 대화 대량 생성 |
| `api.py` | Cloud Run Service: 페르소나 기반 인터랙티브 대화 API |
| `Dockerfile` / `cloudbuild.yaml` / `deploy.sh` | 컨테이너 빌드·배포 |

## 사전 준비

`gcloud` 설치·인증·프로젝트는 [`../gcp/README.md`](../gcp/README.md)의 "사전 준비"와 동일.
추가로 **Vertex AI**(aiplatform) API가 필요하며 deploy.sh가 활성화한다.

데이터셋은 NGC/HuggingFace에서 라이선스 동의가 필요할 수 있다. ko_KR repo id 를 확인해
`PERSONA_HF_REPO`(예: `nvidia/Nemotron-Personas-Korea`)로 지정하고, 비공개면 `PERSONA_HF_TOKEN`을 둔다.

## 배포

```bash
# deploy.sh 상단 변수(PROJECT_ID 등) 수정 후
bash persona-agents/deploy.sh
```

배포가 끝나면 출력 안내대로 ① 페르소나 적재 → ② 배치 실행 → ③ API 호출 순으로 사용한다.

## 산출물

- `BigQuery.persona_dialogues`: `run_id, persona_id, scenario, messages(JSON), model, created_at`
- `gs://<버킷>/persona_dialogues/<run_id>.jsonl`: 동일 내용 JSONL 백업

## 비용/주의

- Gemini 호출량 = 페르소나 수 × 시나리오 수 × 턴 수. `SAMPLE_SIZE`/`TURNS`/`SCENARIOS`로 조절.
- 합성 데이터는 실제 개인이 아니며, 데이터셋 라이선스/이용약관을 준수해 사용한다.
