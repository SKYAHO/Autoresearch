FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export

WORKDIR /source

COPY pyproject.toml uv.lock ./
RUN ["/uv", "export", "--frozen", "--only-group", "orchestration", "--only-group", "orchestration-ui", "--no-hashes", "--output-file", "/requirements.lock"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/tmp

WORKDIR /app

RUN addgroup --gid 10001 appuser \
    && adduser --uid 10001 --gid 10001 --disabled-password --gecos "" --no-create-home appuser

COPY --from=lock-export /requirements.lock ./
# UI는 API 상태 정본을 import하므로 orchestration과 orchestration-ui 의존성을 함께 고정한다.
RUN python -m pip install --no-cache-dir --no-deps -r requirements.lock \
    && rm requirements.lock

COPY agent_orchestration/__init__.py ./agent_orchestration/
# UI의 canonical status model은 SQLAlchemy Base를 import한다. API server·LLM·DB bootstrap
# source는 포함하지 않고 이 표시 모델 의존성만 복사한다.
COPY agent_orchestration/app/__init__.py ./agent_orchestration/app/
COPY agent_orchestration/app/database.py ./agent_orchestration/app/
COPY agent_orchestration/app/experiments/__init__.py ./agent_orchestration/app/experiments/
COPY agent_orchestration/app/experiments/models.py ./agent_orchestration/app/experiments/
COPY agent_orchestration/ui ./agent_orchestration/ui

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="${VCS_REF}"

USER appuser

EXPOSE 8501

CMD ["streamlit", "run", "agent_orchestration/ui/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false", "--server.fileWatcherType=none"]
