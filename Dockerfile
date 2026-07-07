FROM quay.io/astronomer/astro-runtime:13.8.0

# Airflow 컨테이너에서 동작하는 수집 로직(pyarrow, google-api-python-client,
# google-cloud-storage, gcsfs 등)에 필요한 의존성을 이미지에 설치한다.
# requirements.txt 는 pyproject.toml [project].dependencies 의 범위 미러이며
# (전핀 export 를 쓰지 않는 이유는 requirements.txt 헤더 참조) CI app 이미지
# (Dockerfile.app)와 공유한다. 베이스 이미지가 이미 만족하는 범위는 재설치되지
# 않는다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# autoresearch 패키지(src 레이아웃)를 site-packages 에 정식 설치한다.
# --no-deps: 의존성은 위 requirements.txt 미러로 이미 설치되어 있다 — Astro
# 베이스 제약 존중 계약 유지. dags/*.py 는 sys.path 조작 없이 설치된 패키지를
# 그대로 import 한다.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
