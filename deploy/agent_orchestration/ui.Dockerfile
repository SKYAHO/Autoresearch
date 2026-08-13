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

COPY applications/__init__.py ./applications/
COPY applications/experiment_platform/__init__.py ./applications/experiment_platform/
# UI의 canonical status model은 SQLAlchemy Base를 import한다. API server·LLM·DB bootstrap
# source는 포함하지 않고 이 표시 모델 의존성만 복사한다.
COPY applications/experiment_platform/api/__init__.py ./applications/experiment_platform/api/
COPY applications/experiment_platform/api/database.py ./applications/experiment_platform/api/
COPY applications/experiment_platform/api/experiments/__init__.py ./applications/experiment_platform/api/experiments/
COPY applications/experiment_platform/api/experiments/models.py ./applications/experiment_platform/api/experiments/
COPY applications/experiment_platform/workbench ./applications/experiment_platform/workbench
# 테마 정본. WORKDIR이 /app이라 streamlit이 /app/.streamlit/config.toml을 읽는다.
# 이 줄이 없으면 배포 이미지만 스톡 기본 테마로 돌아간다 — 로컬에서는 저장소 루트가
# CWD라 같은 파일이 잡히므로 차이가 드러나지 않는다.
COPY .streamlit ./.streamlit

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="${VCS_REF}"

USER appuser

EXPOSE 8501

CMD ["streamlit", "run", "applications/experiment_platform/workbench/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false", "--server.fileWatcherType=none"]
