from autoresearch.action_logs.cli import build_parser


def test_cli_requires_explicit_hourly_interval():
    parser = build_parser()

    args = parser.parse_args(
        [
            "--mode",
            "single",
            "--partition-date",
            "2026-07-13",
            "--interval-start",
            "2026-07-13T09:00:00+09:00",
            "--interval-end",
            "2026-07-13T10:00:00+09:00",
            "--output-base-path",
            "data/action_log",
            "--filesystem",
            "local",
        ]
    )

    assert args.mode == "single"
    assert args.max_users == 300
    assert args.interval_start == "2026-07-13T09:00:00+09:00"
