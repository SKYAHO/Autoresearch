FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export

WORKDIR /source

COPY pyproject.toml uv.lock ./
RUN ["/uv", "export", "--frozen", "--only-group", "orchestration", "--no-hashes", "--output-file", "/requirements.lock"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --gid 10001 appuser \
    && adduser --uid 10001 --gid 10001 --disabled-password --gecos "" --no-create-home appuser

COPY --from=lock-export /requirements.lock ./
# uv.lock의 고정된 orchestration 런타임 전이 의존성만 설치한다.
RUN python -m pip install --no-cache-dir --no-deps -r requirements.lock \
    && rm requirements.lock

COPY applications/__init__.py ./applications/
COPY applications/experiment_platform/__init__.py ./applications/experiment_platform/
COPY applications/experiment_platform/launcher ./applications/experiment_platform/launcher
# launcher repository가 기존 Experiment 상태 전이 계약을 재사용하므로 API 도메인
# 모듈과 그 GitHub 경계만 포함한다. Codex CLI·Node 런타임은 설치하지 않는다.
COPY applications/experiment_platform/api ./applications/experiment_platform/api
COPY applications/experiment_platform/shared/__init__.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/github_app.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/github_refs.py ./applications/experiment_platform/shared/
# PR 생성기(`launcher.pull_request`, #689)가 최상위에서 import한다. 이 줄이 없으면
# 이미지는 정상 빌드되고 launcher·log_collector도 멀쩡히 돌지만, PR 생성기 컨테이너만
# 기동 즉시 ModuleNotFoundError로 죽는다(#700). 최상위 모듈은 이 목록에 열거해야만
# 들어오므로, `applications/experiment_platform/`에 파일을 추가하는 것만으로는 부족하다.
COPY applications/experiment_platform/shared/github_pull_requests.py ./applications/experiment_platform/shared/

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="${VCS_REF}"

USER appuser

CMD ["python", "-m", "applications.experiment_platform.launcher.main"]
