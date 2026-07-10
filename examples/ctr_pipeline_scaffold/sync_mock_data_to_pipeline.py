#!/usr/bin/env python3
"""
Synchronize mock data from scaffold output to pipeline input directories.

이 스크립트는 다음을 수행한다:
1. 01_generate_mock_raw_data.py, 02_generate_event_log.py를 순서대로 실행해 scaffold 데이터 재생성
2. scaffold/data/의 산출물을 data/raw/, data/processed/로 복사(rename)해 파이프라인이 읽는 경로에 반영

**중요**: data/raw/youtube_videos.csv, data/raw/personas.csv, data/processed/events.csv는
이 스크립트의 산출물이며, 직접 수정하면 안 된다. 스펙 변경 시 scaffold를 수정한 후
이 스크립트를 재실행할 것. 이렇게 하지 않으면 mock 데이터가 stale 상태로 남아 다음
조사/버그 때 같은 문제가 반복된다.

Usage:
    python examples/ctr_pipeline_scaffold/sync_mock_data_to_pipeline.py
"""

import os
import sys
import shutil
from pathlib import Path

# Add PROJECT_ROOT to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# Import modules directly by adding to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util

def load_module(name, path):
    """Load a module from file path."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

gen_raw = load_module("gen_raw", os.path.join(os.path.dirname(__file__), "01_generate_mock_raw_data.py"))
gen_events = load_module("gen_events", os.path.join(os.path.dirname(__file__), "02_generate_event_log.py"))


def sync():
    """Generate scaffold mock data and sync to pipeline input directories."""
    print("=" * 70)
    print("Mock Data Synchronization")
    print("=" * 70)

    scaffold_dir = os.path.dirname(os.path.abspath(__file__))
    scaffold_data_dir = os.path.join(scaffold_dir, "data")

    # 1. Generate mock raw data (videos + personas)
    print("\n[Step 1] Generating mock raw data (videos + personas)...")
    gen_raw.main()

    # 2. Generate event log
    print("\n[Step 2] Generating event log...")
    gen_events.main()

    # 3. Sync to pipeline input directories
    print("\n[Step 3] Syncing to pipeline input directories...")

    # Ensure output directories exist
    raw_dir = os.path.join(PROJECT_ROOT, "data", "raw")
    processed_dir = os.path.join(PROJECT_ROOT, "data", "processed")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    # Copy files with rename
    files_to_sync = [
        (
            os.path.join(scaffold_data_dir, "video_raw.csv"),
            os.path.join(raw_dir, "youtube_videos.csv"),
            "video_raw.csv → youtube_videos.csv"
        ),
        (
            os.path.join(scaffold_data_dir, "persona_raw.csv"),
            os.path.join(raw_dir, "personas.csv"),
            "persona_raw.csv → personas.csv"
        ),
        (
            os.path.join(scaffold_data_dir, "event_log.csv"),
            os.path.join(processed_dir, "events.csv"),
            "event_log.csv → events.csv"
        ),
    ]

    for src, dst, desc in files_to_sync:
        if not os.path.exists(src):
            print(f"  [ERROR] Source file not found: {src}")
            sys.exit(1)
        shutil.copy2(src, dst)
        print(f"  [OK] {desc}")

    print("\n" + "=" * 70)
    print("Synchronization Complete")
    print("=" * 70)
    print(f"Output files:")
    print(f"  data/raw/youtube_videos.csv")
    print(f"  data/raw/personas.csv")
    print(f"  data/processed/events.csv")
    print("\nNext: python src/pipeline/build_training_dataset.py")


if __name__ == "__main__":
    sync()
