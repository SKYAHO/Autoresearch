FROM python:3.12-slim

WORKDIR /app

COPY persona-agents/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY persona-agents/common.py persona-agents/ingest_personas.py \
     persona-agents/simulate_behavior.py persona-agents/api.py ./

# 기본은 인터랙티브 API 서비스. 배치 Job 은 배포 시 command 를 override 한다.
ENV PORT=8080
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT}"]
