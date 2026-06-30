"""Cloud Run Job 엔트리포인트.

매일 한국(KR) YouTube 인기 급상승 트렌딩을 수집해서
  1) GCS 에 일자별 parquet 저장
       gs://$GCS_BUCKET/$GCS_PREFIX/year=YYYY/month=MM/youtube_trending_KR_YYYY-MM-DD.parquet
  2) BigQuery 테이블에 append (같은 날짜 KR 데이터는 먼저 삭제 후 적재 = 재실행 안전)
       $BQ_TABLE  (예: my-proj.youtube.trending)  / video_trending__date 로 일자 파티션

환경변수
  GCS_BUCKET              (필수) 결과 parquet 저장 버킷명
  BQ_TABLE                (필수) project.dataset.table
  YOUTUBE_API_KEY         API 키 직접 지정(개발용)  또는
  YOUTUBE_API_KEY_SECRET  Secret Manager 버전 리소스명
                          (예: projects/123/secrets/youtube-api-key/versions/latest)
  REGION_CODE             기본 KR
  MAX_RESULTS             기본 200
  GCS_PREFIX              기본 youtube_trending
  BQ_LOCATION             기본 asia-northeast3
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from google.cloud import bigquery, storage

import fetch_trending_dataset as ft

KST = ZoneInfo("Asia/Seoul")
REGION_CODE = ft.normalize_region(os.getenv("REGION_CODE", "KR"))
MAX_RESULTS = ft.validate_max_results(int(os.getenv("MAX_RESULTS", "200")))
GCS_BUCKET = os.environ["GCS_BUCKET"]
GCS_PREFIX = os.getenv("GCS_PREFIX", "youtube_trending")
BQ_TABLE = os.environ["BQ_TABLE"]
BQ_LOCATION = os.getenv("BQ_LOCATION", "asia-northeast3")

# BigQuery 스키마 (fetch_trending_dataset 의 타입과 매핑)
BQ_SCHEMA = [
    bigquery.SchemaField("video_id", "STRING"),
    bigquery.SchemaField("video_published_at", "TIMESTAMP"),
    bigquery.SchemaField("video_trending__date", "DATE"),
    bigquery.SchemaField("video_trending_country", "STRING"),
    bigquery.SchemaField("channel_id", "STRING"),
    bigquery.SchemaField("video_title", "STRING"),
    bigquery.SchemaField("video_description", "STRING"),
    bigquery.SchemaField("video_default_thumbnail", "STRING"),
    bigquery.SchemaField("video_category_id", "STRING"),
    bigquery.SchemaField("video_tags", "STRING"),
    bigquery.SchemaField("video_duration", "STRING"),
    bigquery.SchemaField("video_dimension", "STRING"),
    bigquery.SchemaField("video_definition", "STRING"),
    bigquery.SchemaField("video_licensed_content", "BOOLEAN"),
    bigquery.SchemaField("video_view_count", "INTEGER"),
    bigquery.SchemaField("video_like_count", "INTEGER"),
    bigquery.SchemaField("video_comment_count", "INTEGER"),
    bigquery.SchemaField("channel_title", "STRING"),
    bigquery.SchemaField("channel_description", "STRING"),
    bigquery.SchemaField("channel_custom_url", "STRING"),
    bigquery.SchemaField("channel_published_at", "TIMESTAMP"),
    bigquery.SchemaField("channel_country", "STRING"),
    bigquery.SchemaField("channel_view_count", "INTEGER"),
    bigquery.SchemaField("channel_subscriber_count", "INTEGER"),
    bigquery.SchemaField("channel_have_hidden_subscribers", "BOOLEAN"),
    bigquery.SchemaField("channel_video_count", "INTEGER"),
    bigquery.SchemaField("channel_localized_title", "STRING"),
    bigquery.SchemaField("channel_localized_description", "STRING"),
]


def get_api_key() -> str:
    key = os.getenv("YOUTUBE_API_KEY")
    if key:
        return key
    secret = os.getenv("YOUTUBE_API_KEY_SECRET")
    if secret:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        resp = client.access_secret_version(name=secret)
        return resp.payload.data.decode("utf-8").strip()
    raise SystemExit(
        "오류: YOUTUBE_API_KEY 또는 YOUTUBE_API_KEY_SECRET 환경변수가 필요합니다."
    )


def upload_to_gcs(local_path: Path, now: dt.datetime, iso_date: str) -> str:
    blob_path = (
        f"{GCS_PREFIX}/year={now:%Y}/month={now:%m}/"
        f"youtube_trending_{REGION_CODE}_{iso_date}.parquet"
    )
    bucket = storage.Client().bucket(GCS_BUCKET)
    bucket.blob(blob_path).upload_from_filename(str(local_path))
    return f"gs://{GCS_BUCKET}/{blob_path}"


def ensure_table(bq: bigquery.Client) -> None:
    from google.api_core.exceptions import NotFound

    try:
        bq.get_table(BQ_TABLE)
    except NotFound:
        table = bigquery.Table(BQ_TABLE, schema=BQ_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field="video_trending__date"
        )
        bq.create_table(table)
        print(f"BigQuery 테이블 생성: {BQ_TABLE} (일자 파티션)")


def append_to_bq(df: pd.DataFrame, country: str, iso_date: str) -> None:
    bq = bigquery.Client(location=BQ_LOCATION)
    ensure_table(bq)

    # 같은 날짜·국가 기존 행 삭제(재실행 시 중복 방지)
    bq.query(
        f"DELETE FROM `{BQ_TABLE}` "
        f"WHERE video_trending__date = DATE(@d) AND video_trending_country = @c",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("d", "DATE", iso_date),
                bigquery.ScalarQueryParameter("c", "STRING", country),
            ]
        ),
    ).result()

    # DATE 컬럼은 date 객체로 변환해서 적재
    bq_df = df.copy()
    bq_df["video_trending__date"] = pd.to_datetime(
        bq_df["video_trending__date"]
    ).dt.date

    job = bq.load_table_from_dataframe(
        bq_df,
        BQ_TABLE,
        job_config=bigquery.LoadJobConfig(
            schema=BQ_SCHEMA, write_disposition="WRITE_APPEND"
        ),
    )
    job.result()


def main() -> None:
    key = get_api_key()
    now = dt.datetime.now(KST)
    dot_date = now.strftime("%Y.%m.%d")
    iso_date = now.strftime("%Y-%m-%d")
    country = ft.REGION_NAMES.get(REGION_CODE, REGION_CODE)

    print(f"[{iso_date} KST] {REGION_CODE} 트렌딩 수집 (max {MAX_RESULTS})")
    rows = ft.build_rows(key, REGION_CODE, dot_date, MAX_RESULTS)
    if not rows:
        print("수집된 행이 없습니다. 종료.")
        return
    df = ft.to_dataframe(rows)  # 타입 지정된 DF

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / f"youtube_trending_{REGION_CODE}_{iso_date}.parquet"
        df.to_parquet(local, index=False)
        gcs_uri = upload_to_gcs(local, now, iso_date)
    print(f"  GCS 저장: {gcs_uri}  ({len(df)} rows)")

    append_to_bq(df, country, iso_date)
    print(f"  BigQuery append 완료: {BQ_TABLE}  (+{len(df)} rows, 날짜 {iso_date})")


if __name__ == "__main__":
    main()
