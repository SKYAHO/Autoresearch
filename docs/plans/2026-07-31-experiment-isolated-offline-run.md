# 실험별 Feast Registry·offline 실행 격리 구현 계획

> 후속 계획: `docs/plans/2026-08-03-paired-offline-experiment-comparison.md`(#454).
> 실행 context는 조건(baseline/candidate) 격리 좌표로 확장됐다.

## 1. 실행 context 생성기

- [x] `experiment_id`, `condition`, `source_sha`, issue 번호를 검증한다.
- [x] Registry key와 artifact prefix를 결정론적으로 만든다.
- [x] GCS root와 결합한 URI를 생성하고 다른 실험 경로를 거부한다.

## 2. 실행 계약

- [ ] workflow dispatch payload에 image digest, Registry URI, run ID, artifact/log URI를 포함한다.
- [ ] Redis·materialize를 호출하지 않는 offline 실행 경로를 명시한다.
- [ ] 실패 결과도 `#450` 결과 payload로 반환한다.

## 3. 인접 저장소 연결

- [ ] Autoresearch-airflow가 context payload로 실험 Job을 생성한다.
- [ ] Autoresearch-infra에 실험 namespace·IAM·TTL 정책을 반영한다.
- [ ] callback의 lineage 필드를 `#450` 계약과 대조한다.

## 4. 검증

- [ ] 서로 다른 두 실험의 Registry·artifact 경로가 겹치지 않는지 단위 테스트한다.
- [ ] 같은 실험·SHA 재실행이 Registry URI를 재사용하는지 테스트한다.
- [ ] workflow 계약과 `git diff --check`를 실행한다.
