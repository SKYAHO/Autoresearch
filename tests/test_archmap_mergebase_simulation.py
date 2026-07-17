"""INT-1 (최종 전체 리뷰): CI의 2-dot diff가 남의 변경을 이 PR 탓으로 돌림.

`.github/workflows/archmap.yml`은 워크플로우라 단위 테스트로 직접 실행할 수
없다. 대신 실제 git 저장소를 만들어 GitHub `pull_request` 페이로드의
`base.sha`(머지베이스가 아니라 base 브랜치의 "현재 tip")를 그대로 쓰는 2-dot
비교와, `git merge-base`로 구한 실제 분기점을 쓰는 3-dot 의미론 비교를 나란히
재현해 전자만 허위 breaking을 만든다는 것을 증명한다.

시나리오:
  1. 커밋 A(=머지베이스)에서 PR 브랜치를 딴다. PR은 schema.py를 건드리지 않고
     무관한 파일만 바꾼다.
  2. PR이 열려 있는 동안 main이 전진해 커밋 B를 만든다 — schema.py에 새 함수
     `foo`가 추가된다(이 PR과 무관하게 다른 작업이 머지된 것을 시뮬레이션).
  3. GitHub pull_request 페이로드의 base.sha는 이제 B다(머지베이스 A가 아니다).

naive(2-dot, base=B) 비교는 head(A 시점 schema.py, foo 없음)와 B(foo 있음)를
비교해 "이 PR이 foo를 지웠다"는 허위 breaking_signatures를 만든다.
merge-base(3-dot, base=A) 비교는 foo가 애초에 diff 범위 밖이므로 조용하다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tools.archmap.build import build_architecture
from tools.archmap.delta import build_delta, parse_numstat

SCHEMA_REL = "autoresearch/action_logs/schema.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _clone_at(origin: Path, dest: Path, rev: str) -> Path:
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True,
                   capture_output=True, text=True)
    _git(dest, "checkout", "-q", rev)
    return dest


def _build_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """origin 저장소를 만들어 (origin, merge_base, base_tip, head_tip) 커밋을 반환한다."""
    origin = tmp_path / "origin.git_work"
    origin.mkdir()
    _git(origin, "init", "-q")
    _git(origin, "checkout", "-q", "-b", "main")

    # 커밋 A: 머지베이스. schema.py에는 bar()만 있다.
    _write(origin, SCHEMA_REL, "def bar():\n    return 1\n")
    _write(origin, "autoresearch/action_logs/__init__.py", "")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "A: bar 추가")
    merge_base = _git(origin, "rev-parse", "HEAD")

    # PR 브랜치를 A에서 딴다. schema.py는 건드리지 않고 무관한 파일만 바꾼다.
    _git(origin, "checkout", "-q", "-b", "pr-branch")
    _write(origin, "autoresearch/action_logs/other.py", "def other_fn():\n    return 2\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "PR: other.py 추가 (schema.py 무관)")
    head_tip = _git(origin, "rev-parse", "HEAD")

    # main을 A 이후로 전진시킨다(PR과 무관한 별도 머지) — schema.py에 foo 추가.
    _git(origin, "checkout", "-q", "main")
    _write(origin, SCHEMA_REL, "def bar():\n    return 1\n\n\ndef foo():\n    return 3\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "B: main에 foo 머지(PR과 무관)")
    base_tip = _git(origin, "rev-parse", "HEAD")

    return origin, merge_base, base_tip, head_tip


def test_naive_2dot_base_tip_falsely_blames_pr_for_unrelated_removal(tmp_path):
    origin, merge_base, base_tip, head_tip = _build_repo(tmp_path)

    # naive: GitHub pull_request 페이로드의 base.sha(=base_tip)를 그대로 쓴다.
    base_root = _clone_at(origin, tmp_path / "naive_base", base_tip)
    head_root = _clone_at(origin, tmp_path / "naive_head", head_tip)
    base_arch = build_architecture(base_root, "Autoresearch", base_tip, "")
    head_arch = build_architecture(head_root, "Autoresearch", head_tip, "")
    numstat = _git(origin, "diff", "--numstat", base_tip, head_tip)
    changed = parse_numstat(numstat)

    d = build_delta(base_arch, head_arch, changed, pr=165, issue=None)

    # 결함 재현: 이 PR이 foo를 건드린 적이 없는데도 "삭제했다"고 나온다.
    assert {"module": "action_logs.schema", "name": "foo"} in d["breaking_signatures"]
    removed = [m for m in d["changed_modules"] if m["id"] == "action_logs.schema"]
    assert removed and any(s["name"] == "foo" and s["change"] == "removed"
                           for s in removed[0]["symbols_changed"])


def test_mergebase_3dot_does_not_blame_pr_for_unrelated_change(tmp_path):
    origin, merge_base, base_tip, head_tip = _build_repo(tmp_path)

    # 수정된 방식: base.json도, numstat도 merge-base 기준으로 만든다.
    computed_merge_base = _git(origin, "merge-base", base_tip, head_tip)
    assert computed_merge_base == merge_base  # 전제 확인

    base_root = _clone_at(origin, tmp_path / "mb_base", merge_base)
    head_root = _clone_at(origin, tmp_path / "mb_head", head_tip)
    base_arch = build_architecture(base_root, "Autoresearch", merge_base, "")
    head_arch = build_architecture(head_root, "Autoresearch", head_tip, "")
    numstat = _git(origin, "diff", "--numstat", merge_base, head_tip)
    changed = parse_numstat(numstat)

    # schema.py는 PR 범위 밖이라 numstat/changed_modules에 아예 등장하지 않는다.
    assert SCHEMA_REL not in changed

    d = build_delta(base_arch, head_arch, changed, pr=165, issue=None)

    # foo는 애초에 base(merge_base)에도 head에도 없으므로 비교 대상이 아니다 —
    # "이 PR이 foo를 지웠다"는 허위 주장이 사라진다.
    assert not any(b["name"] == "foo" for b in d["breaking_signatures"])
    assert not any(m["id"] == "action_logs.schema" for m in d["changed_modules"])
    # 실제 PR 변경(other.py 추가)만 changed_modules에 남는다.
    other = [m for m in d["changed_modules"] if m["id"] == "action_logs.other"]
    assert other and other[0]["symbols_changed"] == [
        {"name": "other_fn", "change": "added", "line": 1}]
