"""Downsampling calibration을 별도 배포 모델로 패키징하는 얇은 wrapper.

전체 CTR 파이프라인 기준 이 모듈이 담당하는 구간:
- **담당**: negative downsampling으로 왜곡된 메인 모델 출력 확률 q를 원분포 확률 p로
  되돌리는 calibration을 **별도 모델 아티팩트**로 감싼다. 배포 단위를 "메인 모델 +
  calibration 모델" 2개로 만들어 서빙이 main → calibration 순서로 체이닝할 수 있게
  한다(#302). 파라미터는 sampling_rate `w` 하나뿐이며 JSON으로 직렬화한다(pickle 금지).
- **담당 아님(인접 책임)**: 보정 수식 자체는 `downsampling.apply_downsampling_calibration`
  (He 2014, #300)이 소유하며 이 모듈은 그것을 재사용만 한다 — 알고리즘 변경이 아니라
  패키징이다. 학습 시 calibrator 생성·등록은 `train.py`, 서빙 로딩·체이닝은
  `serving/model_loader.py`/`serving/service.py`, 두 모델 짝 검증도 로더가 담당한다.
  ONNX 변환·manifest 해시 검증은 #302 후속 슬라이스로 이 모듈 범위 밖이다.

설계: `docs/guides/ctr-model-specification.md`의 Model Packaging / Deployment 섹션.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.models.downsampling import ArrayLike, apply_downsampling_calibration

# calibration 모델 아티팩트 파일명. 서빙 로더의 MLFLOW_CALIBRATION_ARTIFACT_PATH와 계약.
CALIBRATION_PARAM_FILENAME = "calibration.json"


class DownsamplingCalibrator:
    """메인 모델 출력 q를 원분포 확률 p로 되돌리는 calibration 모델.

    내부 로직은 He 2014 공식 `p = q/(q + (1-q)/w)`(`apply_downsampling_calibration`)뿐이며
    fit이 없다. sampling_rate `w` 하나로 완전히 정의되므로 JSON으로 저장·로드한다.
    """

    def __init__(self, sampling_rate: float) -> None:
        # 진입부 검증은 apply_downsampling_calibration이 호출 시 수행한다(0<w≤1).
        # 저장 시점에도 미리 걸러 잘못된 아티팩트가 남지 않게 한다.
        if not (0.0 < sampling_rate <= 1.0):
            raise ValueError(
                f"sampling_rate는 (0, 1] 범위여야 합니다(negative를 남긴 비율): {sampling_rate}"
            )
        self.sampling_rate = float(sampling_rate)

    def calibrate(self, q: ArrayLike) -> ArrayLike:
        """메인 모델 출력 q(다운샘플 분포)를 원분포 보정 확률 p로 변환한다.

        `sampling_rate == 1.0`이면 항등(보정 없음)이라 downsampling 미사용·하위호환 경로가
        자동으로 no-op이 된다.
        """
        return apply_downsampling_calibration(q, self.sampling_rate)

    def to_json(self) -> str:
        return json.dumps({"sampling_rate": self.sampling_rate})

    def save(self, path: Path | str) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_json(cls, text: str) -> "DownsamplingCalibrator":
        data = json.loads(text)
        return cls(sampling_rate=float(data["sampling_rate"]))

    @classmethod
    def load(cls, path: Path | str) -> "DownsamplingCalibrator":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))
