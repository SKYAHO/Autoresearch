# dev 환경 좌표 카탈로그 연동

GCP 쓰기 workflow는 인증 전 `Autoresearch-infra` 보호 브랜치의 dev 카탈로그와
GitHub 환경 변수를 대조합니다. 카탈로그 ref는 workflow 입력으로 받지 않으며,
비밀은 checkout하지 않습니다. WIF provider와 서비스 계정은 부트스트랩 anchor로
남지만 프로젝트·리전·GKE 좌표 불일치는 GCP 호출 전에 실패합니다.
