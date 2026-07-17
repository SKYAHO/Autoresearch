import textwrap

from tools.archmap.cli_contract import extract_cli_args

SAMPLE = textwrap.dedent('''
    import argparse

    def _build_parser():
        p = argparse.ArgumentParser()
        p.add_argument("--mode", choices=["single", "shard"], required=True)
        p.add_argument("--partition-date", required=True)
        p.add_argument("--max-users", type=int)
        p.add_argument("positional_arg")
        group = p.add_argument_group("etc")
        group.add_argument("--seed", type=int, default=42)
        p.add_argument("--mode", help="중복은 한 번만")
        p.add_argument("-m", "--mode2", required=True)
        return p
''')


def test_extracts_flags_in_source_order_dedup_with_required():
    # FG-1: required 플래그는 그대로 유실되지 않고 각 인자에 보존되어야 한다.
    assert extract_cli_args(SAMPLE) == [
        {"flag": "--mode", "required": True},
        {"flag": "--partition-date", "required": True},
        {"flag": "--max-users", "required": False},
        {"flag": "--seed", "required": False},
        {"flag": "--mode2", "required": True},
    ]


def test_short_alias_before_long_flag_still_picks_long_flag():
    # FG-1 한계비용 0 수정: add_argument("-m", "--mode2", ...)처럼 짧은 별칭이
    # args[0]에 먼저 오면 예전 구현은 "--mode2"를 통째로 놓쳤다.
    source = textwrap.dedent('''
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("-x", "--exclusive-flag", required=True)
    ''')
    assert extract_cli_args(source) == [{"flag": "--exclusive-flag", "required": True}]


def test_no_parser_returns_empty():
    assert extract_cli_args("x = 1\n") == []
