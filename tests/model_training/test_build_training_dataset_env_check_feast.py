"""_verify_assembly_environment()의 GCP 자격증명 체크 — feast 설치가 필요한 부분만
(#404). 환경변수/feast import 체크는 tests/test_build_training_dataset.py(dev
그룹)가 이미 커버한다.
"""

import pytest

pytest.importorskip("feast")

from autoresearch.model_training import build_training_dataset  # noqa: E402

_REQUIRED_ENV = {
    "GCS_REGISTRY_PATH": "gs://registry/registry.db",
    "GCS_STAGING_LOCATION": "gs://staging/",
}


def _set_required_env(monkeypatch) -> None:
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def _set_home_without_adc(monkeypatch, tmp_path) -> None:
    """ADC 파일이 없는 HOME으로 바꾼다 — ``os.path.expanduser("~/...")``가 HOME을 따르므로,
    stdlib를 전역 패치하지 않고도 "ADC 경로가 존재하지 않는다"만 좁게 재현한다.
    CLOUDSDK_CONFIG가 설정돼 있으면 HOME 대신 그 경로를 보므로 함께 지운다.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLOUDSDK_CONFIG", raising=False)


def _write_adc(tmp_path) -> None:
    """``$HOME/.config/gcloud``에 실제 ADC 파일을 만든다(경로 조립 로직 검증용)."""
    gcloud_dir = tmp_path / ".config" / "gcloud"
    gcloud_dir.mkdir(parents=True, exist_ok=True)
    (gcloud_dir / "application_default_credentials.json").write_text("{}")


def test_verify_assembly_environment_requires_gcp_credentials(monkeypatch, tmp_path) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    _set_home_without_adc(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="자격증명"):
        build_training_dataset._verify_assembly_environment()


def test_verify_assembly_environment_skips_credential_check_on_kubernetes(
    monkeypatch, tmp_path
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    _set_home_without_adc(monkeypatch, tmp_path)

    build_training_dataset._verify_assembly_environment()  # 예외 없이 통과해야 한다


def test_verify_assembly_environment_passes_with_google_application_credentials(
    monkeypatch, tmp_path
) -> None:
    # 환경변수가 설정된 것만으로는 부족하고 가리키는 파일이 실제로 있어야 통과한다 —
    # ADC 경로 분기와 같은 기준(존재 여부)이다.
    _set_required_env(monkeypatch)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    _set_home_without_adc(monkeypatch, tmp_path)
    service_account = tmp_path / "service-account.json"
    service_account.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(service_account))

    build_training_dataset._verify_assembly_environment()  # 예외 없이 통과해야 한다


def test_verify_assembly_environment_rejects_google_application_credentials_missing_file(
    monkeypatch, tmp_path
) -> None:
    # 존재하지 않는 파일을 가리키는 환경변수는 인증을 성립시키지 못한다.
    _set_required_env(monkeypatch)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    _set_home_without_adc(monkeypatch, tmp_path)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "nonexistent.json"))

    with pytest.raises(ValueError, match="자격증명"):
        build_training_dataset._verify_assembly_environment()


def test_verify_assembly_environment_passes_when_adc_file_exists(monkeypatch, tmp_path) -> None:
    # ADC 경로 조립($HOME/.config/gcloud/application_default_credentials.json)이
    # 실제로 맞는지 — 파일이 존재할 때 통과하는 쪽으로 검증한다.
    _set_required_env(monkeypatch)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    _set_home_without_adc(monkeypatch, tmp_path)
    _write_adc(tmp_path)

    build_training_dataset._verify_assembly_environment()  # 예외 없이 통과해야 한다
