# Codex OAuth runner 격리와 모델 라우팅 설계

## 목적

OpenAI API 키 없이 팀 공용 ChatGPT OAuth로 Codex CLI를 사용하는 경우에도,
외부 입력 프롬프트가 OAuth 자격 증명·DB 연결 정보·GCP 자격 증명·호스트 파일을
읽어 응답 또는 PostgreSQL에 유출하지 못하도록 실행 경계를 분리한다.

이 문서는 `agent_orchestration/`의 1단계 로컬 스켈레톤 이후 배포 구현의 설계
계약이다. 구현 전까지 Codex OAuth 모드는 신뢰한 로컬 환경의 비민감 프롬프트 검증에만
사용한다.

## 구조

```text
외부 호출자
  → Agent Orchestration API
      ├─ X-Orch-Token 검증·요청 계약 검증
      ├─ PostgreSQL 저장
      └─ private Codex runner 호출
             └─ Codex CLI OAuth 추론
```

- API는 Ingress가 연결되는 유일한 컴포넌트이며, runner는 클러스터 내부 전용
  Service로만 노출한다.
- API와 runner는 서로 다른 Pod, 서비스 계정, 파일시스템, PID namespace를 사용한다.
- API Pod에는 Codex OAuth 자격 증명을 두지 않는다. runner에는 PostgreSQL URL,
  `.env`, 소스 저장소, GCP ADC, Kubernetes 서비스 계정 토큰을 두지 않는다.
- runner는 non-root, read-only root filesystem, 빈 임시 볼륨, 최소 CPU·메모리·PID
  제한으로 실행한다. `hostPath`와 서비스 계정 토큰 자동 마운트는 금지한다.
- runner는 Codex 캐시·세션·임시 파일 전용 writable emptyDir를 `HOME`, `TMPDIR`,
  `XDG_CACHE_HOME`, `XDG_STATE_HOME`으로 제공한다. OAuth 자격 증명 저장소와 이
  요청별 쓰기 경로는 분리하며, 프록시·CA 등 Codex 연결에 필요한 비밀 아닌 환경 변수도
  부모 프로세스 상속 대신 허용 목록으로 명시한다.
- NetworkPolicy는 API → runner 내부 호출과 runner → Codex가 필요한 승인된 외부
  목적지만 허용한다.

## OAuth 자격 증명 경계

`--sandbox read-only`는 OAuth 자격 증명을 보호하는 경계로 간주하지 않는다. 특히
`CODEX_HOME/auth.json`을 runner에 파일로 제공하면 프롬프트가 파일 읽기를 유도할 수
있다.

- 먼저 Codex CLI의 OS keyring 자격 증명 저장(`cli_auth_credentials_store = "keyring"`)
  이 GKE runner에서 비대화형으로 동작하는지 검증한다. 공식 문서:
  <https://learn.chatgpt.com/docs/auth.md>
- keyring이 runner에서 안전하게 동작하지 않거나, runner의 Codex 도구가 자격 증명에
  접근할 수 있음을 배제하지 못하면 OAuth 기반 배포는 진행하지 않는다.
- runner는 Codex `stderr` 원문을 일반 애플리케이션 로그에 남기지 않는다. stderr 또는
  LLM 응답의 문자열 마스킹은 진단 보조 수단일 뿐 자격 증명 보호 대책이 아니다. 인증
  파일·환경 변수·프로세스 환경을 읽을 수 없는 격리 자체가 필요하다.
- 유출 의심 시 runner를 즉시 중지하고 `codex logout` 후 팀 공용 계정을 재로그인한다.

## 모델 전환과 라우팅

처음 배포에서는 승인된 모델 하나를 runner 설정의 `CODEX_MODEL`로 고정한다. 모델을
바꾸는 경우에는 설정 변경과 배포로 전환하며, 사용자 입력의 임의 모델명을 CLI에
전달하지 않는다.

향후 모델 선택 기능은 API 계약에 허용 목록 키만 추가한다.

```json
{
  "prompt": "CTR 개선안을 제안해줘",
  "model_key": "fast"
}
```

API 내부 매핑 예시는 `fast → 승인된 Codex 모델명`이다. 모델별 네트워크, 사용량,
실행 정책이 다르면 `model_key`별 runner Deployment를 분리한다. 이 확장 전까지
`/chat`의 요청 본문은 `prompt`만 받는 현재 계약을 유지한다.

## 배포 전 검증 조건

1. runner에서 `auth.json`, `/proc/*/environ`, GCP ADC, 서비스 계정 토큰, API 소스와
   DB 연결 정보를 프롬프트로 읽으려는 침투 테스트가 모두 실패한다.
2. runner는 API Pod 이외의 호출을 거부하고, 외부 Ingress로 직접 접근할 수 없다.
3. API와 runner의 로그·응답·PostgreSQL 저장에 자격 증명 값이 남지 않는지 확인한다.
4. Codex CLI 버전과 `--sandbox`, `--ephemeral`, `--skip-git-repo-check`, `-C`, `-o`
   인자 계약을 배포 이미지에서 smoke test로 검증한다.
5. Codex 실행 동시성 상한과 초과 시 `429` 또는 `503` 응답 계약, PostgreSQL 커넥션 풀,
   API Gateway/Ingress의 요청 본문 상한과 IP·인증 주체별 rate limit 정책을 확정한다.
6. read-only root filesystem과 writable scratch `HOME`/`TMPDIR` 환경에서 OAuth 상태를
   포함하지 않는 `codex exec` smoke test를 통과하고, 필요한 프록시·CA 설정이 허용
   목록 외의 부모 환경 없이 동작함을 확인한다.

위 조건 중 하나라도 충족하지 못하면 Codex OAuth 배포는 차단한다.
