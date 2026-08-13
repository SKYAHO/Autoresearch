"""실험 리포트 markdown을 워크벤치에 넣을 HTML 페이지로 바꾸는 순수 변환 경계.

[파이프라인]
Experiment API가 돌려준 `report_markdown`을 받은 뒤부터, `views.py`가 그것을 iframe에
넣기 전까지의 구간을 담당한다. 조회(`ui/client.py`)와 화면 배치(`ui/views.py`) 사이에
있으며 Streamlit을 import하지 않는다.

[기능]
markdown을 raw HTML 없이 HTML 조각으로 변환하고, 우리가 소유하는 고정 스타일 문서로
조립한다.

[비책임]
HTTP 조회(`ui/client.py`), session state(`ui/state.py`), 화면 배치와 iframe 삽입
(`ui/views.py`), 리포트 본문의 생성(`executor/report.py`)은 담당하지 않는다.

[중요] **iframe은 격리 경계가 아니다.** Streamlit의 iframe sandbox는 고정 목록이고
`allow-same-origin`과 `allow-scripts`를 둘 다 포함하며 본문은 srcdoc으로 들어간다 —
부모와 같은 origin이다. 따라서 이 모듈의 `html=False`가 **유일한 방어**다. 리포트를
쓰는 Codex의 입력에는 외부 사용자가 쓴 GitHub 이슈 본문 원문이 들어간다
(`executor/prompt.py`). 이 설정을 켜거나 여기서 만든 문서에 스크립트를 넣으면 그
경계가 사라진다. 계약 정본은
`docs/specs/2026-08-10-experiment-report-html-workbench.md` 결정 4·5다.
"""

from __future__ import annotations

from typing import Final

from markdown_it import MarkdownIt


# `html=False`가 인라인·블록 raw HTML을 모두 escape하고, 기본 `validateLink`가
# `javascript:`·`vbscript:`·`file:`·`data:`(이미지 제외) 링크를 앵커로 만들지 않는다.
# **이 설정을 바꾸지 않는다** — 모듈 docstring의 [중요]가 이유다.
#
# `table`은 따로 켠다. commonmark 프리셋에 표 확장이 없어 그냥 두면 리포트의
# baseline·candidate 비교표가 `| 지표 | delta |` 파이프 문자 그대로 한 문단에 찍힌다.
# 리포트에서 표는 장식이 아니라 주 지표가 놓이는 자리다(`prompt.REPORT_SECTIONS`의
# `## 주 지표`). `gfm-like` 프리셋은 쓰지 않는다 — linkify가 켜져 있어
# `linkify-it-py` 없이는 렌더가 예외로 죽는다. 표 규칙은 raw HTML escape나
# `validateLink`를 건드리지 않으므로 위의 방어는 그대로다.
_RENDERER: Final = MarkdownIt("commonmark", {"html": False}).enable("table")

# 리포트 문서의 고정 스타일. 우리가 소유하므로 여기를 고치면 과거 실험의 리포트도
# 전부 같이 바뀐다 — 변환을 UI에 둔 이유가 그것이다(spec 결정 4).
_STYLES: Final = """
  :root { color-scheme: light dark; }
  body {
    margin: 0;
    padding: 1.25rem 1.5rem 2rem;
    font-family: -apple-system, "Segoe UI", "Noto Sans KR", sans-serif;
    font-size: 0.95rem;
    line-height: 1.7;
    word-break: break-word;
  }
  h1 { font-size: 1.45rem; margin: 0 0 1rem; }
  h2 { font-size: 1.2rem; margin: 1.8rem 0 0.7rem; }
  h3 { font-size: 1.05rem; margin: 1.4rem 0 0.5rem; }
  p, li { margin: 0.5rem 0; }
  code { font-size: 0.88em; padding: 0.1em 0.35em; border-radius: 4px; }
  pre { padding: 0.9rem 1rem; border-radius: 8px; overflow-x: auto; }
  pre code { padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; display: block; overflow-x: auto; }
  th, td { border: 1px solid rgba(128, 128, 128, 0.45); padding: 0.4rem 0.6rem; text-align: left; }
  blockquote { margin: 1rem 0; padding: 0.1rem 1rem; border-left: 3px solid rgba(128, 128, 128, 0.5); }
  img { max-width: 100%; height: auto; }
"""


def render_report_html(markdown_text: str) -> str:
    """리포트 markdown을 raw HTML 없이 HTML 조각으로 바꾼다.

    Args:
        markdown_text: 에이전트가 쓴 `report.md` 본문.

    Returns:
        `<p>`·`<h2>` 등으로 이루어진 HTML 조각. 본문이 비면 빈 문자열이다.
    """
    return _RENDERER.render(markdown_text)


def build_report_document(body_html: str) -> str:
    """HTML 조각을 iframe srcdoc에 넣을 완결된 문서로 조립한다.

    **스크립트를 넣지 않는다.** 격리가 없는 곳에 실행 표면을 만들 이유가 없다.

    **`<base target="_blank">`를 넣는다.** Streamlit이 iframe에 붙이는 sandbox
    목록에는 `allow-top-navigation-by-user-activation`이 있다. 리포트 본문에는
    에이전트가 쓴 링크가 들어가고 그 입력에는 외부 사용자가 작성한 GitHub 이슈
    본문 원문이 섞여 있다(모듈 docstring). 사용자가 그 링크를 클릭하면 `target`
    지정 없이는 고정 620px iframe 전체가 외부 페이지로 바뀌어 버리고 돌아갈
    방법이 없으며, 워크벤치 전체를 다른 곳으로 이동시키는 피싱도 가능해진다.
    `<base target="_blank">`는 모든 링크를 새 tab으로 열어 이 경로를 막는다 —
    스크립트 실행은 아니므로 결정 5의 escape 방어와는 별개의 층이다.

    **원격 리소스 자동 로드도 막는다.** `![](http://attacker.example/pixel.png)` 같은
    평범한 markdown 이미지는 raw HTML이 아니라 escape 대상이 아니고, 그대로 `<img>`가
    된다. iframe이 열리는 순간 워크벤치 사용자의 IP와 `Referer`가 그 호스트로 나간다 —
    사용자가 클릭하지 않아도 일어나므로 `<base target>`이 막는 것과 다른 경로다. 같은
    위협 모델(에이전트 입력에 외부 사용자의 이슈 본문이 들어간다)에 그대로 얹힌다.

    - `referrer` `no-referrer`: 어떤 요청에도 워크벤치 URL을 실어 보내지 않는다.
    - CSP `img-src data:`: 원격 이미지 자체를 막아 IP 노출까지 끊는다. 리포트는
      실험을 서술한 산문이고 executor는 이미지를 붙이지 않으므로 잃는 것이 없다.
      `data:image/*`는 markdown-it이 이미 허용하는 유일한 이미지 경로라 남긴다.
    - CSP `script-src 'none'`: 우리 템플릿에 스크립트가 없다는 사실을 브라우저에도
      선언해, `html=False` 하나에 걸려 있던 방어에 층을 하나 더 둔다.

    `default-src`를 두지 않아 인라인 `<style>`은 그대로 적용된다.
    """
    return (
        "<!doctype html>\n"
        '<html lang="ko">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="referrer" content="no-referrer">\n'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"img-src data:; script-src 'none'\">\n"
        '<base target="_blank">\n'
        f"<style>{_STYLES}</style>\n"
        "</head>\n"
        f"<body>\n{body_html}\n</body>\n"
        "</html>\n"
    )


def report_document(markdown_text: str) -> str:
    """리포트 markdown 하나를 화면에 넣을 HTML 문서로 바꾼다."""
    return build_report_document(render_report_html(markdown_text))
