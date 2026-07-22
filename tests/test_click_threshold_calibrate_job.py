import json

from autoresearch.action_logs.pipeline import write_action_log_draft_parquet
from autoresearch.action_logs.schema import ImpressionDraft
from autoresearch.jobs.click_threshold_calibrate import main


def _draft(user_id: str, video_id: str, cp: float) -> ImpressionDraft:
    return ImpressionDraft(
        user_id=user_id, video_id=video_id, click_propensity=cp,
        watch_fraction=0.5, would_like=False, duration_sec=100,
    )


def test_cli_emits_recommendation(tmp_path, capsys) -> None:
    drafts = [
        _draft("u1", "a", 0.9), _draft("u1", "b", 0.2),
        _draft("u2", "c", 0.3), _draft("u2", "d", 0.1),
    ]
    path = tmp_path / "drafts.parquet"
    write_action_log_draft_parquet(drafts, path)
    code = main(["--draft-path", str(path), "--target-ctr", "0.25"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["impressions"] == 4
    assert payload["users"] == 2
    assert payload["recommended_threshold"] == 0.9
    assert payload["achieved_ctr"] == 0.25


def test_cli_requires_target_ctr(tmp_path) -> None:
    import pytest
    with pytest.raises(SystemExit):
        main(["--draft-path", str(tmp_path / "x.parquet")])


def test_cli_missing_draft_path_returns_failed(tmp_path, capsys) -> None:
    bad_path = str(tmp_path / "nope.parquet")
    code = main(["--draft-path", bad_path, "--target-ctr", "0.25"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "failed"
    assert "error_type" in payload
    # 예상치 못한 실패(파일 IO)는 타입만 노출하고 경로는 절대 노출하지 않는다.
    assert "error" not in payload
    assert bad_path not in json.dumps(payload)


def test_cli_ceiling_exceeding_target_surfaces_curated_message(tmp_path, capsys) -> None:
    drafts = [
        _draft("u1", "a", 0.9), _draft("u1", "b", 0.2),
        _draft("u2", "c", 0.3), _draft("u2", "d", 0.1),
    ]
    path = tmp_path / "drafts.parquet"
    write_action_log_draft_parquet(drafts, path)
    code = main(["--draft-path", str(path), "--target-ctr", "0.9"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "failed"
    assert payload["error_type"] == "ValueError"
    assert "CTR ceiling" in payload["error"]
