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
        {"flag": "positional_arg", "required": True},
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


def test_positional_nargs_controls_required_contract():
    # Given: 필수·선택 위치 인자가 섞인 argparse 계약이다.
    source = textwrap.dedent('''
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("input_dir")
        p.add_argument("output_dir", nargs="?")
        p.add_argument("filters", nargs="*")
        p.add_argument("targets", nargs="+")
        p.add_argument("-v")
    ''')

    # When: 공개 CLI 계약을 추출한다.
    extracted = extract_cli_args(source)

    # Then: 위치 인자는 보존되고 실제 선택 가능한 nargs만 선택으로 분류된다.
    assert extracted == [
        {"flag": "input_dir", "required": True},
        {"flag": "output_dir", "required": False},
        {"flag": "filters", "required": False},
        {"flag": "targets", "required": True},
    ]
