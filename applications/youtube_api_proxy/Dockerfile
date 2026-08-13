FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY app.py .

USER appuser

EXPOSE 8080
# Cloud Run 은 $PORT env 로 리슨 포트를 주입(기본 8080). exec form 은 shell 확장을
# 하지 않으므로 sh -c 로 감싸 ${PORT:-8080} 을 읽는다.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
