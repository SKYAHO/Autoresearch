import http from "k6/http";
import { check } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const DEFAULT_BASE_URL =
  "http://autoresearch-serving.autoresearch.svc.cluster.local:8000";
const FIXTURE_USER_ID = "loadtest-user-001";
const LAST_VIDEO_ID = "loadtest-video-200";
const CANARY_CANDIDATE_COUNTS = [24, 200];
const ALLOWED_CANDIDATE_COUNTS = new Set(CANARY_CANDIDATE_COUNTS);
const ALLOWED_VUS = new Set([1, 2, 4, 8]);

function readPositiveInteger(name, fallback) {
  const raw = __ENV[name] || fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer; received ${raw}`);
  }
  return value;
}

const baseUrl = (__ENV.BASE_URL || DEFAULT_BASE_URL).replace(/\/$/, "");
const candidateCount = readPositiveInteger("CANDIDATE_COUNT", "24");
const vus = readPositiveInteger("VUS", "1");
const warmupSeconds = readPositiveInteger("WARMUP_SECONDS", "60");
const measureSeconds = readPositiveInteger("MEASURE_SECONDS", "300");

if (!ALLOWED_CANDIDATE_COUNTS.has(candidateCount)) {
  throw new Error("CANDIDATE_COUNT must be one of 24 or 200.");
}
if (!ALLOWED_VUS.has(vus)) {
  throw new Error("VUS must be one of 1, 2, 4, or 8.");
}

const fixtureVersion = __ENV.FIXTURE_VERSION || "rerank-v1";
const benchmarkLabel = __ENV.BENCHMARK_LABEL || "baseline";
const servingImageRef = __ENV.SERVING_IMAGE_REF || "unknown";
const servingGitSha = __ENV.SERVING_GIT_SHA || "unknown";
const allVideoIds = Array.from(
  { length: 200 },
  (_, index) => `loadtest-video-${String(index + 1).padStart(3, "0")}`,
);

if (allVideoIds[allVideoIds.length - 1] !== LAST_VIDEO_ID) {
  throw new Error("The fixed load-test video fixture must contain 200 ordered IDs.");
}

const selectedVideoIds = allVideoIds.slice(0, candidateCount);
const measurementDuration = new Trend("rerank_measure_duration_seconds");
const measurementRequests = new Counter("rerank_measure_requests");
const measurementFailure = new Rate("rerank_measure_failure");
const measurementStatusCode200 = new Counter("rerank_measure_status_code_200");
const measurementStatusCode422 = new Counter("rerank_measure_status_code_422");
const measurementStatusCode500 = new Counter("rerank_measure_status_code_500");
const measurementStatusCode503 = new Counter("rerank_measure_status_code_503");
const measurementStatusCodeOther = new Counter("rerank_measure_status_code_other");

export const options = {
  scenarios: {
    warmup: {
      executor: "constant-vus",
      exec: "warmup",
      vus,
      duration: String(warmupSeconds) + "s",
      gracefulStop: "0s",
    },
    measure: {
      executor: "constant-vus",
      exec: "measure",
      vus,
      startTime: String(warmupSeconds) + "s",
      duration: String(measureSeconds) + "s",
      gracefulStop: "0s",
    },
  },
  thresholds: {
    rerank_measure_failure: ["rate<0.01"],
  },
};

function validateResponse(response, requestedVideoIds) {
  let payload;
  try {
    payload = response.json();
  } catch (_) {
    payload = null;
  }

  const items = payload && Array.isArray(payload.items) ? payload.items : [];
  const itemCountIsExact = items.length === requestedVideoIds.length;
  const itemOrderIsExact =
    itemCountIsExact &&
    items.every((item, index) => item && item.video_id === requestedVideoIds[index]);
  const hasOneModelId =
    itemCountIsExact &&
    items.every(
      (item) =>
        item &&
        typeof item.model_id === "string" &&
        item.model_id.trim().length > 0,
    ) &&
    new Set(items.map((item) => item.model_id)).size === 1;
  const scoresAreFinite =
    itemCountIsExact &&
    items.every(
      (item) =>
        item &&
        typeof item.ctr_score === "number" &&
        Number.isFinite(item.ctr_score),
    );

  return {
    httpOk: response.status === 200,
    itemCountIsExact,
    itemOrderIsExact,
    hasOneModelId,
    scoresAreFinite,
  };
}

function recordMeasurementStatus(statusCode) {
  switch (statusCode) {
    case 200:
      measurementStatusCode200.add(1);
      break;
    case 422:
      measurementStatusCode422.add(1);
      break;
    case 500:
      measurementStatusCode500.add(1);
      break;
    case 503:
      measurementStatusCode503.add(1);
      break;
    default:
      measurementStatusCodeOther.add(1);
  }
}

function postAndValidate(requestedVideoIds, recordMeasurement) {
  const response = http.post(
    `${baseUrl}/rerank`,
    JSON.stringify({ user_id: FIXTURE_USER_ID, video_ids: requestedVideoIds }),
    { headers: { "Content-Type": "application/json" } },
  );
  const validation = validateResponse(response, requestedVideoIds);
  const isValid = Object.values(validation).every(Boolean);

  check(response, {
    "rerank returns HTTP 200": () => validation.httpOk,
    "rerank returns the requested item count": () => validation.itemCountIsExact,
    "rerank preserves requested item order": () => validation.itemOrderIsExact,
    "rerank returns one non-empty model ID": () => validation.hasOneModelId,
    "rerank returns finite CTR scores": () => validation.scoresAreFinite,
  });

  if (recordMeasurement) {
    // k6 response timing은 milliseconds다. isTime flag 없는 Trend에는 seconds로 기록한다.
    measurementDuration.add(response.timings.duration / 1000);
    measurementRequests.add(1);
    measurementFailure.add(!isValid);
    recordMeasurementStatus(response.status);
  }

  return isValid;
}

export function setup() {
  const canaryFailures = CANARY_CANDIDATE_COUNTS.filter(
    (count) => !postAndValidate(allVideoIds.slice(0, count), false),
  );
  if (canaryFailures.length > 0) {
    throw new Error(
      `Rerank canary failed for candidate counts: ${canaryFailures.join(", ")}.`,
    );
  }
}

export function warmup() {
  postAndValidate(selectedVideoIds, false);
}

export function measure() {
  postAndValidate(selectedVideoIds, true);
}

export function handleSummary(data) {
  return {
    stdout: JSON.stringify({
      metadata: {
        base_url: baseUrl,
        candidate_count: candidateCount,
        vus,
        warmup_seconds: warmupSeconds,
        measure_seconds: measureSeconds,
        fixture_version: fixtureVersion,
        benchmark_label: benchmarkLabel,
        serving_image_ref: servingImageRef,
        serving_git_sha: servingGitSha,
      },
      data: { metrics: data.metrics },
    }),
  };
}
