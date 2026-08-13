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
# 오류 분류(github_issues.py)가 버전 변경으로 조용히 깨진다. `ARG`가 아니라 `ENV`로
# 두어 `--build-arg`로 override할 수 없게 한다 — override 가능하면 CI가 검증하는
# 버전(ci.yml에 하드코딩된 2.97.0)과 실제 빌드 결과가 조용히 갈릴 수 있다.
ENV GH_VERSION=2.97.0
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

COPY applications/__init__.py ./applications/
COPY applications/experiment_platform/__init__.py ./applications/experiment_platform/
COPY applications/experiment_platform/api ./applications/experiment_platform/api
COPY applications/experiment_platform/shared/__init__.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/contracts.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/bootstrap_secrets.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/github_app.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/github_refs.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/entrypoint.sh ./applications/experiment_platform/
# 실험 워크벤치 스키마 migration 실행용. entrypoint.sh는 이 파일을 실행하지 않는다 —
# 이 이미지를 대상으로 `alembic -c applications/experiment_platform/alembic.ini upgrade head`를
# API 기동 전에 실행하는 것은 배포 오케스트레이션(K8s Job/initContainer) 쪽 책임이다.
COPY applications/experiment_platform/alembic.ini ./applications/experiment_platform/
COPY applications/experiment_platform/migrations ./applications/experiment_platform/migrations
RUN chmod 0555 ./applications/experiment_platform/entrypoint.sh

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="${VCS_REF}"

USER appuser

EXPOSE 8000

CMD ["./applications/experiment_platform/entrypoint.sh"]
