# Harness 기반 Virtual User 생성 설계 (MVP 100건)

> **상태:** 설계(spec). 구현 계획(implementation plan)은 이 문서 승인 후 별도로 작성한다.
> **이전 방향과의 관계:** 이 문서는 기존 "무손실 정규화(near-lossless normalization)" 방향을 **대체**한다. `SourcePersona` 정규화 레이어와 "GLM은 derived feature만 생성" 전제를 제거하는 방향 전환이다. 충돌하는 옛 문서 정리 내역은 맨 아래 "기존 문서 정리" 참고.

---

## 1. 목표와 범위

**목표:** 원본 persona raw 데이터 100건을 샘플링해, 현재와 동일한 형태의 `virtual_user` 테이블 100행을 생성한다.

**핵심 전환:**
- 원본 persona row를 **정규화하지 않고**, raw dict 그대로 system/user prompt 하네스에 넣는다.
- LLM이 **한 번의 호출로 전체 schema를 생성**한다 (인구통계/사실 필드 포함).
- 겹겹이 달린 pydantic contract를 줄인다 (contract 다이어트).
- 오염 데이터는 생성 단계에서 막지 않고 **후처리**로 넘긴다.

**출력 schema:** 현재 `VirtualUser` 테이블과 **동일**하다 (필드 변경 없음). YouTube 소비성향 중심 feature를 유지한다.

**이번 범위 밖 (향후 과제):**
- 10만 건 대규모 실행, 증분 저장 / resume, throughput 튜닝 — 이는 `virtual_users`가 아니라 **`user_action_logs`(별도 spec)** 의 문제다.
- `user_action_logs` 시뮬레이터 자체 — 별도 spec.

## 2. 전체 시스템에서의 위치

```
persona → [virtual_users (이 문서, 소수)] → user_action_logs (별도 spec, 대량 ≈10만)
                                                     ↓
                                          개인별 YouTube reranking ML 학습
```

- `virtual_user`의 feature(`category_affinity`, `youtube_profile`, keyword group 등)는 **3단계 `user_action_log` 시뮬레이터의 입력**이 된다. 이 feature들을 유지하는 이유가 여기에 있다.
- MVP에서 virtual_user는 100명 규모이고, 이후 action log가 이들로부터 대량 생성된다.

## 3. 데이터 흐름

**Before (현재):**
```
raw row → SourcePersona(무손실 정규화) → GLM(derived만) → 코드가 factual 병합 + affinity 계산 → VirtualUser
```

**After (이 설계):**
```
raw row(dict, 정규화 X)
  → [균형 샘플러: raw dict에서 age/sex만 읽어 쏠림 없이 100건 샘플]
  → [LLM 하네스: raw row 통째로 prompt → 전체 VirtualUser schema JSON 생성]
  → [parse + VirtualUser 검증]
       ├─ 성공 → 유효 행 → parquet + warehouse jsonl
       └─ 실패 → quarantine.jsonl (원본 row + raw 응답 + 에러)
```

## 4. Contract 다이어트

| 모델 / 로직 | 처리 | 이유 |
|---|---|---|
| `SourcePersona` (정규화 모델) | **제거** | raw dict를 그대로 prompt에 넣음 |
| `DerivedVirtualUserFeatures` (중간 계약) | **제거** | LLM이 전체 schema를 한 번에 생성 |
| `categories.build_category_affinity` 병합, `interests.py` 파생 | **생성 경로에서 제거** | LLM이 `category_affinity`까지 직접 생성 |
| `_virtual_user_from_derived_features` 2단계 병합 | **제거** | "LLM 출력 → 검증" 1단계로 단순화 |
| `VirtualUser` (출력 계약) | **유지** | 검증 게이트 — 통과 못 하면 quarantine |
| `YouTubeProfile`, `GenerationMeta` | **유지** | VirtualUser 하위 구조 / 코드 stamp |

→ "정규화 → 중간 → 병합" 3단계가 "LLM → 검증" 1단계로 줄어든다.

## 5. LLM 하네스 (핵심)

- **system prompt:** virtual_user row generator 역할 부여. "아래 원본 persona를 근거로 지정된 JSON schema를 채워라. 없는 정보를 지어내지 마라. category는 허용 vocabulary 안에서만 선택하라. Markdown/주석 없이 JSON만 출력하라."
- **user prompt:** raw persona row(JSON) + 요구 출력 schema(전체 VirtualUser 필드) + 허용 category vocabulary.
- **LLM이 생성하는 값:** 인구통계/사실 필드(age, sex, occupation, province, district, education_level 등) **포함 전체** + persona_summary + keyword group + primary_categories + category_evidence + category_affinity + youtube_profile(affinity, watch_time_band).
- **코드가 stamp하는 값 (LLM에 맡기지 않음):** "내용은 LLM, 꼬리표는 코드." 아래는 콘텐츠가 아니라 장부/메타성 값이라 코드가 직접 채운다. LLM이 만들면 오히려 틀리거나(오타로 추적 링크 깨짐) 의미가 없다.
  - `virtual_user_id` — 배치 순번(`vu_0001`…). 루프 인덱스라 코드가 이미 안다.
  - `source_uuid` — 프롬프트에 넣은 원본 persona uuid를 그대로 복사 (원본 추적 링크 보존).
  - `source_hash` — 원본 row의 추적용 해시. 코드가 deterministic하게 계산.
  - `generation_meta` — "어떻게 생성했나" 메타데이터 (schema_version / prompt_version / llm_model / generated_at). 전부 코드가 아는 사실.

### 트레이드오프 (의도적 결정)
LLM이 인구통계까지 생성하므로 **원본 값이 훼손될 가능성이 존재**한다 (조용한 변형, 전사 오류, 누락, 환각). 하네스로 위험을 줄이지만 확률적으로만 지켜진다. 이 위험은 아래 **7. Quarantine + 후처리**와 **9. 오염 대응 fallback**으로 관리한다. MVP에서는 속도·단순성을 우선해 이 방식을 택한다.

## 6. 장애 격리 (fault isolation)

**현재 결함:** `pipeline._generate_users()`는 한 행 생성 실패 시 예외가 전파되어 **배치 전체가 중단**된다. 10만 건 중 한 건만 오염돼도 앞의 전부가 날아간다.

**요구사항 (전제):** 각 행 생성을 try/catch로 격리한다. 한 행의 실패가 배치를 죽이지 않고, 실패는 기록만 하고 계속 진행한다.

- LLM 호출(API 에러)과 parse/검증(JSON·schema 위반)을 모두 격리한다.
- 실패 유형 분류: `api_error`, `invalid_json`, `schema_fail`.

## 7. Quarantine + 후처리

유효 행과 실패 행을 **분리 출력**한다.

- **유효 행** → `virtual_users_*.parquet` (기존 Arrow schema 유지) + warehouse `.jsonl` (기존 `to_warehouse_row` 유지)
- **실패 행** → `quarantine.jsonl`, 한 줄 = `{source_uuid, raw_row, raw_llm_response, error_type, error_message}`
- **원본 스냅샷** → raw persona jsonl (재현성용, 기존 유지)
- 배치 종료 시 **요약 로그**: 총 N건 중 성공 / quarantine, 에러 유형별 개수.

메인 테이블은 항상 유효 행만 담아 downstream(action_log)이 깨끗한 입력을 받는다. 오염 행은 quarantine에서 나중에 분석·재처리한다.

## 8. 샘플링 (쏠림 방지 — 유지)

10만 건 규모에서 persona가 특정 성별·연령대로 쏠리는 것을 막는 장치이므로 **다이어트 대상이 아니다.**

- 기존 `sample_personas_by_contract`의 연령대·성별 균형 계약(seed 고정, 재현 가능)을 **유지**한다.
- 단, 정규화를 제거하므로 이 샘플러는 **raw dict에서 직접 age/sex를 읽어** 균형을 맞추도록 조정한다 (`normalize_sex` 헬퍼 재사용). 샘플링에 필요한 age/sex를 읽지 못하는 row는 샘플 풀에서 제외한다.
- MVP 100건은 기본값(예: 20대, 남 50 / 여 50)으로 실행한다.

## 9. 오염 대응 fallback (포인터)

오염이 실제 문제가 되면, LLM이 인구통계까지 생성하는 대신 **"LLM은 derived feature만, 인구통계/사실 필드는 코드가 raw row에서 직접 splice"** 방식으로 전환할 수 있다 (인구통계 오염 위험 0, 출력 토큰 축소로 대규모 실행에 유리).

지금은 채택하지 않으며, 상세 설계는 실제로 필요해질 때 별도로 작성한다. (이 문단은 전환 선택지가 있다는 것만 기록해 둔다.)

## 10. 테스트 계획

- 기존 테스트(`schema` / `persona_source` / `glm_generator` / `pipeline`)를 새 구조에 맞게 재작성한다.
- **핵심 신규 회귀 테스트:** 한 행이 오염돼도 배치가 끝까지 돌고 quarantine에 정확히 1건이 남는지 (장애 격리).
- rule-based fixture generator는 API 없이 **전체 VirtualUser JSON**을 생성하도록 유지하고, 동일한 검증/quarantine 경로를 탄다.
- 검증: `python -m pytest -q`.

## 11. 완료 기준

- raw persona 100건 → 유효 virtual_user 행 + (있으면) quarantine 행으로 분리 저장된다.
- 한 행 실패가 배치 전체를 중단시키지 않는다 (회귀 테스트 통과).
- 출력 parquet/warehouse schema는 기존과 동일하다.
- 샘플러가 성별·연령 균형을 재현 가능하게 유지한다.
- 오염 대응 fallback 전환 경로가 이 문서에 명시돼 있다.

---

## 기존 문서 정리

이 설계는 기존 "무손실 정규화" 방향을 대체하므로, 같은 폴더의 옛 문서 중 방향이 충돌하는 것은 정리 대상이다. (구체 판정·삭제는 사용자 확인 후 진행)
