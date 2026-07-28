"""feast 조립 경로 피크 메모리 벤치 (#359 Phase B).

``scripts/validate_feast_assembly.py``(정본 검증 경로)를 **자식 프로세스로 실행**하고,
부모가 그 프로세스의 RSS(상주 메모리)를 주기적으로 샘플링해 **피크 메모리**를 보고한다.
DuckDB 폴백 제거의 게이트("1.77M 전량이 배치 파드 메모리에 들어가나")를 실측하는 용도다.

왜 이 구조인가:
- **왜 validate를 감싸나** — 조립 로직(store apply·staged 조회·cold-start)을 복제하면 정의가
  드리프트한다. validate를 그대로 자식으로 돌려 정본 경로의 메모리를 잰다.
- **왜 tracemalloc이 아니라 RSS인가** — DuckDB/pyarrow/feast의 네이티브(C++) 힙은 tracemalloc이
  못 잡는다(#292에서 18GB를 놓친 원인). RSS는 프로세스 전체 상주 메모리라 그 힙을 포함한다.
- **왜 psutil인가** — Windows에는 resource.getrusage(maxrss)가 없다. psutil은 크로스플랫폼이고
  feast 격리 그룹에 이미 설치돼 있다(전이 의존).

사용법(feast 격리 그룹):
  $env:PYTHONUTF8 = "1"   # Windows
  uv run --only-group feast python scripts/bench/feast_assembly_bench.py \
    --start 2026-07-07 --end 2026-07-21

  # 스모크(빠른 확인):
  uv run --only-group feast python scripts/bench/feast_assembly_bench.py \
    --start 2026-07-07 --end 2026-07-21 --limit 5000
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import psutil


def _sample_peak_rss(proc: subprocess.Popen, interval: float = 0.1) -> int:
    """자식 프로세스(+그 하위)의 RSS를 interval 초마다 샘플링해 피크 바이트를 반환한다."""
    parent = psutil.Process(proc.pid)
    peak = 0
    while proc.poll() is None:
        try:
            rss = parent.memory_info().rss
            for child in parent.children(recursive=True):
                try:
                    rss += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            peak = max(peak, rss)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            break
        time.sleep(interval)
    return peak


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="KST 시작일 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="KST 종료일 YYYY-MM-DD(포함)")
    parser.add_argument("--limit", type=int, default=None, help="spine 상한(스모크용)")
    parser.add_argument("--out", default=None, help="21피처 CSV 저장 경로(선택)")
    args = parser.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    validate = os.path.join(repo_root, "scripts", "validate_feast_assembly.py")
    cmd = [sys.executable, validate, "--start", args.start, "--end", args.end]
    if args.limit is not None:
        cmd += ["--limit", str(args.limit)]
    if args.out is not None:
        cmd += ["--out", args.out]

    print(f"[bench] 실행: {' '.join(cmd)}", flush=True)
    t0 = time.time()
    # 자식 stdout/stderr는 그대로 흘려보내(validate의 측정 로그를 보존), 부모는 RSS만 샘플링.
    proc = subprocess.Popen(cmd, cwd=repo_root)
    peak = _sample_peak_rss(proc)
    proc.wait()
    elapsed = time.time() - t0

    print("\n" + "=" * 60)
    print("[bench] 피크 메모리 실측 (validate_feast_assembly 자식 프로세스 RSS)")
    print(f"  기간: {args.start} ~ {args.end}" + (f" (limit={args.limit})" if args.limit else " (전량)"))
    print(f"  피크 RSS: {peak / 1e9:.2f} GB ({peak / 1e6:.0f} MB)")
    print(f"  벽시계 시간: {elapsed:.1f}s")
    print(f"  종료 코드: {proc.returncode}")
    print("=" * 60)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
