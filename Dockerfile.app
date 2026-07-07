FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

# autoresearch 패키지(src 레이아웃)를 site-packages 에 정식 설치한다.
# --no-deps: 의존성은 위 requirements.txt 로 이미 설치되어 있다.
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps .

USER appuser

CMD ["python", "-c", "import autoresearch; print('autoresearch image ready')"]
