# Main/Dev 배포 경로 분리

## 목적

배포 가능한 변경을 브랜치 기준으로 분리한다. `main`은 prod 경로만, `dev`는
dev 환경만 사용할 수 있어야 하며, 수동 실행으로 이 경계를 우회하지 않는다.

## 현재 문제

- 원격 `dev` 브랜치와 dev push 트리거가 없다.
- `dev` GitHub Environment가 `main`만 허용해 브랜치 모델과 충돌한다.
- release workflow는 임의 SHA를 받아, `main`에 병합되지 않은 커밋도 이미지
  릴리스와 Airflow digest 승격으로 이어질 수 있다.
- main ruleset은 PR 승인·squash·두 pytest만 강제하며, 대화 해결·최신 push 후
  재승인·Lint는 강제하지 않는다.

## 확정 동작

| 소스 브랜치/이벤트 | 대상 환경 | 허용 작업 |
| --- | --- | --- |
| `main` push (Feast 대상 파일) | `prod` | Feast apply만 수행한다. |
| `dev` push (Feast 대상 파일) | `dev` | Feast apply만 수행한다. Redis online store 삭제 스캔은 끈다. |
| published release | `prod` | release target이 `main` 조상이면 이미지 발행과 Airflow digest 승격 PR을 수행한다. |
| workflow_dispatch | 선택 환경 | 운영자 수동 적용은 허용하되, prod source SHA는 `main` 조상만 허용한다. |

`dev`에는 image release 및 Airflow digest 승격을 추가하지 않는다. 이 저장소의
Airflow/GCP 배포 소유 경계도 유지한다.

## 구현 결정

1. Feast workflow는 main/dev push를 받되 job environment와
   `AUTORESEARCH_ENV`를 `github.ref_name`으로 결정한다. 수동 실행 입력은
   기존 `environment` 선택을 유지한다.
2. GitHub Environment branch policy는 prod=`main`, dev=`dev`로 설정한다.
3. release workflow는 체크아웃 SHA가 `origin/main`의 조상인지 검증한다.
4. main ruleset에는 Lint, Feast group pytest, thread resolution, 마지막 push
   승인 규칙을 추가한다. 이 설정은 GitHub API로 적용하며 코드와 같은 PR이
   병합된 뒤 유효해진다.

## 검증

- workflow 정적 계약 테스트를 main/dev 분기로 확장한다.
- release SHA 검증을 테스트한다.
- GitHub API로 ruleset, Environment branch policy, dev branch 존재를 다시 읽는다.
- 기존 성공 run과 새 dev 경로 실행을 확인한다. prod WIF 장애는 infra 이슈로
  별도 추적하며, 고치지 못한 경우 차단 상태를 명시한다.
