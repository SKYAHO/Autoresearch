# MLflow Tracking Server 배포 전략

> **작성일**: 2026-07-14  
> **상태**: 검증 진행 중 (TBD 항목 포함)  
> **범위**: #94 MLflow Tracking Server 배포 이미지 및 Runtime 의존성 확정

---

## 1. 목표

MLflow `v2.22.1`의 **공식 이미지를 기준**으로 PostgreSQL, MinIO/S3, GCS 연동에 필요한 Runtime 의존성을 검증한다.

검증 결과에 따라 다음 중 하나를 결정한다:

```
1. 공식 MLflow 이미지 (ghcr.io/mlflow/mlflow:v2.22.1) 직접 사용
2. 공식 MLflow 이미지 기반 최소 파생 이미지 사용
```

최종 Linux 배포 환경에서는 모든 Python 의존성을 **UV로 관리**하며, 컨테이너 시작 시 `pip install`을 실행하지 않는다.

---

## 2. 기본 원칙

### 실행 환경

| 항목 | 값 |
|---|---|
| 운영체제 | Linux |
| Python | 3.12 |
| MLflow | 2.22.1 (고정) |
| 의존성 관리 | UV (`uv sync`, `uv lock`) |

### 의존성 관리 전략

**로컬 프로젝트 환경:**
- `pyproject.toml`에 선언
- `uv.lock`으로 버전 고정
- `uv sync`로 환경 구성
- `uv run`으로 테스트 및 검증 실행

**Linux Docker 배포 환경:**
- 컨테이너 시작 시 패키지 설치 금지
- 이미지 **Build 단계**에서 UV를 사용해 의존성 설치
- `pyproject.toml` 및 `uv.lock` 기반
- `--frozen` 옵션으로 lockfile 일관성 보증

---

## 3. 현재 Compose 구성의 의미

### 현재 방식

```yaml
command: >
  sh -c "
    pip install psycopg2-binary &&
    mlflow server ...
  "
```

### 의도

PostgreSQL 연동 가능성을 **빠르게 확인**하기 위한 임시 로컬 검증 방식

### 문제점

| 문제 | 영향 |
|---|---|
| 매 시작마다 패키지 재설치 | Pod 시작 시간 증가 |
| PyPI 장애 시 MLflow 시작 실패 | 배포 안정성 저하 |
| 실행 시점마다 다른 환경 | 버전 불일치 가능 |
| `uv.lock` 미적용 | 재현성 및 롤백 능력 저하 |

### 최종 운영 환경

**Build 시점에 의존성 설치**, 컨테이너 시작 시 추가 설치 금지

---

## 4. MLflow 버전 전략

### 고정 버전

```
MLflow v2.22.1
```

### 선정 근거

프로젝트에서 다음 항목을 검증한 버전:

| 항목 | 상태 |
|---|---|
| Tracking Server UI | ✅ 검증 완료 |
| PostgreSQL Backend Store | ✅ 검증 완료 |
| Local Artifact Store | ✅ 검증 완료 |
| MinIO Artifact Store (S3 호환) | ✅ 검증 완료 (#136) |
| Artifact Proxy Mode | ✅ 검증 완료 |
| 학습 파이프라인 Tracking 연동 | ✅ 검증 완료 (bd5765a) |
| Run 영속성 (PostgreSQL) | ✅ 검증 완료 |

### 3.x 업그레이드 시점

- 전체 파이프라인 Kubernetes 배포 완료 후
- 호환성 검증을 별도 이슈로 진행
- Custom Fork 또는 Compression 기능 필요 시 재평가

---

## 5. 공식 이미지 우선 전략

### 기본 이미지

```
ghcr.io/mlflow/mlflow:v2.22.1
```

### 사용 조건

공식 이미지에 **필요한 Runtime 의존성이 모두 포함**되어 있으면 그대로 사용

### 확인 대상

| 의존성 | 연결 대상 | 확인 방식 |
|---|---|---|
| `psycopg2-binary` | PostgreSQL | `docker run ... python -c "import psycopg2"` |
| `boto3` | MinIO/S3 | `docker run ... python -c "import boto3"` |
| `google-cloud-storage` | GCS | `docker run ... python -c "from google.cloud import storage"` |

---

## 6. 최소 파생 이미지 사용 조건

### 정의

```
최소 파생 이미지
= 공식 MLflow 이미지
+ PostgreSQL/S3/GCS 연동에 필요한 Python 패키지
```

### 사용 기준

공식 이미지에 **필요한 의존성이 부족한 경우**에만 생성

### 포함 사항

- 누락된 Python 패키지 설치
- UV 기반 Build-time 설치
- 프로젝트 `pyproject.toml` 기반 버전 관리

### 제외 사항

| 항목 | 이유 |
|---|---|
| MLflow 소스 코드 수정 | Custom Fork는 후순위 |
| Artifact Compression | 이번 범위 제외 |
| Zstandard 포팅 | 이번 범위 제외 |
| MLflow 3.x 기능 포팅 | 전체 파이프라인 후 검토 |
| 불필요한 패키지 추가 | 최소 유지 원칙 |

---

## 7. Runtime 의존성 매트릭스

### 확인 상태

| 의존성 | 연결 대상 | 공식 이미지 포함 | Compose 설치 방식 | 최종 판단 |
|---|---|---|---|---|
| `psycopg2-binary` | PostgreSQL | **TBD** | 시작 시 `pip install` | **TBD** |
| `boto3` | MinIO/S3 | **TBD** | Compose 파일 확인 필요 | **TBD** |
| `google-cloud-storage` | GCS | **TBD** | 시작 시 `pip install` | **TBD** |

### 검증 일정

**Phase 3-4**: 공식 이미지 및 Compose Runtime 검증

---

## 8. UV 기반 최소 파생 이미지 (필요 시)

### Dockerfile 예시

```dockerfile
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM ghcr.io/mlflow/mlflow:v2.22.1

COPY --from=uv /uv /uvx /bin/

WORKDIR /opt/mlflow-runtime

COPY pyproject.toml uv.lock ./

RUN uv export \
      --frozen \
      --no-dev \
      --group mlflow-server \
      --no-emit-project \
      --output-file /tmp/mlflow-server-requirements.txt \
    && uv pip install \
      --system \
      --requirement /tmp/mlflow-server-requirements.txt \
    && rm -f /tmp/mlflow-server-requirements.txt

EXPOSE 5000
```

### 특징

- **Build 시점에 설치**: `uv pip install`로 의존성 설치
- **Lockfile 기반**: `uv.lock`으로 버전 재현성 보증
- **`--frozen` 옵션**: lockfile과 선언 불일치 시 Build 실패 → 안정성
- **컨테이너 시작 시 추가 설치 금지**: Pod 시작 시간 최소화
- **MLflow 소스 미수정**: 기본 이미지 기능만 사용

### pyproject.toml 설정

```toml
[dependency-groups]
mlflow-server = [
    "psycopg2-binary==X.X.X",   # PostgreSQL
    "boto3==X.X.X",               # MinIO/S3
    "google-cloud-storage==X.X.X" # GCS
]
```

실제 버전은 검증 결과를 반영하여 작성

---

## 9. Registry 전략

### 두 가지 옵션

| 옵션 | 장점 | 단점 | 선택 조건 |
|---|---|---|---|
| **A. GHCR 직접 사용** | 간단함, 외부 의존 최소 | 외부 Registry 의존 | 공식 이미지만 사용 |
| **B. GAR Mirror** | 접근 제어, 가용성 통제 | 추가 관리 | 커스텀 이미지 또는 정책 필요 |

### GAR 사용 이유

필요한 경우에만:
- 외부 Registry 의존성 축소
- GCP 내부 접근 제어
- 이미지 감사 및 관리 정책
- 최소 파생 이미지 저장

---

## 10. Artifact Store 연동

### 로컬 개발 환경

| Mode | Backend | Artifact Store |
|---|---|---|
| Local | PostgreSQL | Local volume |
| MinIO | PostgreSQL | MinIO (S3 호환) |
| GCS | PostgreSQL | GCS (선택적) |

### 최종 Kubernetes 환경

- Backend: PostgreSQL (Secret)
- Artifact: GCS (기본)

---

## 11. #94 작업 범위

### 포함 사항

- ✅ MLflow v2.22.1 공식 이미지 Runtime 확인
- ✅ PostgreSQL 연동 의존성 확인
- ✅ MinIO/S3 연동 의존성 확인
- ✅ GCS 연동 의존성 확인
- ✅ PostgreSQL + Local Artifact 로컬 검증
- ✅ PostgreSQL + MinIO Artifact 로컬 검증
- ✅ 컨테이너 재생성 후 Run 영속성 검증
- ✅ 공식 이미지 또는 최소 파생 이미지 결정
- ✅ Kubernetes 최종 이미지 URI 확정
- ✅ 선택 근거 및 검증 결과 문서화

### 제외 사항

| 항목 | 이유 |
|---|---|
| MLflow Custom Fork | 후순위 (#95+ 범위) |
| MLflow 소스 수정 | 최소 파생 이미지만 허용 |
| Artifact Compression | 이번 범위 제외 |
| Zstandard 포팅 | 이번 범위 제외 |
| MLflow 3.x 업그레이드 | 전체 파이프라인 후 검토 |
| Kubernetes Deployment | #95 범위 |
| Workload Identity | #95 범위 |
| GCS IAM 권한 설정 | #95 범위 |
| Kubernetes Pod 실제 업로드 | #95 범위 |

---

## 12. 검증 결과

### Phase 3: 공식 이미지 Runtime 의존성 확인

#### 이미지 정보

| 항목 | 값 |
|---|---|
| Image URI | `ghcr.io/mlflow/mlflow:v2.22.1` |
| Image ID | `sha256:752d6d7e9fae6c321a67632fdc835b42b2d39f6dd6d684e483f4de0772743e81` |
| Digest | `ghcr.io/mlflow/mlflow@sha256:752d6d7e9fae6c321a67632fdc835b42b2d39f6dd6d684e483f4de0772743e81` |
| 검증 날짜 | 2026-07-14 |

#### 공식 이미지 자체 의존성

| 패키지 | Import 결과 | 버전 | 오류 메시지 |
|---|---|---|---|
| `psycopg2` | ❌ 실패 | - | ModuleNotFoundError: No module named 'psycopg2' |
| `boto3` | ❌ 실패 | - | ModuleNotFoundError: No module named 'boto3' |
| `google-cloud-storage` | ❌ 실패 | - | ModuleNotFoundError: No module named 'google.cloud' |

#### Compose 시작 시 설치 후 Runtime

| 모드 | 패키지 | 결과 | 버전 |
|---|---|---|---|
| Local | `psycopg2` | ✅ 성공 | 2.9.10 |
| MinIO | `psycopg2` | ✅ 성공 | 2.9.10 |
| MinIO | `boto3` | ✅ 성공 | 1.43.47 |
| GCS | `google-cloud-storage` | ✅ 성공 | >=2,<3 |

---

## 13. E2E 검증 결과

### Phase 5: Local Artifact E2E ✅

| 항목 | 결과 |
|---|---|
| MLflow 서버 응답 | ✅ |
| Run 생성 | ✅ |
| Parameter 기록/검증 | ✅ |
| Metric 기록/검증 | ✅ |
| Artifact 기록/조회/다운로드 | ✅ |
| Run Status | FINISHED ✅ |

### Phase 6: MinIO Artifact E2E ✅

| 항목 | 결과 |
|---|---|
| MLflow 서버 응답 | ✅ |
| Run 생성 | ✅ |
| S3 호환 Artifact 업로드 | ✅ |
| Artifact 다운로드 및 검증 | ✅ |
| Run Status | FINISHED ✅ |

### Phase 7: 영속성 검증 (down→up) ✅

| 항목 | 결과 |
|---|---|
| PostgreSQL named volume | ✅ 영속성 확인 |
| MinIO Object 영속성 | ✅ 영속성 확인 |
| 기존 Run 재조회 | ✅ 동일 Run ID 확인 |
| Artifact 재다운로드 | ✅ 성공 |

### Phase 8: GCS Artifact E2E ✅

**환경**:
- GCP Project: `ar-infra-501607`
- Bucket: `gs://ar-infra-501607-autoresearch-mlflow-artifacts`
- MLflow: `ghcr.io/mlflow/mlflow:v2.22.1`
- Backend Store: PostgreSQL
- 인증: 로컬 ADC (Application Default Credentials)
- Artifact 방식: MLflow Artifact Proxy Mode

**문제 해결**:
- 초기 오류: Project ID를 추론하지 못함
- 해결: `GOOGLE_CLOUD_PROJECT=${GCP_PROJECT_ID}` 환경변수 추가

**검증 결과**:

| 항목 | 결과 |
|---|---|
| Bucket 접근 | ✅ |
| Object 업로드 | ✅ |
| Object 다운로드 | ✅ |
| 내용 비교 | ✅ |
| MLflow Smoke Test | ✅ |
| Run Status | FINISHED ✅ |
| GCS Object 생성 | ✅ |

**생성된 GCS Object**:
```
gs://ar-infra-501607-autoresearch-mlflow-artifacts/mlflow-artifacts/7/bafd490a32b4476a978cbd6bd3733a16/artifacts/smoke-artifact.txt
```

#### 결론

**공식 MLflow 이미지 v2.22.1에는 PostgreSQL, MinIO/S3, GCS 연동에 필요한 모든 패키지가 포함되지 않음.**

→ 최소 파생 이미지 사용이 필요

---

## 14. Decision (최종 확정)

### MLflow 버전

**✅ 확정됨**: v2.22.1 (프로젝트 고정 버전)

### 공식 이미지 의존성

**✅ 검증 완료**:
- 공식 이미지 v2.22.1 (Digest: `sha256:752d6d7e9fae6c321a67632fdc835b42b2d39f6dd6d684e483f4de0772743e81`)
- 필요한 3가지 패키지 미포함:
  - `psycopg2-binary` (PostgreSQL)
  - `boto3` (MinIO/S3)
  - `google-cloud-storage` (GCS)

### 이미지 선택

**✅ 최소 파생 이미지 필수**

**근거**:
1. 공식 이미지는 필수 Runtime 패키지 3가지 미포함 (Phase 3)
2. Compose 시작 시 설치로 보완 가능함 확인 (Phase 4)
3. Local/MinIO/GCS 모두 E2E 검증 완료 (Phase 5-8)
4. 현재 Compose는 임시 방식 (프로덕션 부적합)
5. 최종 Linux 배포는 Build-time 설치 필요

**최소 파생 이미지 구성**:

```text
deploy/mlflow/Dockerfile
deploy/mlflow/runtime/pyproject.toml
deploy/mlflow/runtime/uv.lock
```

Dockerfile은 루트 프로젝트 manifest가 아니라 MLflow 서버 전용 runtime manifest와 lockfile을 복사한다. Build 단계에서는 `uv export --frozen --no-dev --no-hashes`로 requirements를 생성하고, 파일이 비어 있지 않은지 확인한 뒤 `uv pip install --system`으로 설치한다.

**Runtime manifest**:

```toml
[project]
name = "autoresearch-mlflow-runtime"
version = "0.1.0"
requires-python = ">=3.10,<3.11"
dependencies = [
    "psycopg2-binary==2.9.10",
    "boto3==1.43.47",
    "google-cloud-storage==2.19.0",
    "protobuf<7",
]

[tool.uv]
package = false
```

루트 프로젝트는 Python `>=3.11,<3.14` 범위의 학습/애플리케이션 환경을 관리하고, MLflow 서버 이미지는 Python 3.10 공식 이미지 위에서 별도 manifest/lockfile로 관리한다. 두 환경을 같은 `uv.lock`에 묶지 않는다.

### Runtime 의존성 최종 결정

| 패키지 | 제약 조건 | 검증 버전 | 설치 방식 | 용도 |
|---|---|---|---|---|
| `psycopg2-binary` | ==2.9.10 | 2.9.10 | UV Build-time | PostgreSQL Backend |
| `boto3` | ==1.43.47 | 1.43.47 | UV Build-time | MinIO/S3 Artifact |
| `google-cloud-storage` | ==2.19.0 | 2.19.0 | UV Build-time | GCS Artifact |
| `protobuf` | <7 | 6.33.6 | UV Build-time | MLflow 2.22.1 API 호환성 |

`protobuf<7`은 MLflow 2.22.1 호환성 제약이다. 최초 runtime lock에서 `protobuf==7.35.1`이 선택되자 MLflow API 요청 중 `FieldDescriptor.label` 오류가 발생했다. `protobuf<7` 추가 후 `protobuf==6.33.6`으로 고정했고 Local/MinIO/GCS 검증에서 정상 동작을 확인했다.

### 컨테이너 시작 시 pip install 제거

**최종 상태**:

Compose command에는 `mlflow server` 실행만 남기고 시작 시 `pip install`을 제거했다. 패키지는 `autoresearch-mlflow-server:2.22.1-runtime-v1` 이미지 build-time에 설치된다.

검증 결과 `deploy/mlflow` 하위 Compose YAML에서 `pip install` 문자열은 0건이다. Pod 시작 시간 단축, 네트워크 장애 영향 제거, 버전 재현성을 확보한다.

### Base 이미지와 최종 이미지 Registry 구분

**Base Image**:
```text
ghcr.io/mlflow/mlflow:v2.22.1
Python: 3.10.18
Digest: sha256:752d6d7e9fae6c321a67632fdc835b42b2d39f6dd6d684e483f4de0772743e81
```

**Final Derived Image**:
```text
Local tag: autoresearch-mlflow-server:2.22.1-runtime-v1
Build 도구: UV
Dependency source: deploy/mlflow/runtime/pyproject.toml + deploy/mlflow/runtime/uv.lock
```

### Python Runtime 결정

**✅ 확정됨**:

MLflow Tracking Server는 공식 MLflow v2.22.1 이미지가 제공하는 **Python 3.10.18** Runtime을 사용한다.

학습 파이프라인의 Python 3.12 환경과 MLflow 서버 환경은 **독립된 컨테이너**이며, MLflow API를 통해 통신하므로 Python 버전을 동일하게 맞추지 않는다.

**현재 #94에서는 공식 이미지의 검증된 실행 환경을 유지**하고, 필요한 PostgreSQL, S3 및 GCS Runtime 패키지만 추가한다.

**Risk**:
Python 3.10의 지원 종료가 2026년 10월로 예정되어 있다.

**Mitigation**:
전체 파이프라인 안정화 후 MLflow 및 Python Runtime 업그레이드를 별도 후속 과제로 관리한다.

### Kubernetes 배포 이미지

**최종 결정**:
- 최소 파생 이미지 사용
- Backend Store: PostgreSQL (Secret)
- Artifact Proxy Mode: GCS (Workload Identity)
- 환경변수 필수:
  - `MLFLOW_BACKEND_STORE_URI`: PostgreSQL 연결 문자열
  - `MLFLOW_ARTIFACT_DESTINATION`: GCS 경로
  - `GOOGLE_CLOUD_PROJECT`: GCP Project ID (선택사항, ADC 추론 가능)

### #94 현재 상태

**✅ Phase 9 이미지 전략 결정: 완료**
- MLflow v2.22.1 고정
- Python 3.10.18 Runtime 허용
- 공식 이미지 기반 최소 파생 이미지 선택
- UV 기반 Build-time 설치
- Runtime 패키지 제약 조건 확정

**✅ Phase 10 최소 파생 이미지 구현 및 검증: 완료**
- Dockerfile 작성 완료
- MLflow runtime 전용 manifest/lockfile 생성 완료
- 이미지 Build 완료: `autoresearch-mlflow-server:2.22.1-runtime-v1`
- Local Artifact E2E 완료
- MinIO Artifact E2E 완료
- GCS Artifact Proxy E2E 완료
- PostgreSQL Run 영속성 확인 완료
- MinIO Object 영속성 확인 완료
- Compose 시작 시 `pip install` 제거 완료

**❌ 제외 항목** (후속 과제):
- Kubernetes 배포 설정 (#95)
- Workload Identity (#95)
- GCS IAM 권한 (#95)
- Python 3.10 Runtime 업그레이드 (별도 후속 과제)
- MLflow 3.x 업그레이드 (별도 후속 과제)

---

## 14. 참고 자료

| 항목 | 참고 | 상태 |
|---|---|---|
| 로컬 MLflow 검증 | #131 (2b65d1c) | ✅ 완료 |
| MinIO 통합 | #136 (d5c73a4) | ✅ 완료 |
| 학습 파이프라인 연동 | #93 (bd5765a) | ✅ 완료 |
| Kubernetes 배포 | #95 | 예정 |

---

## 15. 검증 일정

| Phase | 작업 | 상태 |
|---|---|---|
| 0 | Smoke Test 인터페이스 확인 | ✅ 완료 |
| 1 | Spec 초안 작성 | ✅ 완료 |
| 2 | #94 이슈 수정 | ✅ 완료 |
| 3 | 공식 이미지 의존성 검증 | ✅ 완료 |
| 4 | Compose Runtime 검증 | ✅ 완료 |
| 5 | Local Artifact E2E | ✅ 완료 |
| 6 | MinIO Artifact E2E | ✅ 완료 |
| 7 | 영속성 검증 (down→up) | ✅ 완료 |
| 8 | GCS Artifact E2E | ✅ 완료 |
| 9 | 최종 이미지 결정 | ✅ 완료 |
| 10 | Decision 및 Spec 최종화 | ✅ 완료 |

---

## 16. 다음 단계

### #94 마무리

최소 파생 이미지 구현과 Local/MinIO/GCS 검증은 완료됐다. 남은 작업은 관련 파일만 커밋하고 원격 브랜치와 동기화한 뒤 push하는 것이다.

### #95 (Kubernetes 배포)

이어서 진행:

1. **Workload Identity 설정**
   - ServiceAccount 생성
   - GCS IAM 권한

2. **Kubernetes Deployment 작성**
   - Pod 사양
   - Volume/Secret 마운트
   - 환경변수 설정

3. **통합 테스트**
   - Pod에서 GCS Artifact 업로드
   - Run 조회 검증
