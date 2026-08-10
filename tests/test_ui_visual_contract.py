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

import pytest

pytest.importorskip("streamlit", reason="orchestration-ui 그룹이 설치돼야 한다")

from streamlit import config as streamlit_config  # noqa: E402

from agent_orchestration.ui import views  # noqa: E402
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


def test_theme_owns_the_colors_so_css_does_not_reintroduce_dead_variables() -> None:
    """CSS가 다시 Streamlit 테마 변수를 참조하면 그 선언은 또 죽는다."""
    css = workbench_css()

    assert "var(--background-color)" not in css
    assert "var(--text-color)" not in css
    assert "var(--primary-color)" not in css
    assert "var(--secondary-background-color)" not in css


def test_run_summary_shows_conditions_and_never_dumps_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """인스펙터는 실행 조건만 적는다. 지표는 결과 탭이 소유한다.

    이 패널은 `metric_summary` 전체를 `st.code(json.dumps(...))`로 덤프했고, 결과 탭의
    카드와 **같은 값**이 두 번 나왔다.
    """
    captions: list[str] = []
    forbidden: list[str] = []
    monkeypatch.setattr(views.st, "caption", lambda text, **_: captions.append(text))
    monkeypatch.setattr(views.st, "code", lambda *_a, **_k: forbidden.append("code"))
    monkeypatch.setattr(views.st, "json", lambda *_a, **_k: forbidden.append("json"))
    monkeypatch.setattr(views.st, "metric", lambda *_a, **_k: forbidden.append("metric"))
    monkeypatch.setattr(views.st, "markdown", lambda *_a, **_k: forbidden.append("markdown"))

    views._render_metrics(_SNAPSHOT)

    assert forbidden == []
    assert any("시드 3개" in caption for caption in captions)
    assert any("42, 43, 44" in caption for caption in captions)
    # conditions·paired는 결과 탭이 그린다. 이름조차 여기 나오면 안 된다.
    assert not any("conditions" in caption for caption in captions)
    assert not any("paired" in caption for caption in captions)


def test_run_summary_keeps_unknown_keys_instead_of_dropping_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """스냅샷 계약이 넓어져도 새 값이 화면에서 조용히 사라지지 않는다."""
    captions: list[str] = []
    monkeypatch.setattr(views.st, "caption", lambda text, **_: captions.append(text))

    views._render_metrics({"seeds": [1], "brand_new_field": "42"})

    assert any("brand_new_field: 42" in caption for caption in captions)


def test_split_mismatch_is_stated_in_the_inspector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """테스트셋이 갈렸다는 사실은 지표 옆 어디에서든 읽혀야 한다."""
    captions: list[str] = []
    monkeypatch.setattr(views.st, "caption", lambda text, **_: captions.append(text))

    views._render_metrics({"split_matches": False})

    assert any("불일치" in caption for caption in captions)


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        pytest.param("짧은 가설", 34, "짧은 가설", id="한도_이내는_그대로"),
        pytest.param(
            "기존 21개 CTR 스칼라 피처와 동일한 고정 training 구성을 쓴다",
            34,
            "기존 21개 CTR 스칼라 피처와 동일한 고정…",
            id="공백_경계에서_끊는다",
        ),
        pytest.param("가" * 50, 10, "가" * 10 + "…", id="공백이_없으면_그대로_자른다"),
        pytest.param("첫 줄\n둘째 줄", 34, "첫 줄 둘째 줄", id="줄바꿈을_접는다"),
    ],
)
def test_shorten_never_cuts_a_word_in_half(text: str, limit: int, expected: str) -> None:
    """사이드바 라벨이 "고정 traini"처럼 끝나던 절단을 막는다."""
    assert views._shorten(text, limit) == expected
