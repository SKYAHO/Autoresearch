# syntax=docker/dockerfile:1

# 실제 사용 중인 UV 버전으로 고정한다.
ARG UV_VERSION=0.11.28

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ghcr.io/mlflow/mlflow:v2.22.1

COPY --from=uv /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=0
ENV UV_SYSTEM_PYTHON=1

WORKDIR /opt/mlflow-runtime

COPY pyproject.toml uv.lock ./

RUN uv export \
      --frozen \
      --only-group mlflow-server \
      --no-hashes \
      --no-header \
      --output-file /tmp/mlflow-server-requirements.txt \
    && echo "===== exported requirements =====" \
    && cat /tmp/mlflow-server-requirements.txt \
    && echo "===== installing requirements =====" \
    && uv pip install \
      --system \
      --requirement /tmp/mlflow-server-requirements.txt \
    && echo "===== verifying imports =====" \
    && python -c "import psycopg2, boto3, google.cloud.storage; print('psycopg2=', psycopg2.__version__); print('boto3=', boto3.__version__); print('google-cloud-storage=', google.cloud.storage.__version__)" \
    && rm -f /tmp/mlflow-server-requirements.txt

EXPOSE 5000
