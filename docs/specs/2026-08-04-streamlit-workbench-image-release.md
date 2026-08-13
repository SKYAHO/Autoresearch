# Streamlit Experiment Workbench 이미지·릴리스 계약

## 목적

`agent_orchestration/ui`의 Streamlit Experiment Workbench를 GKE 내부에서
재현 가능하게 실행할 별도 컨테이너 이미지를 제공한다. 이 문서는 UI 이미지의
소스 경계, immutable digest handoff, 기존 Agent Orchestration API·Runner 이미지와의
분리를 정한다.

## 범위

- `deployment/experiment_platform/workbench.Dockerfile`로 Streamlit UI 전용 이미지를 정의한다.
- 기존 release workflow가 동일한 `main` source SHA에서 UI 이미지를 build·push·검증한다.
- release summary가 인프라 저장소가 소비할 UI 이미지의 immutable digest를 남긴다.

다음은 범위 밖이다.

- GKE Deployment, Service, NetworkPolicy, port-forward 권한은
  `SKYAHO/Autoresearch-infra` 소유이다.
- UI의 Experiment 생성·조회 동작과 Experiment API 계약은 변경하지 않는다.
- 외부 Ingress, LoadBalancer, 사용자 인증, 새 IAM 권한은 추가하지 않는다.

## 이미지 경계

UI 이미지는 Python과 `orchestration`·`orchestration-ui` dependency group,
`agent_orchestration/ui`, 그리고 UI가 읽는 `app/database.py`와 Experiment API 표시 모델
의존성만 포함한다. `app/main.py` FastAPI 서버, Codex CLI, OAuth 상태, DB bootstrap,
Runner 소스는 포함하지 않는다.

컨테이너는 UID/GID `10001`의 비루트 사용자로 실행한다. Streamlit은
`0.0.0.0:8501`에서 headless 모드로 실행하며, `PYTHONPATH=/app`으로
`applications/experiment_platform/workbench/app.py`를 로드한다. telemetry와 source watcher는 비활성화한다.
`/_stcore/health`가 readiness·liveness 경로다. API base URL과 토큰은 이미지에 넣지
않으며, Kubernetes Deployment가 런타임 환경 변수로 주입한다.

## 릴리스 계약

기존 `release.yml`의 Agent Orchestration API·Runner publish job과 같은 source SHA를
사용하는 독립 UI publish job을 추가한다.

1. `main`의 immutable source SHA를 checkout한다.
2. `autoresearch-agent-orchestration-ui:sha-<source-sha>` 태그로 Artifact Registry에
   push한다.
3. 반환된 OCI digest를 pull한 뒤 OCI revision label, 비루트 사용자, 실제 Streamlit
   기동과 `/_stcore/health` 응답을 검증한다.
4. release summary에 `digest_ref`를 기록한다.

인프라 배포 manifest는 mutable tag가 아니라 이 `digest_ref`만 참조한다. UI 코드의
롤백은 이전 source SHA에서 생성된 digest를 manifest에 다시 고정하는 방식으로 한다.

## 운영·보안 제약

- `ORCH_UI_API_TOKEN`은 release workflow, Docker build argument, Docker layer,
  로그, 문서에 절대 포함하지 않는다.
- UI 이미지는 API와 같은 클러스터 내부 Service를 호출하되, 외부 네트워크를
  필요로 하지 않는다.
- release workflow의 OIDC/WIF 및 GAR push 권한은 기존 이미지 publish job의
  최소 권한 범위를 재사용하며, 새 secret·새 서비스 계정은 만들지 않는다.

## 완료 조건

- release workflow가 UI source SHA와 OCI digest를 검증해 GAR에 UI 이미지를 push한다.
- 이미지가 API·Runner 이미지와 독립된 Streamlit 전용 실행 단위다.
- 인프라 작업자가 release summary의 digest만으로 배포 manifest를 작성할 수 있다.
