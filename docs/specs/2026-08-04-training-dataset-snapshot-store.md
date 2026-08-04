# 학습 데이터셋 스냅샷 GCS 게시·재사용 계약

> 상태: 설계 확정 · 구현 대기
> 관련: #188/#189(한 Pod 내 즉시 소비로 회피), #299(Feast PIT 전환, 별개 축),
> #454(실험 조립 판별·피처 보존), #464(spine 커버리지 가드), #423(snapshot provenance)

## 1. 배경 — "사라진다"가 아니라 "주소가 없다"

착수 전 조사에서 전제 하나가 정정됐다. **`training_dataset.csv`는 이미 GCS에
남고 있다.** 다만 데이터셋이 아니라 재현 아티팩트로서다.

- 조립(`_assemble_via_feast`)은 CSV를 쓴 뒤 **항상** sidecar
  `training_dataset.csv.snapshot.json`을 원자 게시한다
  (`build_training_dataset.py:714-724`). `dataset_sha256`·`schema_sha256`·
  `row_count`·events 구간·`feature_service`·registry generation이 들어 있다.
- `run-pipeline`은 `require_snapshot=True`로 학습을 부르고, sidecar가 있으면
  `train.py`가 **CSV 실물 + snapshot manifest + split manifest를 같은 MLflow run의
  `reproducibility/` artifact로 업로드**한다 (`train.py:352-379`, 호출부 `638-642`).
- GCP MLflow는 artifact destination이 GCS이므로, 결과적으로 Pod가 죽어도 CSV
  바이트는 보존된다.

그래서 실제 구멍은 소실이 아니라 **주소 체계와 소비 계약의 부재**다.

| 구멍 | 내용 |
|---|---|
| run 중심 주소 | `runs:/<run_id>/reproducibility/snapshot/training_dataset.csv` 뿐이다. **run_id를 먼저 알아야** 찾을 수 있고, "2026-08-01 구간 학습 데이터셋"처럼 날짜로 조회할 수단이 없다. |
| `build-features` 단독 실행 | MLflow run이 없으므로 업로드 자체가 없다. 로컬에만 쓰고 Pod와 함께 사라진다. |
| 네임스페이스 겸용 | `reproducibility/`는 감사·재현용 이름이다. 다른 Task·실험이 **입력으로 소비하는 계약**으로 문서화되거나 보장된 적이 없다. |
| 반복 조립 비용 | 같은 구간을 N개 실험 변형에 쓰려면 매번 Feast PIT를 다시 돈다. |

#188/#189가 `run-pipeline`을 한 Pod 안 순차 실행으로 만든 이유("Task마다 Pod가
분리돼 파일시스템이 공유되지 않는다")는 이 구멍을 **회피**한 것이지 해소한 것이
아니다. 이 spec이 그 회피를 정면으로 대체한다.

## 2. 목표 / 비목표

**목표**

1. 조립된 학습 데이터셋을 **불변·content-addressed** 객체로 GCS에 게시한다.
2. 날짜와 FeatureService로 최신 스냅샷을 찾을 수 있는 **포인터**를 둔다.
3. 학습이 스냅샷 URI를 1급 입력으로 받아 **재조립 없이** 소비하되, 바이트
   동일성과 커버리지 게이트를 재검증한다.

**비목표**

- Airflow DAG 배선(Job spec에 환경변수 주입, Task 분리)은 `SKYAHO/Autoresearch-airflow`
  소유다. 별도 이슈로 낸다.
- dev/prod 네임스페이스 분리. `FEAST_ENV`가 `src/` 어디에도 없는 현재, 이 spec이
  독자적으로 dev/prod 개념을 발명하면 이후 Feast 환경 분리 작업과 정의가 어긋난다.
  §7의 운영 계약으로 대신한다.
- 별도 조회 CLI. by-date 포인터가 사람이 읽는 JSON이라 `gsutil cat`으로 충분하다.

## 3. 저장 계약

### 3.1 레이아웃

```
gs://<root>/by-hash/<dataset_sha256>/training_dataset.csv
gs://<root>/by-hash/<dataset_sha256>/snapshot_manifest.json
gs://<root>/by-date/dt=<events_end_date>/<feature_service>.json
```

`<dataset_sha256>`은 **full 64자**다. 잘라 쓰면 "충돌 확률이 충분히 낮은가"를
나중에 누가 다시 계산해야 하는데, 경로 길이가 제약이 아니므로 자를 이유가 없다.

### 3.2 write-once

업로드는 `if_generation_match=0` 전제조건으로 보낸다. 이미 있으면 412가 오고,
주소가 곧 내용 해시이므로 **내용이 같음이 보장된다 — no-op으로 흡수한다.**
덮어쓰는 경로를 코드에 두지 않는다.

### 3.3 by-date 포인터

```json
{
  "dataset_sha256": "…64자…",
  "uri": "gs://<root>/by-hash/<sha>/",
  "events_start_date": "2026-07-26",
  "events_end_date": "2026-08-01",
  "feature_service": "ctr_training_v1",
  "registry_generation": "1722…",
  "published_at": "2026-08-04T05:00:00Z",
  "previous": [{"dataset_sha256": "…", "published_at": "…"}]
}
```

- 같은 `dt`·같은 FeatureService를 다시 조립해 sha가 달라지면(늦게 도착한 이벤트,
  registry 갱신) 포인터는 **최신을 가리키도록 갱신**한다. `by-hash/`는 불변이고
  재현은 항상 sha 주소로 하므로, 포인터 이동이 과거 학습을 깨뜨리지 않는다.
- 갱신은 read-modify-write + `if_generation_match=<현재 generation>`. 경합하면
  다시 읽고 재시도한다.
- `previous`는 **최근 10개로 캡**한다. 무한히 자라면 매 갱신이 전체를 읽고 쓰는
  비용이 계속 커진다.

## 4. `TrainingSnapshotManifest` 확장

`training_provenance.py`의 기존 모델에 필드 **하나**를 더한다.

```python
# 기존 v1 snapshot manifest JSON은 이 필드가 없으므로 None으로 읽는다.
# 재사용 학습(--dataset-uri)은 None이면 커버리지 게이트를 검증할 수 없어 거부한다.
spine_usable_days: NonNegativeInt | None = None
```

**이 필드는 스키마 레벨에서 optional이다(no-default 필수 필드가 아니다).**
설계 논의 중 "필수로 넣는다"고 서술한 적이 있으나 그 판단은 철회한다. 근거:

- 저장소 관례가 하위호환 Optional이다 — `TrainingSplitManifest.experiment_plan_receipt`
  (`training_provenance.py:140-142`)가 같은 주석과 함께 `| None = None`으로 들어와 있다.
- 필수로 만들면 이 필드와 무관한 기존 소비자(`training_comparison.py`,
  `degradation_eval.py`)와 픽스처가 스키마 변경에 얽힌다.

**강제는 스키마가 아니라 소비 시점에서 한다.** 재사용 학습이 `spine_usable_days is
None`이고 적용 `min_days > 0`이면 **모델 fit 전에 거부**한다. 조용히 빠지지 않는다.

별도 모델로 분리하는 안은 채택하지 않는다. coverage는 그 스냅샷 자체의 속성인데
파일을 쪼개면 by-hash 밑에 객체가 셋이 되고, write-once가 객체 단위라 **manifest만
올라가고 coverage가 빠진 상태가 성립**한다 — 둘의 정합성을 아무것도 보장하지 못한다.

**타입 주의**: 같은 개념이 두 곳에서 타입이 다르다.

| 위치 | 타입 | 값 |
|---|---|---|
| `SpineCoverage.as_lineage_params()` (`build_training_dataset.py:263`) | `str` | `str(len(self.usable_days))` — MLflow 파라미터는 문자열만 받는다 |
| `TrainingSnapshotManifest.spine_usable_days` | `int \| None` | `len(coverage.usable_days)` |

이름을 일부러 일치시켰으므로(`spine_usable_days`) 타입만 다르다는 점을 구현 시
혼동하지 않는다.

## 5. 새 모듈 `src/pipeline/training_snapshot_store.py`

- **책임**: content-addressed 스냅샷의 GCS 게시·다운로드, 레이아웃과 write-once
  의미론.
- **비책임**: 조립(`build_training_dataset`), 학습(`train`), manifest 형식
  (`training_provenance`).
- GCS client는 기존 `_download_pinned_registry`(`build_training_dataset.py:519-523`)와
  같은 `client: object | None = None` 주입 패턴을 따른다 — 테스트가 monkeypatch로
  대체하는 저장소 관행 그대로다.
- **재시도 단위는 게시 호출 전체**(CSV + manifest + 포인터)다. write-once가 412를
  no-op으로 흡수하므로, CSV만 올라간 상태에서 재시도하면 manifest부터 자연스럽게
  이어진다. 이 의도를 모듈 docstring에 명시한다.

## 6. 게시 경로

### 6.1 게시는 `main()`에서 한다 — `_assemble_via_feast`는 무변경

`_assemble_via_feast`가 리턴한 시점에 CSV와 sidecar는 디스크에 유효하게 존재하고,
`main()`은 `output_path`를 이미 안다. 게시는 그 둘을 읽어 올리는 순수한 후속
동작이라 조립 내부에 있을 이유가 없다.

- `_assemble_via_feast` — **시그니처·반환 무변경.** 이 함수를 직접 부르는 테스트가
  `test_build_training_dataset_feast_path.py`에만 18곳이고, 그중 `:336`은 반환값을
  받아 `coverage.requested_days`를 어서션한다. 게시를 안쪽에 두면 이 픽스처들이
  전부 흔들린다.
- `main()` — 반환만 `SpineCoverage` → `AssemblyOutcome(coverage, snapshot_uri)`.
  `train.py`의 `TrainingOutcome`과 같은 패턴이다.

`main()` 호출부 4곳 중 반환값을 소비하는 것은 `cli.py:445` 하나다
(`cli.py:115`, `degradation_eval.py:759`, `:806`은 버린다).

### 6.2 루트 해석 — 환경변수는 `cli.py`에서만 읽는다

`main(snapshot_root: str | None = None)`은 **명시적으로 받은 경우에만** 게시한다.
`TRAINING_SNAPSHOT_ROOT` fallback은 `cli.py`에서만 해석한다.

이 분리는 실제 오염을 막는다. `degradation_eval.py`는 `main()`을 두 번 부르는데,
`:806`은 **horizon 평가일마다 도는 루프**(`events_start=events_end=date_str`)다.
`main()`이 환경변수를 직접 읽었다면 열화 측정 한 번에 평가일 수만큼 by-date
포인터가 갱신되며 그날의 prod 포인터를 덮어쓰고, `previous` 10개 캡까지 밀려나
흔적도 남지 않는다. 아무것도 넘기지 않는 `degradation_eval`은 이 설계에서
**구조적으로 게시 경로에 들어가지 못한다.**

루트 미지정 시에는 stdout에 한 줄 남긴다:
`[게시 없음] snapshot root 미지정 — 로컬에만 저장`.

### 6.3 실험 조립은 포인터를 쓰지 않는다 (코드 가드)

`--snapshot-root`가 켜져 있어도 **실험 조립이면 by-hash에만 올리고 by-date
포인터는 건드리지 않는다.** 누가 실험 스크립트에 루트를 켜도 prod 포인터가
오염되지 않는다.

판별은 #454가 이미 가진 신호를 쓴다. 다만 그 신호는 지금 predicate가 아니다 —
`require_explicit_experiment_output`(`build_training_dataset.py:443-465`)은
"실험 조립인데 경로 미지정이면 raise"하는 **부수효과 가드**이고, 판정
`experiment_service = feature_service is not None and feature_service != DEFAULT_SERVICE`는
함수 안에 갇혀 있다. 이대로 재사용하면 같은 조건식이 두 벌 생기고, 나중에
`DEFAULT_SERVICE` 판별 기준이 바뀔 때 한쪽만 고치는 실수가 난다.

**따라서 predicate를 추출한다:**

```python
def is_experiment_assembly(
    *, feature_service: str | None, extra_features: Sequence[str] | None
) -> bool: ...
```

`require_explicit_experiment_output`과 게시 게이팅이 **같은 함수를 호출**한다.
이 추출은 동작 변경이 없는 순수 구조 변경이므로 CLAUDE.md Core Rules("구조 변경과
동작 변경은 분리한다")에 따라 **별도 커밋**으로 낸다.

### 6.4 게시 실패는 조립 실패다

3회 지수 백오프 후에도 실패하면 조립 전체를 실패시킨다. 게시가 이 작업의 목적
자체라, 실패했는데 학습이 계속되면 "스냅샷이 있다"는 전제로 짜인 하위 소비자가
나중에 없는 URI를 참조한다. GCS 일시 장애가 파이프라인을 막는다는 뜻이며,
**의도한 트레이드오프**다.

단, 실패 메시지에 **로컬 CSV는 유효하게 써졌고 게시만 실패했다**는 사실과 로컬
경로를 반드시 싣는다 — Pod가 살아있는 동안 사람이 손으로 건질 수 있어야 한다.

## 7. 소비 경로

### 7.1 `--dataset-uri`

`train-model`과 `run-pipeline`이 `--dataset-uri gs://<root>/by-hash/<sha>/`를
1급 입력으로 받는다.

- **상호배타**: `train-model`은 `--dataset-uri` ↔ `--data-path`(`cli.py:249`),
  `run-pipeline`은 `--dataset-uri` ↔ `--dataset-path`(`cli.py:349`)다. 옵션 이름이
  두 명령에서 다르므로 구현 시 혼동하지 않는다. `run-pipeline`에서는
  `--dataset-uri` ↔ `--events-start-date`/`--events-end-date`도 배타다 — 스냅샷이
  구간을 이미 확정했는데 다른 구간을 받으면 둘 중 무엇이 진짜인지 코드가 답할 수
  없다. `resolve_training_seeds`의 "명시 조합 아니면 raise" 관례를 따른다.
- **파일명 복원**: GCS에는 `snapshot_manifest.json`으로 두지만
  `snapshot_manifest_path()`(`training_provenance.py:182-184`)는
  `f"{dataset_path}.snapshot.json"`을 기대한다. 임시 디렉터리에
  `training_dataset.csv` + `training_dataset.csv.snapshot.json`으로 내려받으면
  **기존 `load_training_snapshot_manifest()`가 그대로 재사용**되고 byte·schema·
  `row_count` 검증이 따라온다.
- **교차검증**: URI 경로의 `<sha>`와 `manifest.dataset_sha256`이 일치해야 한다.
  불일치면 fit 전 중단 — content-addressing을 실제로 신뢰할 근거가 여기서 생긴다.
- **커버리지 게이트**: `require_spine_coverage`(`build_training_dataset.py:380`)의
  술어 `len(coverage.usable_days) < min_days`를 `spine_usable_days < min_days`로
  재현한다. `None`이고 `min_days > 0`이면 거부한다(§4).

### 7.2 `run-pipeline --dataset-uri`

`[1/4] build-features`를 건너뛰고, MLflow lineage 파라미터(`events_start_date`,
`events_end_date`, `feature_service`, `feast_registry_path`)를 **manifest에서**
채운다. 재조립을 하지 않아 조립 반환값이 없지만 sidecar가 그 값을 전부 갖고 있다.

## 8. 운영 계약 (코드가 강제하지 못하는 부분)

**`--snapshot-root`/`TRAINING_SNAPSHOT_ROOT`는 prod 재학습 경로에만 세팅한다.
실험·dev 파이프라인은 이 옵션을 세팅하지 않는다.**

포인터를 쓰는 주체가 하나면 "두 프로세스가 같은 좌표에 쓴다"는 상황 자체가
성립하지 않는다. read-modify-write 재시도는 같은 prod DAG의 재시도·재실행 겹침
대비로 여전히 필요하지만, 이종 환경 간 경합이라는 층위가 통째로 사라진다.

dev 실험이 backfill을 짧게(≈7일) 쓰는 이유는 빠른 가설 검증이지 재사용 최적화가
아니다. 재사용 캐싱이 필요한 쪽은 반복 조립 비용이 큰 prod 재학습과 rolling-origin
평가다.

**한계를 명시한다**: `FEAST_ENV`가 코드에 없으므로 "이건 prod 경로다"를 코드가
판별할 수 없다. §6.3의 코드 가드는 **실험 조립**(비기본 FeatureService 또는
`extra_features`)만 막는다. 실험 조립이 아닌 dev 백필은 규율의 영역이며,
`Autoresearch-airflow`에서 prod 재학습 DAG의 Job spec에만 환경변수를 넣는 방식으로
지켜져야 한다.

## 9. 영향 컴포넌트

**신규**
- `src/pipeline/training_snapshot_store.py`
- `tests/test_training_snapshot_store.py`

**수정**
- `src/pipeline/training_provenance.py` — `spine_usable_days` optional 필드, 포인터 모델
- `src/pipeline/build_training_dataset.py` — `is_experiment_assembly` 추출,
  `main()` 게시·반환 타입(`AssemblyOutcome`)
- `src/pipeline/train.py` — `--dataset-uri` 다운로드·검증
- `src/cli.py` — `--snapshot-root`/`--dataset-uri` 인자, 환경변수 해석,
  `run-pipeline` 조립 스킵

**테스트 (기존 픽스처 갱신 대상)**
- `tests/test_cli.py`, `tests/test_degradation_eval_hold.py`,
  `tests/test_degradation_eval_detection.py` — `TrainingSnapshotManifest` 생성자를
  직접 호출한다. `spine_usable_days`가 optional이라 **스키마 차원에서는 무변경**이며,
  재사용 경로 동작을 검증하는 케이스에서만 값을 채운다.
- `tests/test_build_training_dataset*.py`, `tests/test_pipeline_train.py`

**문서 (같은 PR에서 갱신 — CLAUDE.md Core Rules)**
- `.env.example` — `TRAINING_SNAPSHOT_ROOT`
- `README.md`, `.claude/docs/agent-project-reference.md`
- `docs/specs/2026-07-13-public-batch-execution-contract.md` — `--snapshot-root`, `--dataset-uri`
- `docs/guides/training-dataset.md` — 스냅샷 절 (최초 문제 제기였던 "학습 데이터가
  어디 저장되는가"의 문서 갭이 여기서 메워진다)
- `docs/README.md` — 인덱스 등재

## 10. 완료 조건

1. `--snapshot-root` 지정 시 조립이 by-hash 객체 2개와 by-date 포인터를 게시하고,
   미지정 시 게시하지 않는다.
2. 같은 입력을 재조립하면 같은 주소에 write-once no-op으로 흡수된다.
3. 실험 조립은 루트가 켜져 있어도 by-date 포인터를 기록하지 않는다.
4. 게시 실패 시 조립이 실패하고, 메시지에 로컬 CSV 경로가 있다.
5. `train-model --dataset-uri`가 스냅샷을 내려받아 sha·schema·row_count를 재검증하고,
   불일치 또는 커버리지 미검증(`spine_usable_days is None` + `min_days>0`) 시 fit 전에
   중단한다.
6. `run-pipeline --dataset-uri`가 조립을 건너뛰고 lineage를 manifest에서 채운다.
7. 상호배타 인자 조합이 거부된다.
8. `is_experiment_assembly` 추출이 동작 변경 없는 별도 커밋으로 들어간다.
9. §9의 문서가 같은 PR에서 갱신된다.
