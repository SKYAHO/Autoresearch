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

COPY agent_orchestration/__init__.py ./agent_orchestration/
COPY agent_orchestration/launcher ./agent_orchestration/launcher
# launcher repository가 기존 Experiment 상태 전이 계약을 재사용하므로 API 도메인
# 모듈과 그 GitHub 경계만 포함한다. Codex CLI·Node 런타임은 설치하지 않는다.
COPY agent_orchestration/app ./agent_orchestration/app
COPY agent_orchestration/github_app.py ./agent_orchestration/
COPY agent_orchestration/github_refs.py ./agent_orchestration/

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="${VCS_REF}"

USER appuser

CMD ["python", "-m", "agent_orchestration.launcher.main"]
