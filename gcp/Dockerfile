FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저 설치(레이어 캐시)
COPY gcp/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 공용 수집 로직 + 엔트리포인트
COPY fetch_trending_dataset.py ./fetch_trending_dataset.py
COPY gcp/main.py ./main.py

ENTRYPOINT ["python", "main.py"]
