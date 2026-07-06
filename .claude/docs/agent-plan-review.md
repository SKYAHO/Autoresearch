# Plan Review Execution Guide

> Last Updated: 2026-07-06

구현 전 계획(plan) 문서를 리뷰할 때 사용하는 가이드입니다. 코드나
diff 리뷰는 `agent-peer-review.md`를 사용합니다.

## When To Use This Doc

- 대규모 다중 파일 변경
- 스키마·데이터 계약 변경 또는 마이그레이션
- DAG, CI workflow, 배포처럼 운영 리스크가 있는 변경
- CLI·공개 API 동작 변경
- "계획 리뷰"나 모호성 점검이 명시적으로 요청된 경우

## Review Questions

계획을 다음 질문으로 평가합니다:

1. 목표가 명확하고 범위가 한정되어 있는가?
2. 비목표(non-goal)가 범위 확장을 막을 만큼 명시적인가?
3. 영향받는 모듈과 소유권 경계가 정확한가?
   (`agent-project-reference.md`의 팀 도메인 기준)
4. 구조 변경과 동작 변경이 분리되어 있는가?
5. 테스트와 검증 명령이 구체적인가? (`python -m pytest`, Docker
   빌드 등 실제 실행 가능한 명령인가)
6. 스키마 변경, 롤백, 하위 호환 리스크가 다뤄져 있는가?
7. 가정이 명시되어 있고 확인 가능한가?
8. 요청된 결과에 비해 과잉 설계된 단계는 없는가?

## Output Shape

다음 순서로 반환합니다:

1. Blocking 이슈
2. Non-blocking 개선 사항
3. 모호한 부분과 가정
4. 제안하는 계획 수정

계획이 타당하면 그렇다고 말하고, 남아 있는 가장 큰 리스크를
나열합니다.
