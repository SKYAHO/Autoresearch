FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export

WORKDIR /source

COPY pyproject.toml uv.lock ./
RUN ["/uv", "export", "--frozen", "--no-dev", "--group", "feast", "--no-hashes", "--output-file", "/requirements.lock"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY --from=lock-export /requirements.lock ./
# uv.lock이 고정한 전체 전이 의존성을 pip 재해석 없이 그대로 설치한다.
RUN python -m pip install --no-cache-dir --no-deps -r requirements.lock && rm requirements.lock

COPY autoresearch ./autoresearch
COPY feature_repo ./feature_repo
COPY src ./src

USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.serving.app:app", "--host", "0.0.0.0", "--port", "8000"]
