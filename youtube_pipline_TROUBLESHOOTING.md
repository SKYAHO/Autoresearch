# 트러블슈팅

파이프라인 구축·배포 중 발생한 오류와 해결 방법 정리.

---

## 1. `pip install requirements.txt` 실패

**증상**
```
ERROR: Could not find a version that satisfies the requirement requirements.txt
HINT: ... use the '-r' flag to install the packages listed in requirements.txt
```

**원인**
`-r` 플래그 없이 실행하면 pip이 "requirements.txt"라는 이름의 패키지를 찾으려다 실패한다.

**해결**
```bash
pip install -r requirements.txt
```

---

## 2. run_daily.py — 마스터 parquet 쓰기 권한 오류

**증상**
```
PermissionError: [Errno 13] Permission denied: 'data/youtube_trending_videos_global.parquet'
```
(일자 파일은 정상 저장되는데 마스터 append 단계에서만 실패)

**원인**
기존 마스터 파일을 직접 열어 덮어쓰려 했는데, 그 파일의 쓰기 권한이 없었다.
`to_parquet(master)`는 대상 파일을 직접 `open(..., 'wb')` 하므로 파일 권한이 필요하다.

**해결**
임시 파일에 쓴 뒤 `os.replace()`로 원자적 교체. rename은 **폴더** 쓰기 권한만 있으면 되므로
대상 파일이 읽기전용이어도 동작한다.
```python
tmp = master.with_name(master.name + ".tmp")
combined.to_parquet(tmp, index=False)
os.replace(tmp, master)
```

---

## 3. `gcloud: command not found`

**증상**
```
gcp/deploy.sh: line 22: gcloud: command not found
```

**원인**
Google Cloud SDK(gcloud)가 설치되어 있지 않음.

**해결**
```bash
brew install --cask google-cloud-sdk          # 또는 공식 설치 스크립트
gcloud --version
gcloud auth login
gcloud auth application-default login
```

---

## 4. GCP 프로젝트 ID 형식 오류

**증상**
`PROJECT_ID="Autoresearch"`(대문자 포함)로 두면 이후 gcloud 명령이 실패한다.

**원인**
GCP 프로젝트 ID는 **소문자/숫자/하이픈**만 허용한다. GitHub 레포 이름과 혼동했다.

**해결**
실제 프로젝트 ID를 확인해서 사용.
```bash
gcloud projects list      # 예: autoresearch-501004
```
`gcp/deploy.sh`의 `PROJECT_ID=`를 실제 값으로 수정.

---

## 5. ADC quota project 경고

**증상**
```
WARNING: Your active project does not match the quota project in your local
Application Default Credentials file.
```

**원인**
경고일 뿐 치명적이지 않다. ADC에 기록된 quota project가 현재 프로젝트와 다를 때 표시.

**해결(선택)**
```bash
gcloud auth application-default set-quota-project autoresearch-501004
```

---

## 6. `Secret Payload cannot be empty`

**증상**
```
ERROR: (gcloud.secrets.versions.add) INVALID_ARGUMENT: Secret Payload cannot be empty.
```

**원인**
`YOUTUBE_API_KEY`가 비어 있는 채로 Secret Manager에 버전을 추가하려 했다.
(키는 커밋되는 deploy.sh에 직접 적으면 안 되므로 비워 둔 상태였음)

**해결**
- 키를 `.env`에서 읽거나 환경변수로 주입하도록 변경.
- 키가 비면 시크릿 단계를 건너뛰고 경고만 출력(스크립트 중단 방지).
```bash
# 환경변수 주입 방식
YOUTUBE_API_KEY="발급받은_키" bash gcp/deploy.sh
# 또는 .env 에 YOUTUBE_API_KEY=... 를 두면 자동 로드
```

---

## 7. `Service account ... does not exist` (IAM 바인딩 실패)

**증상**
```
ERROR: (gcloud.storage.buckets.add-iam-policy-binding) HTTPError 400:
Service account yt-job-sa@<proj>.iam.gserviceaccount.com does not exist.
```

**원인**
서비스 계정 생성 직후 전파(propagation) 지연으로, 곧바로 IAM 바인딩을 걸면 아직 "없는 것"으로 보인다.

**해결**
생성 후 존재가 확인될 때까지 폴링 대기(최대 60초) 후 바인딩.
```bash
for SA in "$RUN_SA" "$SCHED_SA"; do
  for _ in $(seq 1 12); do
    gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 && break
    sleep 5
  done
done
```

---

## 참고: 배포 후 확인 명령

```bash
gcloud run jobs execute youtube-trending-daily --region=asia-northeast3
gcloud run jobs executions list --job=youtube-trending-daily --region=asia-northeast3
gcloud storage ls "gs://<버킷>/youtube_trending/**"
bq query --use_legacy_sql=false \
  'SELECT video_trending__date, COUNT(*) AS n FROM `<프로젝트>.youtube.trending` GROUP BY 1 ORDER BY 1 DESC'
```
