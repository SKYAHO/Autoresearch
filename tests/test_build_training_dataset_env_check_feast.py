"""_verify_assembly_environment()의 GCP 자격증명 체크 — feast 설치가 필요한 부분만
(#404). 환경변수/feast import 체크는 tests/test_build_training_dataset.py(dev
그룹)가 이미 커버한다.
"""

import pytest

pytest.importorskip("feast")

from src.pipeline import build_training_dataset  # noqa: E402

_REQUIRED_ENV = {
    "GCS_REGISTRY_PATH": "gs://registry/registry.db",
    "GCS_STAGING_LOCATION": "gs://staging/",
}


def _set_required_env(monkeypatch) -> None:
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def test_verify_assembly_environment_requires_gcp_credentials(monkeypatch, tmp_path) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    fake_home = tmp_path / "no_adc_here"
    fake_home.mkdir()
    monkeypatch.setattr(
        build_training_dataset.os.path, "expanduser", lambda p: str(fake_home / "adc.json")
    )

    with pytest.raises(ValueError, match="자격증명"):
        build_training_dataset._verify_assembly_environment()


def test_verify_assembly_environment_skips_credential_check_on_kubernetes(
    monkeypatch, tmp_path
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    fake_home = tmp_path / "no_adc_here"
    fake_home.mkdir()
    monkeypatch.setattr(
        build_training_dataset.os.path, "expanduser", lambda p: str(fake_home / "adc.json")
    )

    build_training_dataset._verify_assembly_environment()  # 예외 없이 통과해야 한다


def test_verify_assembly_environment_passes_with_google_application_credentials(
    monkeypatch, tmp_path
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    cred_file = tmp_path / "service-account.json"
    cred_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(cred_file))

    build_training_dataset._verify_assembly_environment()  # 예외 없이 통과해야 한다
