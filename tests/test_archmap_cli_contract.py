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
        return p
''')


def test_extracts_flags_in_source_order_dedup():
    assert extract_cli_args(SAMPLE) == ["--mode", "--partition-date", "--max-users", "--seed"]


def test_no_parser_returns_empty():
    assert extract_cli_args("x = 1\n") == []
