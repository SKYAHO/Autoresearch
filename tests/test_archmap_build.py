import json
from pathlib import Path

from tools.archmap.build import STAGES, build_architecture


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "autoresearch" / "action_logs").mkdir(parents=True)
    (root / "autoresearch" / "jobs").mkdir(parents=True)
    (root / "src" / "features").mkdir(parents=True)
    (root / "autoresearch" / "__init__.py").write_text("", encoding="utf-8")
    (root / "autoresearch" / "action_logs" / "__init__.py").write_text("", encoding="utf-8")
    (root / "autoresearch" / "action_logs" / "schema.py").write_text(
        'ACTION_LOG_SCHEMA_VERSION = "action_log_schema_v1"\n', encoding="utf-8")
    (root / "autoresearch" / "jobs" / "__init__.py").write_text(
        'BATCH_CONTRACT_VERSION = "batch-contract-v1"\n__all__ = ["BATCH_CONTRACT_VERSION"]\n',
        encoding="utf-8")
    (root / "autoresearch" / "jobs" / "action_log.py").write_text(
        'import argparse\n\ndef _p():\n    p = argparse.ArgumentParser()\n'
        '    p.add_argument("--mode")\n    p.add_argument("--max-users")\n    return p\n',
        encoding="utf-8")
    (root / "src" / "features" / "build.py").write_text(
        "def build_features(df):\n    return df\n", encoding="utf-8")
    return root


def test_build_architecture(tmp_path):
    arch = build_architecture(_make_repo(tmp_path), "Autoresearch", "abc1234",
                              "https://github.com/SKYAHO/Autoresearch")
    assert arch["schema_version"] == "archmap-v0"
    assert arch["repo"] == "Autoresearch" and arch["revision"] == "abc1234"
    assert arch["stages"] == STAGES
    ids = {m["id"]: m for m in arch["modules"]}
    assert ids["action_logs.schema"]["stage"] == "action_logs"
    assert ids["action_logs.schema"]["version_consts"]["ACTION_LOG_SCHEMA_VERSION"]["value"] \
        == "action_log_schema_v1"
    assert ids["jobs"]["stage"] == "orchestration"          # jobs/__init__.py — 상수 보유
    assert "action_logs" not in ids                          # 빈 __init__.py 제외
    assert ids["src.features.build"]["stage"] == "training"
    assert arch["contracts"] == [{
        "name": "batch-contract-v1", "module": "jobs",
        "cli_args": ["--mode", "--max-users"], "required_args": [],
        "consumed_by": ["Autoresearch-airflow"]}]
    json.dumps(arch)  # 직렬화 가능해야 한다


def test_real_repo_smoke():
    repo_root = Path(__file__).resolve().parent.parent
    arch = build_architecture(repo_root, "Autoresearch", "HEAD", "")
    ids = {m["id"] for m in arch["modules"]}
    assert {"action_logs.schema", "virtual_users.schema", "youtube_collection.schema",
            "jobs.action_log"} <= ids
    consts = {m["id"]: m["version_consts"] for m in arch["modules"]}
    assert "ACTION_LOG_SCHEMA_VERSION" in consts["action_logs.schema"]
    contract = arch["contracts"][0]
    assert contract["name"] == "batch-contract-v1" and "--mode" in contract["cli_args"]
    # FG-1: 이 레포의 jobs/action_log.py는 --mode를 required=True로 등록한다.
    # required_args가 이를 보존하지 못하면 필수 인자 추가가 조용히 초록 처리된다.
    assert "--mode" in contract["required_args"]
    assert "--partition-date" in contract["required_args"]
