"""Feast 환경(prod/dev) 셀렉터 — 온라인 스토어 접촉 여부 해석.

[파이프라인] 피처 구간 — Feast repo config 가 로드되기 **직전**에, 이 실행이
어느 환경(prod/dev)에 속하는지 판정하고 online store 삭제 스캔
(``full_scan_for_deletion``)을 켤지 끌지 결정한다.

[배경] dev 환경(오토리서치 에이전트의 실험)은 오프라인 전용이다 — 새 피처를
정의(apply)하고 ``get_historical_features``(BigQuery PIT)로 학습셋을 조립·평가할
뿐, 온라인 서빙(Redis)은 prod 만의 책임이다(#399). 문제는 ``feast apply`` 가
``full_scan_for_deletion: true`` 일 때 삭제된 FeatureView 의 고아 키를 정리하려고
online store(Redis)에 접속한다는 점이다. dev registry 로 apply 하면서 prod Redis 를
스캔하면 prod 키를 지울 수 있다. 그래서 **dev 는 full_scan 을 꺼서** apply 가 Redis 에
아예 접속하지 않게 한다(리포 spec: full_scan=false 동안 apply 는 Redis 미접속).

[기능]
- ``resolve_environment``: ``AUTORESEARCH_ENV``(prod|dev, 기본 prod)를 검증해 반환.
- ``online_full_scan_for_deletion``: 이 실행에서 online 삭제 스캔을 켤지(bool).
  배포가 ``FEAST_ONLINE_FULL_SCAN_FOR_DELETION`` 을 명시하면 그 값이 최종 권한을
  가지고, 없으면 환경에서 파생한다(prod → True, dev → False).
- ``ensure_online_store_env``: ``feature_store.yaml`` 의
  ``full_scan_for_deletion: ${FEAST_ONLINE_FULL_SCAN_FOR_DELETION}`` 치환이
  성립하도록 그 변수를 **setdefault** 로 채운다(이미 있으면 유지).

[비책임] Registry/Offline/Staging 좌표(``GCS_REGISTRY_PATH``/``BQ_DATASET``/
``GCS_STAGING_LOCATION``)의 prod/dev 분리는 배포 레이어가 env 로 주입한다 — 이
모듈은 관여하지 않는다. Feast ``project`` 이름은 registry 경로가 이미 환경을
가르므로 prod/dev 공통으로 둔다. Entity·FeatureView 정의는
``feature_definitions.py`` 가, FeatureStore 생성·CA 조달은 ``bootstrap.py`` 가 소유한다.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
import os

ENV_PROD = "prod"
ENV_DEV = "dev"
_VALID_ENVIRONMENTS = (ENV_PROD, ENV_DEV)

# 환경 셀렉터와 online 삭제 스캔 여부를 담는 env 키.
ENVIRONMENT_ENV_VAR = "AUTORESEARCH_ENV"
FULL_SCAN_ENV_VAR = "FEAST_ONLINE_FULL_SCAN_FOR_DELETION"

_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off"})


def resolve_environment(
    environment: Mapping[str, str] | None = None,
) -> str:
    """``AUTORESEARCH_ENV``를 읽어 실행 환경을 반환한다(기본 ``prod``).

    미설정·공백은 안전하게 ``prod``로 간주한다 — 환경 셀렉터가 없던 기존 배포가
    실수로 dev로 떨어지지 않도록 하기 위함이다. 허용되지 않는 값은 즉시 실패시켜
    오타로 인한 조용한 오분류를 막는다.
    """
    env = os.environ if environment is None else environment
    value = env.get(ENVIRONMENT_ENV_VAR, "").strip().lower()
    if not value:
        return ENV_PROD
    if value not in _VALID_ENVIRONMENTS:
        raise ValueError(
            f"{ENVIRONMENT_ENV_VAR} must be one of "
            f"{_VALID_ENVIRONMENTS!r}, got {value!r}"
        )
    return value


def _parse_bool(value: str) -> bool:
    token = value.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ValueError(
        f"{FULL_SCAN_ENV_VAR} must be a boolean token "
        f"(true/false), got {value!r}"
    )


def online_full_scan_for_deletion(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """이 실행에서 online store 삭제 스캔(``full_scan_for_deletion``)을 켤지 반환한다.

    배포가 ``FEAST_ONLINE_FULL_SCAN_FOR_DELETION`` 을 명시하면 그 값이 최종 권한을
    가진다. 없으면 환경에서 파생한다 — prod 는 True(고아 키 GC 유지), dev 는 False
    (apply 가 Redis 에 접속하지 않아 오프라인 실험이 prod Redis 를 건드리지 않음).
    """
    env = os.environ if environment is None else environment
    explicit = env.get(FULL_SCAN_ENV_VAR, "").strip()
    if explicit:
        return _parse_bool(explicit)
    return resolve_environment(env) == ENV_PROD


def ensure_online_store_env(
    environment: MutableMapping[str, str] | None = None,
) -> str:
    """``FEAST_ONLINE_FULL_SCAN_FOR_DELETION`` 을 setdefault 로 채우고 그 값을 반환한다.

    ``feature_store.yaml`` 의 ``full_scan_for_deletion: ${FEAST_ONLINE_FULL_SCAN_FOR_DELETION}``
    치환은 이 변수가 설정돼 있어야 성립한다. FeatureStore 를 ``repo_path`` 로 생성하기
    직전에 호출해, materialize·서빙 등 Python 경로가 무설정 시에도 prod(True)로 안전하게
    해석되도록 한다. 배포가 이미 값을 심었으면 덮지 않는다. 반환 문자열은 YAML 이
    boolean 으로 파싱하도록 ``"true"``/``"false"`` 소문자다.

    blank(빈 문자열·공백)는 ``online_full_scan_for_deletion`` 과 동일하게 **미설정**으로
    보고 덮어쓴다. ``setdefault`` 는 blank 를 "이미 설정됨"으로 취급해 그대로 두는데,
    그러면 두 함수의 해석이 어긋나고 yaml 치환 결과가 ``full_scan_for_deletion:`` (YAML
    null)이 된다. Feast 의 해당 필드는 ``Optional[bool]`` 이라 null 이 falsy 로 떨어지면
    prod 고아 키 GC 가 조용히 꺼진다 — 이 모듈이 막으려는 바로 그 사고다.
    """
    env = os.environ if environment is None else environment
    value = "true" if online_full_scan_for_deletion(env) else "false"
    if not env.get(FULL_SCAN_ENV_VAR, "").strip():
        env[FULL_SCAN_ENV_VAR] = value
    return env[FULL_SCAN_ENV_VAR]
