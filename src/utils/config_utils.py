#!/usr/bin/env python3
"""프로젝트 루트 및 config 로드 공통 유틸리티."""

import os
import yaml


def get_project_root() -> str:
    """Windows/Linux 공통 루트 탐색."""
    current = os.path.dirname(os.path.abspath(__file__))
    while os.path.dirname(current) != current:
        if os.path.exists(os.path.join(current, "src")):
            return current
        current = os.path.dirname(current)
    raise RuntimeError("프로젝트 루트를 찾을 수 없습니다")


def load_config(config_path: str) -> dict:
    """config.yaml 로드."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)
