# syntax=docker/dockerfile:1

ARG UV_VERSION=0.10.0

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ghcr.io/mlflow/mlflow:v2.22.1

COPY --from=uv /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=0
ENV UV_SYSTEM_PYTHON=1

WORKDIR /opt/mlflow-runtime

COPY deploy/mlflow/runtime/pyproject.toml ./pyproject.toml
COPY deploy/mlflow/runtime/uv.lock ./uv.lock

RUN uv export \
      --frozen \
      --no-dev \
      --no-hashes \
      --output-file /tmp/mlflow-runtime-requirements.txt \
    && test -s /tmp/mlflow-runtime-requirements.txt \
    && uv pip install \
      --system \
      --requirement /tmp/mlflow-runtime-requirements.txt \
    && python -c "import psycopg2; import boto3; import google.cloud.storage; print('psycopg2:', psycopg2.__version__); print('boto3:', boto3.__version__); print('google-cloud-storage:', google.cloud.storage.__version__)" \
    && rm -f /tmp/mlflow-runtime-requirements.txt

EXPOSE 5000
