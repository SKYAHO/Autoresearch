FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export

WORKDIR /source

COPY pyproject.toml uv.lock ./
# Phase 2 verifier는 source tree에서 Ruff·pytest를 실행한다. 기본 프로젝트와 dev
# group을 lockfile 그대로 export하되, 별도 환경인 feast group은 image에 넣지 않는다.
RUN ["/uv", "export", "--frozen", "--group", "dev", "--no-group", "feast", "--no-hashes", "--output-file", "/requirements.lock"]

FROM node:22.16.0-slim AS codex-cli

RUN npm install --global @openai/codex@0.146.0 \
    && codex --version

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/autoresearch-venv \
    PATH=/opt/autoresearch-venv/bin:${PATH}

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --gid 10001 appuser \
    && adduser --uid 10001 --gid 10001 --disabled-password --gecos "" --no-create-home appuser

COPY --from=lock-export /uv /usr/local/bin/uv
COPY --from=lock-export /requirements.lock ./
# lockfile에서 고정된 기본+dev 전이 의존성을 executor 전용 venv에 미리 설치한다.
RUN uv venv /opt/autoresearch-venv \
    && uv pip install --python /opt/autoresearch-venv/bin/python --no-deps -r requirements.lock \
    && rm requirements.lock

# Codex CLI와 필요한 Node runtime만 반입한다. 인증은 빌드 문맥에서 COPY하지 않고
# codex-worker의 runtime CODEX_HOME mount로만 제공한다.
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

COPY agent_orchestration/__init__.py ./agent_orchestration/
# 같은 image digest를 Stage 6의 branch creator, token minter, workspace preparer,
# Codex worker, verifier, finalizer가 command override로 공유한다.
COPY agent_orchestration/executor ./agent_orchestration/executor
COPY agent_orchestration/github_app.py ./agent_orchestration/
COPY agent_orchestration/github_refs.py ./agent_orchestration/
# workspace-preparer가 import하는 issue parser는 runtime clone이 아닌 image에 봉인한
# copy다. WORKDIR=/app 이므로 Python은 이 copy를 먼저 해석한다.
COPY tools/__init__.py ./tools/
COPY tools/auto_research_issue_branch.py ./tools/

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="${VCS_REF}"

USER appuser

CMD ["python", "-m", "agent_orchestration.executor.main"]
