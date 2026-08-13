FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export

WORKDIR /source

COPY pyproject.toml uv.lock ./
RUN ["/uv", "export", "--frozen", "--only-group", "orchestration", "--no-hashes", "--output-file", "/requirements.lock"]

FROM node:22.16.0-slim AS codex-cli

RUN npm install --global @openai/codex@0.146.0 \
    && codex --version

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEX_HOME=/var/lib/codex \
    TMPDIR=/tmp

WORKDIR /app

RUN addgroup --gid 10001 appuser \
    && adduser --uid 10001 --gid 10001 --disabled-password --gecos "" --no-create-home appuser \
    && mkdir --parents /var/lib/codex \
    && chown appuser:appuser /var/lib/codex

COPY --from=lock-export /requirements.lock ./
# uv.lock의 고정된 orchestration 런타임 전이 의존성만 설치한다.
RUN python -m pip install --no-cache-dir --no-deps -r requirements.lock \
    && rm requirements.lock

# Codex CLI와 이에 필요한 Node 런타임만 Runner 이미지에 반입한다.
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

COPY applications/__init__.py ./applications/
COPY applications/experiment_platform/__init__.py ./applications/experiment_platform/
COPY applications/experiment_platform/shared/__init__.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/contracts.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/codex.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/runner ./applications/experiment_platform/runner
COPY applications/experiment_platform/runner_entrypoint.sh ./applications/experiment_platform/
RUN chmod 0555 ./applications/experiment_platform/runner_entrypoint.sh

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="${VCS_REF}"

USER appuser

EXPOSE 8080

CMD ["./applications/experiment_platform/runner_entrypoint.sh"]
