"""PR 이해 리포트 report.json 생성기 (OpenRouter).

전체 파이프라인 기준 CI·리포트 구간(cross/ci_release)의 보조 도구입니다.
pr-report.yml의 analyze job에서 실행되며, PR 메타데이터·diff·파이프라인 정본
문서를 결정적으로 수집해 OpenRouter chat completions API에 단일 프롬프트로
전달하고, report.schema.json 계약을 따르는 report.json을 저장소 루트에
생성합니다. 스키마 검증 실패 시 오류 내용을 피드백해 1회 재시도합니다.
템플릿 주입·배포·코멘트는 담당하지 않습니다 (inject.py와 워크플로 담당).

필요 환경 변수:
  OPENROUTER_API_KEY  OpenRouter API 키
  PR_REPORT_MODEL     모델 슬러그 (예: google/gemini-3.6-flash)
  PR_NUMBER           대상 PR 번호
  GH_TOKEN            gh CLI 인증 (Actions 기본 제공)

사용법: python generate_report.py [--dry-run]  (--dry-run은 프롬프트만 출력)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REPORT_PATH = Path("report.json")
SCHEMA_PATH = Path(".github/pr-report/report.schema.json")
NODES_PATH = Path(".github/pr-report/pipeline-nodes.json")
PIPELINE_DOCS = [
    Path("docs/guides/pipeline-overview.md"),
    Path(".claude/docs/architecture-overview.md"),
]

# 생성 파일은 diff에서 제외 (내용 이해에 불필요, 토큰 낭비)
EXCLUDED_FILE_PATTERNS = re.compile(
    r"(uv\.lock|poetry\.lock|package-lock\.json|yarn\.lock|\.parquet|\.pyc|"
    r"__pycache__|\.min\.(js|css))"
)
PER_FILE_DIFF_LIMIT = 20_000  # 파일당 최대 문자 수
TOTAL_DIFF_LIMIT = 150_000  # 전체 diff 최대 문자 수
DOC_LIMIT = 15_000  # 정본 문서당 최대 문자 수
MAX_ATTEMPTS = 2


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def gather_pr_meta(pr_number: str) -> dict:
    raw = json.loads(
        run(["gh", "pr", "view", pr_number, "--json", "title,author,body,headRefOid"])
    )
    body = raw.get("body") or ""
    issue_refs = sorted(
        {int(n) for n in re.findall(r"(?:[Cc]loses|[Ff]ixes|[Rr]esolves)\s+#(\d+)", body)}
    )
    return {
        "number": int(pr_number),
        "title": raw["title"],
        "author": raw["author"]["login"],
        "issue_refs": issue_refs,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "head_sha": raw["headRefOid"],
        "body": body,
    }


def gather_diff(pr_number: str) -> tuple[str, list[str]]:
    """diff를 파일 섹션 단위로 분해해 생성 파일 제외·크기 제한 후 재조립합니다."""
    full = run(["gh", "pr", "diff", pr_number])
    sections = re.split(r"(?m)^(?=diff --git )", full)
    kept: list[str] = []
    skipped: list[str] = []
    total = 0
    for sec in sections:
        if not sec.strip():
            continue
        m = re.match(r"diff --git a/(\S+)", sec)
        path = m.group(1) if m else "(unknown)"
        if EXCLUDED_FILE_PATTERNS.search(path):
            skipped.append(f"{path} (생성 파일 — docs 등급으로만 집계)")
            continue
        if total >= TOTAL_DIFF_LIMIT:
            skipped.append(f"{path} (전체 diff 크기 제한 초과로 생략)")
            continue
        if len(sec) > PER_FILE_DIFF_LIMIT:
            sec = sec[:PER_FILE_DIFF_LIMIT] + "\n... (이 파일의 diff는 길이 제한으로 잘림)\n"
        kept.append(sec)
        total += len(sec)
    return "".join(kept), skipped


def read_limited(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) > DOC_LIMIT:
        text = text[:DOC_LIMIT] + "\n... (길이 제한으로 잘림)"
    return text


def build_messages(meta: dict, diff: str, skipped: list[str]) -> list[dict]:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    nodes = NODES_PATH.read_text(encoding="utf-8")
    docs = "\n\n".join(
        f"### {p}\n{read_limited(p)}" for p in PIPELINE_DOCS if p.exists()
    )
    system = f"""당신은 이 PR을 팀원이 빠르게 이해하도록 돕는 분석가입니다. 모든 텍스트 출력은 한국어 격식체를 사용합니다.

아래 JSON Schema를 정확히 따르는 JSON 객체 **하나만** 출력하십시오. 마크다운 코드 펜스, 설명 문장 등 JSON 외 텍스트를 절대 포함하지 마십시오. additionalProperties가 false이므로 스키마에 없는 키를 넣으면 안 됩니다.

## 출력 스키마 (report.schema.json)
{schema}

## 파이프라인 노드 정본 (pipeline-nodes.json)
{nodes}

## 파이프라인 배경 문서
{docs}

## pipeline 작성 규칙
- nodes는 위 정본 카탈로그의 전체 노드를 복사하고 각 노드의 status만 판정합니다(unchanged/modified/added/removed). 이 PR이 카탈로그에 없는 새 구성요소를 도입할 때만 "custom:" 접두 id로 노드를 추가할 수 있습니다.
- 변경된 노드에는 as_is_ko / to_be_ko 를 각 1~2문장으로 채우고, focus에 중심 노드 id를 1~3개 지정합니다.
- edges는 카탈로그를 복사하되, 이 PR로 의미가 바뀌는 에지에만 status를 부여합니다.

## changes 중요도 rubric (rank는 같은 등급 안에서 영향이 큰 순)
- core     — 도메인 로직·알고리즘·동작 변경 (파이프라인 산출물이 달라짐)
- contract — pydantic 스키마, parquet/BigQuery 스키마, 공개 batch CLI 인자, API 요청/응답, Feast 정의 등 모듈 간 계약
- config   — 설정, 환경 변수, Dockerfile, CI 워크플로우, 의존성
- test     — 테스트 추가·수정
- docs     — 문서, 주석, 생성 파일(uv.lock 등)

같은 파일이라도 함수/클래스 단위로 나눠 항목화하고, docs/test는 묶어서 1~2개 항목으로 압축합니다. 총 20개 이하. diff_snippet은 이해에 필요한 핵심 hunk만 발췌합니다(항목당 4000자 이하, 불필요하면 생략). risk_notes_ko에는 데이터 계약 파급, 학습-서빙 일관성, 롤백 주의점 등을 적습니다.

summary_ko는 정확히 3줄, 각 줄 120자 이하입니다. qa_note_ko에는 "코드리뷰 봇이 diff 인라인에 남긴 '이해도 확인:' 질문에 답변한 뒤 스레드를 resolve해 주십시오"를 이 PR의 핵심 로직 주제와 함께 안내합니다."""

    skipped_note = (
        "\n\n## diff에서 제외된 파일\n" + "\n".join(f"- {s}" for s in skipped)
        if skipped
        else ""
    )
    user = f"""## PR 메타데이터
- number: {meta["number"]}
- title: {meta["title"]}
- author: {meta["author"]}
- issue_refs: {meta["issue_refs"]}
- head_sha: {meta["head_sha"]}
- generated_at: {meta["generated_at"]} (report.json의 pr.generated_at에 이 값을 그대로 사용)

## PR 본문
{meta["body"] or "(없음)"}{skipped_note}

## PR diff
{diff}"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def call_openrouter(messages: list[dict]) -> str:
    payload = {
        "model": os.environ["PR_REPORT_MODEL"],
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 20_000,
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
            "X-Title": "Autoresearch PR Comprehension Report",
        },
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"OpenRouter error: {data['error']}")
    return data["choices"][0]["message"]["content"]


def strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.DOTALL)
    return m.group(1) if m else text


def validate(path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["pipx", "run", "check-jsonschema", "--schemafile", str(SCHEMA_PATH), str(path)],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, proc.stdout + proc.stderr


def main() -> int:
    pr_number = os.environ["PR_NUMBER"]
    meta = gather_pr_meta(pr_number)
    diff, skipped = gather_diff(pr_number)
    messages = build_messages(meta, diff, skipped)

    if "--dry-run" in sys.argv:
        print(json.dumps(messages, ensure_ascii=False, indent=2))
        return 0

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if last_error:
            messages.append(
                {
                    "role": "user",
                    "content": "직전 출력이 스키마 검증에 실패했습니다. 아래 오류를 "
                    "수정해 스키마를 정확히 따르는 JSON 객체 하나만 다시 출력하십시오.\n\n"
                    + last_error[:4000],
                }
            )
        print(f"[generate_report] attempt {attempt}/{MAX_ATTEMPTS} "
              f"(model={os.environ['PR_REPORT_MODEL']})", file=sys.stderr)
        content = strip_fences(call_openrouter(messages))
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            last_error = f"JSON 파싱 실패: {e}"
            print(f"[generate_report] {last_error}", file=sys.stderr)
            messages.append({"role": "assistant", "content": content[:8000]})
            continue
        REPORT_PATH.write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ok, output = validate(REPORT_PATH)
        if ok:
            print("[generate_report] report.json 생성·검증 완료", file=sys.stderr)
            return 0
        last_error = output
        print(f"[generate_report] 스키마 검증 실패:\n{output}", file=sys.stderr)
        messages.append({"role": "assistant", "content": content[:8000]})

    print("[generate_report] 재시도 소진 — 실패", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
