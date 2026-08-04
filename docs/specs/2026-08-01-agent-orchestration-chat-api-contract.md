# Agent Orchestration `/chat` API 계약

## 목적과 적용 범위

이 문서는 클러스터 내부 호출 서비스가 Agent Orchestration에 단일 프롬프트를
전달하고, 생성 결과를 받는 `POST /chat`의 정본 계약이다. API는 LLM 생성 결과를
PostgreSQL에 저장한 뒤에만 성공 응답을 반환한다.

현재 서비스는 GKE 내부 전용 ClusterIP Service다. 호출자는 같은 클러스터에서
서비스 DNS를 사용하거나, 운영자가 허용한 `kubectl port-forward` 경로로만 접근한다.
공개 URL, 외부 사용자 인증, 사용자별 세션은 이 계약의 범위가 아니다.

Runner의 `/v1/generate`와 Codex OAuth는 API 구현 내부 경계이며, 호출 서비스가
직접 사용하거나 의존해서는 안 된다.

## 호출 규칙

### 엔드포인트

```text
POST /chat
Content-Type: application/json
X-Orch-Token: <공유 API 토큰>
```

`X-Orch-Token`은 필수다. 이 토큰은 호출 서비스 인증용 공유 시크릿이며, Codex
OAuth 토큰과 다르다. 토큰 값은 코드·설정 예시·로그에 기록하지 않는다.

### 요청 본문

```json
{
  "prompt": "CTR 개선 방안을 세 줄로 요약해 주세요."
}
```

| 필드 | 타입 | 필수 | 제약 |
| --- | --- | --- | --- |
| `prompt` | string | 예 | 1자 이상, 8,192자 이하 |

요청 본문에는 `prompt` 외의 필드를 넣을 수 없다. 모델명, 사용자 ID, 대화 ID,
시스템 프롬프트는 현재 요청 계약에 포함하지 않는다. 모델은 운영 환경의 Runner
설정으로 선택된다.

## 성공 응답

정상 처리 시 HTTP `201 Created`를 반환한다.

```json
{
  "id": 2,
  "prompt": "CTR 개선 방안을 세 줄로 요약해 주세요.",
  "response": "...",
  "model": "gpt-5.6-luna",
  "latency_ms": 10153,
  "token_count": null,
  "created_at": "2026-08-01T00:32:05.216166+09:00"
}
```

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `id` | integer | 저장된 상호작용 레코드의 식별자 |
| `prompt` | string | 저장된 요청 프롬프트 |
| `response` | string | LLM이 생성한 응답 텍스트 |
| `model` | string | 해당 요청을 처리한 운영 모델 식별자 |
| `latency_ms` | integer | LLM 호출에 걸린 시간(밀리초). PostgreSQL 저장 시간은 제외 |
| `token_count` | integer 또는 `null` | 백엔드가 제공한 토큰 수. 제공하지 않으면 `null` |
| `created_at` | RFC 3339 datetime string | PostgreSQL에 레코드가 생성된 시각 |

`id`가 포함된 `201` 응답은 PostgreSQL 저장과 커밋까지 성공했음을 뜻한다. 현재
GKE 운영 환경에서는 `agent_orchestration.chat_interactions`에 `prompt`, `response`,
`model`, `latency_ms`, `token_count`, `created_at`을 저장한다. `prompt`와 `response`는
평문으로 보관하므로 비민감 데이터만 전송한다.

## 오류 응답

인증·저장·LLM 오류는 아래 공통 형식을 사용한다.

```json
{
  "detail": "오류 설명"
}
```

| HTTP 상태 | 발생 조건 | `detail` 값 |
| --- | --- | --- |
| `401 Unauthorized` | `X-Orch-Token`이 없거나 일치하지 않음 | `Invalid orchestration API token.` |
| `500 Internal Server Error` | LLM 응답은 받았지만 PostgreSQL 저장에 실패함 | `Failed to save chat interaction.` |
| `502 Bad Gateway` | Runner 또는 LLM 백엔드 호출 실패·시간 초과 | `Failed to call LLM backend.` |
| `503 Service Unavailable` | 서비스 기동 준비 전이거나 Runner가 동시 실행 한도를 초과함 | `Service is unavailable.` 또는 `LLM backend is temporarily overloaded.` |

요청 본문이 유효하지 않으면 FastAPI 검증 오류로 HTTP `422 Unprocessable Content`를
반환한다. 이 응답의 `detail`은 문자열이 아니라 오류 객체 배열이다.

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "prompt"],
      "msg": "String should have at least 1 character",
      "input": ""
    }
  ]
}
```

검증 오류 객체에는 제약에 따라 `ctx`, `url` 같은 추가 필드가 포함될 수 있으므로,
호출자는 오류 메시지 문자열이 아닌 HTTP 상태와 `detail` 배열 여부로 처리한다.

호출자가 연결을 끊으면 서버는 진행 중인 LLM 작업을 취소하고 `499`로 처리를
종료한다. 이 경우 PostgreSQL 저장은 수행하지 않는다. 클라이언트가 이미 연결을
종료했으므로 이를 일반적인 응답 계약으로 사용해서는 안 된다.

## 저장과 재시도 의미

처리 순서는 다음과 같다.

1. API가 인증 헤더와 요청 JSON을 검증한다.
2. API가 내부 Runner를 통해 운영 모델을 호출한다.
3. LLM 결과와 지연 시간을 PostgreSQL에 저장한다.
4. 저장된 행을 기반으로 `201` 응답을 반환한다.

LLM 호출 실패(`502` 또는 `503`)와 요청 취소(`499`)에는 상호작용 레코드를 저장하지
않는다. 저장 실패(`500`)에도 성공 응답을 반환하지 않는다.

현재 API에는 멱등 키가 없다. 따라서 HTTP 클라이언트, 프록시, 로드밸런서는 동일
프롬프트를 자동 재시도해서는 안 된다. 네트워크 단절로 성공 응답을 받지 못한 경우를
포함해 재시도하면 LLM 사용량이 추가로 발생하거나 PostgreSQL에 중복 레코드가 저장될
수 있다. 재시도가 필요한 호출 서비스는 호출 목적에 맞는 사용자 확인 또는 별도
멱등성 계약이 추가될 때까지 명시적으로 결정한다.

## 연동 예시

```bash
curl --request POST "http://<agent-orchestration-api>/chat" \
  --header "Content-Type: application/json" \
  --header "X-Orch-Token: <공유 API 토큰>" \
  --data '{"prompt":"CTR 개선 방안을 세 줄로 요약해 주세요."}'
```

개발·운영자가 접근 권한을 가진 환경에서는 Swagger UI의 `/docs`에서 동일한 요청·응답
스키마를 확인할 수 있다. Swagger는 이 문서의 보조 확인 수단이며, 호출 의미와
재시도 제한의 정본은 본 문서다.
