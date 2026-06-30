"""Nemotron-Personas (ko_KR) 데이터셋을 받아 GCS 저장 + BigQuery 적재.

데이터셋 출처(둘 중 하나):
  - HuggingFace:  PERSONA_HF_REPO (예: nvidia/Nemotron-Personas-Korea)
  - 로컬 parquet: PERSONA_LOCAL_PARQUET 경로

환경변수
  GCP_PROJECT            (필수)
  GCS_BUCKET             (필수) 원본 보관 버킷
  BQ_TABLE               (필수) project.dataset.personas
  PERSONA_HF_REPO        HuggingFace 데이터셋 repo id (NGC/HF 페이지에서 확인)
  PERSONA_HF_TOKEN       (선택) 접근 토큰
  PERSONA_LOCAL_PARQUET  (선택) HF 대신 로컬 parquet 사용
  BQ_LOCATION            기본 asia-northeast3
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd
from google.cloud import bigquery, storage

GCS_BUCKET = os.environ["GCS_BUCKET"]
BQ_TABLE = os.environ["BQ_TABLE"]
BQ_LOCATION = os.getenv("BQ_LOCATION", "asia-northeast3")
HF_REPO = os.getenv("PERSONA_HF_REPO", "")
HF_TOKEN = os.getenv("PERSONA_HF_TOKEN") or None
LOCAL_PARQUET = os.getenv("PERSONA_LOCAL_PARQUET", "")


def load_personas() -> pd.DataFrame:
    if LOCAL_PARQUET:
        print(f"로컬 parquet 로드: {LOCAL_PARQUET}")
        return pd.read_parquet(LOCAL_PARQUET)
    if not HF_REPO:
        raise SystemExit(
            "오류: PERSONA_HF_REPO 또는 PERSONA_LOCAL_PARQUET 중 하나가 필요합니다.\n"
            "  NGC/HuggingFace 의 ko_KR 데이터셋 repo id 를 PERSONA_HF_REPO 로 지정하세요."
        )
    from datasets import load_dataset

    print(f"HuggingFace 데이터셋 로드: {HF_REPO}")
    ds = load_dataset(HF_REPO, split="train", token=HF_TOKEN)
    return ds.to_pandas()


def main() -> None:
    df = load_personas()
    # 모든 컬럼 문자열화(스키마 안정) — 필요한 분석 컬럼은 이후 캐스팅
    df = df.astype("string")
    print(f"페르소나 {len(df):,}행 · 컬럼 {len(df.columns)}개")

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "personas_ko_kr.parquet"
        df.to_parquet(local, index=False)
        blob = "nemotron_personas/ko_kr/personas.parquet"
        storage.Client().bucket(GCS_BUCKET).blob(blob).upload_from_filename(str(local))
        print(f"GCS 저장: gs://{GCS_BUCKET}/{blob}")

    bq = bigquery.Client(location=BQ_LOCATION)
    job = bq.load_table_from_dataframe(
        df, BQ_TABLE,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE", autodetect=True
        ),
    )
    job.result()
    print(f"BigQuery 적재 완료: {BQ_TABLE} ({len(df):,} rows)")


if __name__ == "__main__":
    main()
