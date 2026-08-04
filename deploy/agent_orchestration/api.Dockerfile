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

# gh CLI — 이슈 발행에 사용한다(#516). 버전을 고정하지 않으면 stderr 문자열 기반
# 오류 분류(github_issues.py)가 버전 변경으로 조용히 깨진다.
ARG GH_VERSION=2.97.0
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz"; \
    tar -xzf /tmp/gh.tar.gz -C /tmp; \
    install -m 0555 "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_${arch}"; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    gh --version

COPY agent_orchestration/__init__.py ./agent_orchestration/
COPY agent_orchestration/app ./agent_orchestration/app
COPY agent_orchestration/contracts.py ./agent_orchestration/
COPY agent_orchestration/bootstrap_secrets.py ./agent_orchestration/
COPY agent_orchestration/entrypoint.sh ./agent_orchestration/
# 실험 워크벤치 스키마 migration 실행용. entrypoint.sh는 이 파일을 실행하지 않는다 —
# 이 이미지를 대상으로 `alembic -c agent_orchestration/alembic.ini upgrade head`를
# API 기동 전에 실행하는 것은 배포 오케스트레이션(K8s Job/initContainer) 쪽 책임이다.
COPY agent_orchestration/alembic.ini ./agent_orchestration/
COPY agent_orchestration/migrations ./agent_orchestration/migrations
RUN chmod 0555 ./agent_orchestration/entrypoint.sh

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="${VCS_REF}"

USER appuser

EXPOSE 8000

CMD ["./agent_orchestration/entrypoint.sh"]
