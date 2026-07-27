"""Autoresearch application의 공개 batch module 진입점."""

from autoresearch.logging_json import setup_json_logging

BATCH_CONTRACT_VERSION = "batch-contract-v1"

__all__ = ["BATCH_CONTRACT_VERSION"]

# 모든 batch CLI(`python -m autoresearch.jobs.*`)의 공용 로깅 설정 지점 —
# KPO 파드 stdout이 Filebeat에 수집되므로 serving과 동일한 JSON 계약을
# 따른다 (#352). 멱등이며 AUTORESEARCH_JSON_LOGS=0으로 끌 수 있다.
setup_json_logging()
