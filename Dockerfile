FROM quay.io/astronomer/astro-runtime:13.8.0

# Airflow 컨테이너에서 동작하는 수집 로직(pyarrow, google-api-python-client,
# google-cloud-storage, gcsfs 등)에 필요한 의존성을 이미지에 설치한다.
# requirements.txt 는 CI app 이미지(Dockerfile.app) 와 공유하는 단일 출처.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# autoresearch 패키지를 /usr/local/airflow/autoresearch 에 배치한다.
# astro-runtime 의 WORKDIR/AIRFLOW_HOME 은 /usr/local/airflow 이고,
# astro dev 는 ./dags -> /usr/local/airflow/dags 로 마운트하므로
# dags/*.py 의 sys.path hack(parents[1] == /usr/local/airflow) 로
# autoresearch.* import 가 해결된다.
COPY autoresearch ./autoresearch
