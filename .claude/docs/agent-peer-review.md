# Peer Review Execution Guide

> Last Updated: 2026-07-06

코드/diff 리뷰에 사용하는 가이드입니다. 계획 문서 리뷰는
`agent-plan-review.md`를 사용합니다.

## When To Use This Doc

- 변경이 여러 파일이나 데이터 계약에 걸칠 때
- 스키마, DAG, workflow, 배포 동작이 바뀌었을 때
- 커밋이나 PR 생성 전 최종 품질 점검이 필요할 때
- PR 리뷰를 수행할 때

## Default Review Perspectives

| 관점 | 초점 |
| --- | --- |
| Critic | 기존 저장소 패턴에 맞는가, 기존 동작의 의도치 않은 변경이 없는가 |
| Code quality | 모듈 경계가 명확한가, 중복이 적은가, 추상화가 정당한가 |
| Convention | 이름, import, 문서, 타입, 검증이 저장소 규칙을 따르는가 |
| Security | 시크릿, 경로, 외부 입력, 권한이 안전하게 처리되는가 |

자격 증명, 외부 입력, workflow, 배포 설정이 바뀌었으면 Security
관점을 반드시 포함합니다.

## Review Focus (CLAUDE.md 리뷰 가이드와 동일)

심각도 순으로:

1. 정확성 버그와 기존 동작의 의도치 않은 변경
2. 시크릿·자격 증명 처리 위험
3. 데이터 스키마/계약(pydantic 모델, parquet 스키마) 변경 위험
4. 변경된 동작에 대한 테스트 누락·부실
5. 타입 안정성 문제
6. 전제 조건과 영향이 명확한 성능 문제

## Review Prompt Template

```text
Autoresearch의 현재 diff를 critic, code quality, convention, security
관점에서 리뷰하라. 정확성 버그, 기존 동작의 의도치 않은 변경, 테스트
누락, 안전하지 않은 권한, CLAUDE.md 및 .claude/docs/*.md 위반에
집중하라. 발견 사항을 파일·라인 참조와 함께 심각도 순으로 반환하라.
발견 사항이 없으면 남은 검증 리스크를 명시하라.
```

## Output Rules

- 발견 사항을 심각도 순으로 먼저 제시합니다.
- 파일과 라인 참조를 포함합니다.
- 칭찬은 나열하지 않습니다.
- 요약보다 구체적 발견이 우선입니다.
- 테스트 공백과 검증 공백을 명시적으로 지적합니다.
- 구체적 코드 이슈는 인라인 코멘트로, 요약 코멘트는 짧게 유지합니다.
