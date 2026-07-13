FROM python:3.12-slim

ARG VCS_REF=unknown

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV AUTORESEARCH_REVISION=${VCS_REF}

LABEL org.opencontainers.image.source="https://github.com/SKYAHO/Autoresearch" \
      org.opencontainers.image.revision="${VCS_REF}" \
      io.autoresearch.batch-contract.version="batch-contract-v1"

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY autoresearch ./autoresearch

USER appuser

CMD ["python", "-c", "import autoresearch; print('autoresearch image ready')"]
