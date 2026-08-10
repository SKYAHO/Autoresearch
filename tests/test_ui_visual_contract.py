"""워크벤치 표현 계층의 계약을 고정한다.

전체 파이프라인 중 사용자가 보는 화면의 **표현 규칙**만 검증한다. API 호출과 상태
전이는 `tests/test_ui_submission_app.py`와 서버 쪽 테스트가 담당한다.

여기 있는 테스트가 잡는 실패는 모두 조용하다. 잘못된 테마 키는 오류 없이 무시되고,
JSON 덤프는 예외 없이 그려지며, 낱말이 끊긴 라벨도 정상 렌더링이다 — 사람이 화면을
열어보기 전까지 아무도 모른다(#657).
"""

from __future__ import annotations

import pathlib
import tomllib
from datetime import datetime, timezone

import pytest

pytest.importorskip("streamlit", reason="orchestration-ui 그룹이 설치돼야 한다")

from streamlit import config as streamlit_config  # noqa: E402

from agent_orchestration.ui import views  # noqa: E402
from agent_orchestration.ui.models import status_tone  # noqa: E402
from agent_orchestration.ui.styles import workbench_css  # noqa: E402


THEME_CONFIG_PATH = pathlib.Path(".streamlit/config.toml")

# `tests/test_agent_orchestration_ui_report.py`의 SNAPSHOT_FIXTURE와 같은 계약이다.
_SNAPSHOT: dict[str, object] = {
    "contract_version": "experiment-metric-snapshot-v1",
    "primary_metric": "roc_auc",
    "seeds": [42, 43, 44],
    "conditions": {
        "baseline": {"roc_auc": 0.812},
        "candidate": {"roc_auc": 0.831},
    },
    "paired": {"roc_auc": {"mean": 0.019, "standard_error": 0.004}},
    "split_matches": True,
    "dataset_fingerprint": "sha256:abcdef0123456789",
}


def _flatten(table: dict, prefix: str = "") -> list[str]:
    """중첩 TOML 테이블을 `theme.sidebar.borderColor` 형태의 키로 편다."""
    keys: list[str] = []
    for name, value in table.items():
        path = f"{prefix}{name}"
        if isinstance(value, dict):
            keys.extend(_flatten(value, f"{path}."))
        else:
            keys.append(path)
    return keys


def test_every_theme_key_is_one_streamlit_actually_reads() -> None:
    """오타 난 테마 키는 오류 없이 무시된다 — 그래서 여기서 잡는다.

    이 저장소는 이미 같은 종류로 한 번 당했다. `styles.py`가 존재하지 않는
    `var(--background-color)`를 참조해 스타일 절반이 조용히 버려지고 있었다(#657).
    """
    settings = tomllib.loads(THEME_CONFIG_PATH.read_text(encoding="utf-8"))
    unknown = [
        key
        for key in _flatten(settings)
        if key not in streamlit_config._config_options_template
    ]

    assert unknown == []


@pytest.mark.parametrize("name", ["workbench-kicker", "workbench-hypothesis"])
def test_paragraph_classes_keep_the_specificity_that_makes_font_size_apply(
    name: str,
) -> None:
    """`<p>` 위의 클래스 하나짜리 선택자는 `font-size`만 조용히 진다.

    Streamlit이 `stMarkdownContainer`의 `p`에 거는 크기 규칙에 밀려, 색과 굵기는
    먹는데 크기만 무시된다. 가설 본문이 1.3rem 대신 16px로 그려지고 있었는데 화면만
    봐서는 알아채기 어려웠다 — 브라우저에서 계산값을 읽고서야 드러났다(#657).

    선택자를 `.workbench-hypothesis`로 되돌리는 순간 같은 증상이 돌아오므로
    특정성 자체를 계약으로 고정한다.
    """
    css = workbench_css()

    assert f".stApp p.{name}" in css
    # 클래스만 단독으로 쓴 선언이 남아 있으면 그 선언이 진다.
    for line in css.splitlines():
        stripped = line.strip()
        if stripped.startswith(f".{name}"):
            pytest.fail(f"특정성이 부족한 선택자가 남아 있습니다: {stripped}")


def test_top_padding_clears_the_streamlit_header() -> None:
    """헤더가 본문을 덮으면 첫 줄만 잘린다 — 배경색이 같아 덮은 티도 안 난다.

    `stHeader`는 `position: absolute`에 높이 60px다. 상단 여백을 2.2rem(35.2px)으로
    줄였더니 kicker가 8.8px 덮였다(#657). 브라우저 좌표로 확인한 값이므로 여백이
    60px 아래로 다시 내려가지 못하게 막는다.
    """
    root_font_px = 16
    header_px = 60
    values = [
        float(line.split("padding-top:")[1].split("rem")[0])
        for line in workbench_css().splitlines()
        if "padding-top:" in line and "rem" in line
    ]

    assert values, "block-container의 상단 여백 선언을 찾지 못했습니다."
    for value in values:
        assert value * root_font_px > header_px, f"{value}rem은 헤더 60px를 덮습니다."


def test_theme_owns_the_colors_so_css_does_not_reintroduce_dead_variables() -> None:
    """CSS가 다시 Streamlit 테마 변수를 참조하면 그 선언은 또 죽는다."""
    css = workbench_css()

    assert "var(--background-color)" not in css
    assert "var(--text-color)" not in css
    assert "var(--primary-color)" not in css
    assert "var(--secondary-background-color)" not in css


def _rendered_inspector(
    monkeypatch: pytest.MonkeyPatch, metrics: dict[str, object]
) -> tuple[str, list[str]]:
    """`_render_metrics`가 그린 HTML과 금지된 위젯 호출 기록을 반환한다."""
    markdown: list[str] = []
    forbidden: list[str] = []
    monkeypatch.setattr(views.st, "markdown", lambda text, **_: markdown.append(text))
    monkeypatch.setattr(views.st, "code", lambda *_a, **_k: forbidden.append("code"))
    monkeypatch.setattr(views.st, "json", lambda *_a, **_k: forbidden.append("json"))
    monkeypatch.setattr(views.st, "metric", lambda *_a, **_k: forbidden.append("metric"))
    monkeypatch.setattr(views.st, "caption", lambda *_a, **_k: None)

    views._render_metrics(metrics)

    return "".join(markdown), forbidden


def test_run_summary_shows_conditions_and_never_dumps_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """인스펙터는 실행 조건만 적는다. 지표는 결과 탭이 소유한다.

    이 패널은 `metric_summary` 전체를 `st.code(json.dumps(...))`로 덤프했고, 결과 탭의
    카드와 **같은 값**이 두 번 나왔다.
    """
    rendered, forbidden = _rendered_inspector(monkeypatch, _SNAPSHOT)

    assert forbidden == []
    assert "42, 43, 44" in rendered
    # conditions·paired는 결과 탭이 그린다. 이름조차 여기 나오면 안 된다.
    assert "conditions" not in rendered
    assert "paired" not in rendered


def test_run_summary_keeps_unknown_keys_instead_of_dropping_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """스냅샷 계약이 넓어져도 새 값이 화면에서 조용히 사라지지 않는다."""
    rendered, _ = _rendered_inspector(
        monkeypatch, {"seeds": [1], "brand_new_field": "42"}
    )

    assert "brand_new_field" in rendered
    assert "42" in rendered


def test_split_mismatch_is_stated_in_the_inspector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """테스트셋이 갈렸다는 사실은 지표 옆 어디에서든 읽혀야 한다."""
    rendered, _ = _rendered_inspector(monkeypatch, {"split_matches": False})

    assert "불일치" in rendered


def test_inspector_escapes_values_because_it_renders_raw_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """스냅샷 값은 결국 에이전트가 쓴다. `unsafe_allow_html` 경로가 신뢰 경계다."""
    rendered, _ = _rendered_inspector(
        monkeypatch, {"results_uri": "<img src=x onerror=alert(1)>"}
    )

    assert "<img" not in rendered
    assert "&lt;img" in rendered


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("짧은 가설", "짧은 가설", id="그대로"),
        pytest.param("첫 줄\n둘째 줄", "첫 줄 둘째 줄", id="줄바꿈을_접는다"),
        pytest.param("공백   여러   칸", "공백 여러 칸", id="연속_공백을_접는다"),
        pytest.param("가" * 200, "가" * 200, id="길어도_버리지_않는다"),
    ],
)
def test_one_line_folds_whitespace_without_dropping_characters(
    text: str, expected: str
) -> None:
    """접기만 하고 자르지는 않는다."""
    assert views._one_line(text) == expected


_STARTED_AT = datetime(2026, 8, 9, 9, 15, tzinfo=timezone.utc)  # KST 18:15


def _label_lines(status: str, hypothesis: str) -> tuple[str, str]:
    """목록 라벨을 (윗줄, 요약줄)로 가른다."""
    head, _, summary = views._list_label(status, hypothesis, _STARTED_AT).partition("\n")
    return head.strip(), summary


def test_sidebar_label_leads_with_status_and_time() -> None:
    """25개를 세로로 훑는 화면에서 눈이 먼저 찾는 것은 상태와 시각이다.

    한 줄에 모두 이어 붙이면 상태가 문장에 묻혀 목록을 훑을 수 없다(#657).
    위젯 라벨에는 HTML을 넣을 수 없어 Streamlit 마크다운 색 문법을 쓴다.
    """
    head, summary = _label_lines("PASSED", "가설 본문이다.")

    assert head.startswith(":green[PASSED]")
    assert "08-09 18:15" in head
    assert summary == "가설 본문이다."


def test_sidebar_label_ends_at_a_sentence_not_mid_phrase() -> None:
    """목록이 전부 `…`로 끝나면 어느 실험인지 고를 수 없다.

    32자·34자·50자를 차례로 시도했지만 어느 값이든 결과는 같았다 — 같은 파라미터를
    건드린 실험들이 모두 같은 접두사로 끝나 서로 구별되지 않았다. 반대로 자르지 않고
    전문을 넣으니 항목 하나가 스무 줄이 되어 목록으로 쓸 수 없었다(#657).
    """
    _, summary = _label_lines(
        "PASSED",
        "learning_rate를 0.05에서 0.03으로 낮추면 test ROC-AUC가 개선된다. "
        "변경 대상은 learning_rate 하나이며, 데이터·분할·피처는 바꾸지 않는다.",
    )

    assert summary.endswith("개선된다.")
    assert "…" not in summary
    # 실험을 가르는 값이 남아야 목록에서 고를 수 있다.
    assert "0.03" in summary


def test_sidebar_label_keeps_a_decimal_point_from_ending_the_sentence() -> None:
    """`0.05`의 소수점을 문장 끝으로 오인하면 요약이 "learning_rate를 0."이 된다."""
    _, summary = _label_lines("RUNNING", "learning_rate를 0.05에서 0.03으로 낮춘다.")

    assert summary == "learning_rate를 0.05에서 0.03으로 낮춘다."


def test_sidebar_label_still_caps_a_runaway_first_sentence() -> None:
    """첫 문장이 통째로 한 문단인 가설도 있다 — 그때는 잘라야 한다."""
    _, summary = _label_lines("ERROR", "가" * 300 + ".")

    assert len(summary) < 90
    assert summary.endswith("…")


def test_every_status_has_a_markdown_tone() -> None:
    """색 이름이 없으면 상태가 전부 회색으로 묻힌다."""
    for status in ("CREATED", "RUNNING", "EVALUATING", "PASSED", "FAILED", "ERROR", "PROMOTED"):
        assert status_tone(status) != "" and status_tone(status) is not None
    # 표에 없는 값도 화면을 깨뜨리지 않는다.
    assert status_tone("WHATEVER") == "gray"
