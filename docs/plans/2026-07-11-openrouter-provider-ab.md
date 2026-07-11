# Plan: OpenRouter provider 자동/고정 라우팅 A/B 구현

- Status: Implementation Complete; publish pending
- Date: 2026-07-11
- Issue: `SKYAHO/Autoresearch#116`
- Spec: `docs/specs/2026-07-11-openrouter-provider-ab.md`
- 대상 파일: `autoresearch/action_logs/llm_generator.py`,
  `autoresearch/action_logs/daily.py`, `.env.example`, action-log 문서
- 테스트: `tests/test_action_logs_llm_generator.py`,
  `tests/test_action_logs_daily.py`, `tests/test_action_logs_observability.py`

## 구현 순서

### 1. Routing mode 정규화와 payload 분리

- `default`, `auto`, `fixed` mode와 fixed provider slug를 생성자 입구에서
  검증합니다.
- default는 기존 explicit/env preference resolution을 유지합니다.
- auto는 ambient/explicit preference를 읽지 않고 provider payload를 비웁니다.
- fixed는 ambient/explicit preference를 무시하고 `only=[slug]`,
  `allow_fallbacks=false`만 생성합니다.
- model 문자열은 어느 mode에서도 바꾸지 않습니다.

### 2. 공식 metadata opt-in과 안전한 집계

- completion 요청에 `X-OpenRouter-Metadata: enabled`를 추가합니다.
- 성공 응답의 `model_extra.openrouter_metadata`를 type/범위 검사하며
  permissive하게 읽습니다.
- 선택 provider, router attempt/fallback/429 수만 구조화 로그에 추가합니다.
- 기존 response-level provider fallback과 application retry 필드를 유지합니다.

### 3. Daily 공개 계약과 fingerprint

- 두 daily runner의 keyword-only signature에 mode/slug/expected count를 추가합니다.
- `_build_generator`가 routing 계약을 OpenRouter generator로 전달하고,
  rule-based에 non-default mode가 오면 거부합니다.
- 전체 virtual user 수를 shard split 전에 검증합니다.
- generator fingerprint config를 manifest/checkpoint fingerprint에 그대로
  전파합니다.

### 4. 문서와 테스트

- `.env.example`에 ambient provider 설정은 default mode 전용임을 명시합니다.
- action-log README에 세 mode, metadata 필드, expected count를 기록합니다.
- 요청 payload, validation, fingerprint, metadata, 민감 본문 미노출,
  100-user precondition 테스트를 추가합니다.

## 검증 명령

```powershell
py -m uv run --no-sync python -m pytest `
  tests/test_action_logs_llm_generator.py `
  tests/test_action_logs_observability.py `
  tests/test_action_logs_daily.py -q
py -m uv run --no-sync ruff check autoresearch tests
git diff --check
```

Windows에서 기존 shard daily 테스트 일부는 별도 기본 checkpoint 부모 경로
결함으로 기준선부터 실패합니다. 이 작업에서는 결함을 수정하지 않고, 신규 shard
테스트에는 명시적 임시 checkpoint 경로를 주며 변경 전후 실패 집합을 비교합니다.

## 완료 체크리스트

- [x] routing mode/slug와 payload 계약 구현
- [x] router metadata 안전 집계 구현
- [x] daily API/expected count/fingerprint 구현
- [x] 문서와 `.env.example` 갱신
- [x] targeted tests와 ruff 결과 기록
- [ ] 한국어 커밋, push, Draft PR 생성(merge 금지)

## 검증 결과 (2026-07-11)

```text
action-log LLM/observability/pipeline: 67 passed
신규 daily 계약:                    11 passed, 18 deselected
전체 pytest:                         255 passed, 2 skipped, 12 failed
ruff check autoresearch tests:       passed
git diff --check:                    passed
```

전체 pytest의 12개 실패는 변경 전에도 동일하게 발생한 Windows 로컬 checkpoint
부모 경로 결함입니다. 이 작업은 해당 결함을 수정하지 않았고, 테스트가 저장소에
만든 `action_log_checkpoints/`와 `action_log_progress/` 생성물은 삭제했습니다.
추가 `ruff check .`는 소유 범위 밖 `examples/`·`scripts/`의 기존 9건으로
실패했으며, 필수 CI 범위인 `ruff check autoresearch tests`는 통과했습니다.
라이브 OpenRouter 요청과 100-user A/B benchmark는 후속 운영 검증 항목입니다.
