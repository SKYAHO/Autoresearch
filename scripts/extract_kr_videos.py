"""KR 트렌딩 영상을 backfill CSV(113개국, ~7.6GB)에서 추출해 parquet으로 저장한다.

DuckDB로 스트리밍 쿼리하므로 전체를 메모리에 올리지 않는다. video_id로 dedup(최신
snapshot 채택) 후 조회수 상위 N개를 뽑는다. action_logs.video_source.load_video_records가
읽을 수 있는 컬럼 스키마로 저장한다.

사용:
  python scripts/extract_kr_videos.py [--count 2000] [--country KR]
      [--csv data/raw/youtube/trending_yt_videos_113_countries.csv]
      [--out data/raw/youtube/kr_trending_2000.parquet]
"""
import argparse

import duckdb


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/raw/youtube/trending_yt_videos_113_countries.csv")
    ap.add_argument("--out", default="data/raw/youtube/kr_trending_2000.parquet")
    ap.add_argument("--country", default="KR")
    ap.add_argument("--count", type=int, default=2000)
    args = ap.parse_args()

    query = f"""
    COPY (
      SELECT video_id, title, description, video_tags,
             view_count, like_count, comment_count, channel_name, publish_date, snapshot_date
      FROM (
        SELECT *, row_number() OVER (PARTITION BY video_id ORDER BY snapshot_date DESC) AS rn
        FROM read_csv_auto('{args.csv}', ignore_errors=true)
        WHERE country = '{args.country}'
      ) WHERE rn = 1
      ORDER BY view_count DESC NULLS LAST
      LIMIT {args.count}
    ) TO '{args.out}' (FORMAT parquet)
    """
    duckdb.connect().execute(query)

    import pyarrow.parquet as pq

    table = pq.read_table(args.out)
    uniq = len(set(table.column("video_id").to_pylist()))
    print(f"추출 완료: {table.num_rows} rows (unique video_id: {uniq}) → {args.out}")


if __name__ == "__main__":
    main()
