"""virtual_user parquet + KR 영상 parquet에서 대규모 action log를 생성한다.

action_logs 파이프라인의 **청킹(chunk_size) + 병렬(max_concurrency)** 를 사용해 유저당
후보를 작은 청크로 쪼개 독립 LLM 콜로 처리한다(약한 모델의 truncation·격리 방지, 대량
throughput). 기존 로그에 이어붙일 때 event_id 충돌을 막도록 --event-offset을 지원한다.

전제:
  - OPENROUTER_API_KEY 환경변수 (mistral-nemo 등 OpenRouter 모델).
  - users parquet은 `user_id` 컬럼 보유(virtual_users 산출물).

사용 예:
  # 신규 유저(vu_1101+)만 생성해 기존 로그에 이어붙이기
  OPENROUTER_API_KEY=... python scripts/generate_action_logs_scale.py \
      --users asset/virtual_user/vu_1000.parquet --min-user-index 1100 \
      --videos data/raw/youtube/kr_trending_2000.parquet \
      --candidates 96 --chunk 24 --concurrency 60 \
      --out asset/action_log/event_log_batch2.parquet --event-offset 100000
"""
import argparse
import time
from collections import Counter

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.action_logs.llm_generator import OpenRouterActionLogGenerator
from autoresearch.action_logs.pipeline import generate_action_log_batch
from autoresearch.action_logs.schema import EventGenerationRequest
from autoresearch.action_logs.video_source import load_video_records


def _user_index(user_id: str) -> int:
    try:
        return int(user_id.split("_")[1])
    except (IndexError, ValueError):
        return -1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", required=True, help="virtual_user parquet (user_id 컬럼)")
    ap.add_argument("--videos", required=True, help="KR 영상 parquet")
    ap.add_argument("--out", required=True, help="출력 event log parquet")
    ap.add_argument("--min-user-index", type=int, default=0,
                    help="이 인덱스 초과 user_id만 생성(신규 유저만 처리할 때)")
    ap.add_argument("--candidates", type=int, default=96)
    ap.add_argument("--chunk", type=int, default=24)
    ap.add_argument("--concurrency", type=int, default=60)
    ap.add_argument("--target-ctr", type=float, default=0.02)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--model", default="mistralai/mistral-nemo")
    ap.add_argument("--event-offset", type=int, default=0,
                    help="event_id 숫자에 더할 offset(기존 로그와 병합 시 충돌 방지)")
    args = ap.parse_args()

    users = [u for u in pq.read_table(args.users).to_pylist()
             if _user_index(u["user_id"]) > args.min_user_index]
    videos = load_video_records(args.videos)
    print(f"users={len(users)}, videos={len(videos)}, "
          f"콜≈{len(users) * max(1, args.candidates // args.chunk)}", flush=True)

    request = EventGenerationRequest(
        candidates_per_user=args.candidates, chunk_size=args.chunk,
        max_concurrency=args.concurrency, target_ctr=args.target_ctr, seed=args.seed,
        output_path=args.out,
        warehouse_output_path=args.out.replace(".parquet", ".jsonl"),
        quarantine_output_path=args.out.replace(".parquet", "_quarantine.jsonl"),
    )
    gen = OpenRouterActionLogGenerator(model_name=args.model)
    t0 = time.time()
    result = generate_action_log_batch(request, users, videos, gen)
    print(f"생성 완료 ({(time.time() - t0) / 60:.1f}분): {result.summary}", flush=True)

    if args.event_offset:
        table = pq.read_table(args.out)
        eids = [f"evt_{int(e.split('_')[1]) + args.event_offset:08d}"
                for e in table.column("event_id").to_pylist()]
        table = table.set_column(
            table.schema.get_field_index("event_id"), "event_id", pa.array(eids, pa.string()))
        pq.write_table(table, args.out)
        print(f"event_id +{args.event_offset} offset 적용", flush=True)

    cnt = Counter(e.event_type for e in result.batch.events)
    print(f"event_type: {dict(cnt)} | CTR: "
          f"{cnt['click'] / max(1, cnt['impression']) * 100:.2f}%", flush=True)


if __name__ == "__main__":
    main()
