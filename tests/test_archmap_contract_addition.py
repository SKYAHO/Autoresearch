from tools.archmap.delta import build_delta


def test_new_contract_is_reported_as_nonbreaking_addition():
    # Given: base에는 계약이 없고 head에 새 작업 계약이 추가됐다.
    base = {
        "repo": "Autoresearch",
        "revision": "base-sha",
        "modules": [],
        "contracts": [],
    }
    head = {
        "repo": "Autoresearch",
        "revision": "head-sha",
        "modules": [],
        "contracts": [{
            "name": "batch-contract-v1:jobs.new_job",
            "cli_args": ["--mode"],
            "required_args": [],
        }],
    }

    # When: 두 아키텍처의 PR delta를 만든다.
    delta = build_delta(base, head, {}, pr=166, issue=None)

    # Then: 새 계약은 rename이나 breaking이 아니라 addition으로 보고된다.
    assert delta["cross_repo"] == [{
        "contract": "batch-contract-v1:jobs.new_job",
        "impact": "contract-added",
        "breaking": False,
        "details": "새 계약이 추가됨",
    }]
