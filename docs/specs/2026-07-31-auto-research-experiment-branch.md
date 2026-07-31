# Auto Research 이슈 기반 실험 브랜치

## 목적

Auto Research 이슈가 만들어지면 `dev` 최신 커밋에서
`exp/<issue>-<experiment-id>` 브랜치를 자동 생성해 가설별 실험의 출발점을
고정한다. 여러 실험이 같은 이슈를 공유할 수 있으므로 `experiment_id`를 branch와
Registry key에 포함한다.

## 구조화 성공 기준

이슈 폼은 자유 문장 성공 기준을 다음 기계 판독 필드로 대체한다.

- 주 지표 이름
- 주 지표 방향(`higher_is_better` 또는 `lower_is_better`)
- baseline 대비 최소 개선폭(0 이상의 decimal)
- guardrail 지표 이름·방향·허용 최대 비열화(모두 `없음`이면 미사용)

사람이 읽는 가설·변경·재현 조건은 기존 필드를 유지한다.

## 동작 계약

1. `auto-research`와 `experiment` 라벨이 붙은 이슈의 `opened` 또는 `labeled`
   이벤트만 처리한다.
2. workflow는 `dev` ref의 SHA를 조회한 뒤 `exp/<issue>-<experiment-id>`를 만든다.
3. 같은 이름의 ref가 이미 같은 SHA면 성공으로 끝내고, 다른 SHA면 ref를 이동하지
   않고 실패한다.
4. 성공 시 이슈에 branch, candidate SHA, 실험별 Registry key를 댓글로 남긴다.
5. Registry key는 `experiments/<issue>/<experiment-id>/<candidate-sha>/registry.db`
   형식이다. 실제 GCS bucket URI와 Registry 생성은 격리 실행 workflow가 담당한다.
6. 생성된 exp 브랜치는 공용 배포 브랜치가 아니며, 이 workflow는 PR·공용 dev 배포·실험
   Job을 생성하지 않는다.

## 보안·경계

- workflow는 `contents: write`, `issues: write`만 사용한다.
- 이슈 제목·본문을 shell 코드로 실행하지 않는다.
- Airflow·GCP 리소스는 수정하지 않는다.

## 검증

- Issue Form 구조화 필드와 workflow 트리거·권한·idempotency 계약을 pytest로 검사한다.
- workflow는 GitHub API의 create-ref 422를 idempotent case로만 처리한다.
