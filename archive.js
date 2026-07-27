"use strict";

var CATEGORY_SOURCES = [
  { key: "application", label: "Application" },
  {
    key: "airflow",
    label: "Airflow",
    url: "https://skyaho.github.io/Autoresearch-airflow/archive.json",
    base: "https://skyaho.github.io/Autoresearch-airflow/",
  },
  {
    key: "infra",
    label: "Infra",
    url: "https://skyaho.github.io/Autoresearch-infra/archive.json",
    base: "https://skyaho.github.io/Autoresearch-infra/",
  },
];
var FETCH_TIMEOUT_MS = 6000;
var CATEGORY_LABELS = CATEGORY_SOURCES.reduce(function (labels, source) {
  labels[source.key] = source.label;
  return labels;
}, {});

function matchesArchiveEntry(entry, rawQuery) {
  var query = String(rawQuery || "").trim().toLocaleLowerCase("ko-KR");
  if (!query) return true;
  var haystack = [String(entry.number), entry.title, entry.author]
    .join(" ")
    .toLocaleLowerCase("ko-KR");
  return haystack.indexOf(query) !== -1;
}

function matchesCategory(entry, selectedKey) {
  return selectedKey === "all" || entry.category === selectedKey;
}

function shallowCopy(entry) {
  var copy = {};
  for (var key in entry) {
    if (Object.prototype.hasOwnProperty.call(entry, key)) copy[key] = entry[key];
  }
  return copy;
}

function tagCategory(entries, categoryKey) {
  return entries.map(function (entry) {
    var tagged = shallowCopy(entry);
    tagged.category = categoryKey;
    return tagged;
  });
}

function absolutizeReportUrl(entries, baseUrl) {
  return entries.map(function (entry) {
    var absolute = shallowCopy(entry);
    absolute.report_url = baseUrl + entry.report_url;
    return absolute;
  });
}

function mergeAndSortReports(reportArrays) {
  var merged = [].concat.apply([], reportArrays);
  return merged.slice().sort(function (a, b) {
    if (a.merged_at === b.merged_at) return b.number - a.number;
    return a.merged_at < b.merged_at ? 1 : -1;
  });
}

function appendTextElement(parent, tagName, className, text) {
  var element = document.createElement(tagName);
  if (className) element.className = className;
  element.textContent = text;
  parent.appendChild(element);
  return element;
}

function createReportCard(entry) {
  var article = document.createElement("article");
  article.className = "report-card";

  var heading = document.createElement("div");
  heading.className = "card-heading";
  appendTextElement(heading, "span", "pr-number", "#" + entry.number);
  appendTextElement(
    heading,
    "span",
    "pr-category",
    CATEGORY_LABELS[entry.category] || entry.category
  );
  appendTextElement(heading, "h2", "pr-title", entry.title);
  article.appendChild(heading);

  var meta = document.createElement("p");
  meta.className = "report-meta";
  appendTextElement(meta, "span", "", "@" + entry.author);
  appendTextElement(
    meta,
    "time",
    "",
    new Date(entry.merged_at).toLocaleDateString("ko-KR", {
      year: "numeric",
      month: "long",
      day: "numeric",
      timeZone: "UTC",
    })
  ).setAttribute("datetime", entry.merged_at);
  article.appendChild(meta);

  var summary = document.createElement("ol");
  summary.className = "summary-list";
  entry.summary_ko.forEach(function (line) {
    appendTextElement(summary, "li", "", line);
  });
  article.appendChild(summary);

  var link = document.createElement("a");
  link.className = "report-link";
  link.href = entry.report_url;
  link.textContent = "리포트 보기";
  link.setAttribute("aria-label", "PR #" + entry.number + " 리포트 보기");
  article.appendChild(link);
  return article;
}

function buildReportCardSafely(entry) {
  try {
    return createReportCard(entry);
  } catch (error) {
    console.warn(
      "[archive] skipped malformed entry (number=" +
        (entry && entry.number) +
        ", category=" +
        (entry && entry.category) +
        "):",
      error
    );
    return null;
  }
}

function initializeArchive() {
  var dataElement = document.getElementById("archive-data");
  var list = document.getElementById("report-list");
  var count = document.getElementById("report-count");
  var search = document.getElementById("archive-search");
  var empty = document.getElementById("empty-state");
  var tabs = document.getElementById("category-tabs");
  if (!dataElement || !list || !count || !search || !empty || !tabs) return;

  var payload;
  try {
    payload = JSON.parse(dataElement.textContent);
  } catch (error) {
    empty.hidden = false;
    empty.textContent = "아카이브 데이터를 읽을 수 없습니다.";
    return;
  }

  var reports = tagCategory(
    Array.isArray(payload.reports) ? payload.reports : [],
    "application"
  );
  var selectedCategory = "all";
  var pendingCategories = {};
  var failedCategories = {};

  function render(rawQuery) {
    var visible = reports
      .filter(function (entry) {
        return matchesCategory(entry, selectedCategory);
      })
      .filter(function (entry) {
        return matchesArchiveEntry(entry, rawQuery);
      });
    list.replaceChildren();
    var renderedCount = 0;
    visible.forEach(function (entry) {
      var card = buildReportCardSafely(entry);
      if (!card) return;
      list.appendChild(card);
      renderedCount += 1;
    });
    count.textContent = String(renderedCount);
    empty.hidden = renderedCount !== 0;
    if (renderedCount === 0) {
      if (failedCategories[selectedCategory]) {
        empty.textContent = "이 카테고리를 불러오지 못했습니다.";
      } else if (pendingCategories[selectedCategory]) {
        empty.textContent = "불러오는 중입니다.";
      } else {
        empty.textContent = rawQuery
          ? "검색 결과가 없습니다."
          : "아직 등록된 merge 리포트가 없습니다.";
      }
    }
  }

  function loadExternalCategory(source) {
    pendingCategories[source.key] = true;
    var controller =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    var timeoutId = controller
      ? setTimeout(function () {
          controller.abort();
        }, FETCH_TIMEOUT_MS)
      : null;
    fetch(source.url, controller ? { signal: controller.signal } : {})
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (externalPayload) {
        if (timeoutId) clearTimeout(timeoutId);
        delete pendingCategories[source.key];
        delete failedCategories[source.key];
        var entries = Array.isArray(externalPayload.reports)
          ? externalPayload.reports
          : [];
        entries = tagCategory(entries, source.key);
        entries = absolutizeReportUrl(entries, source.base);
        reports = mergeAndSortReports([reports, entries]);
        render(search.value);
      })
      .catch(function (error) {
        if (timeoutId) clearTimeout(timeoutId);
        delete pendingCategories[source.key];
        failedCategories[source.key] = true;
        console.warn(
          "[archive] failed to load category '" + source.key + "':",
          error
        );
        render(search.value);
      });
  }

  search.addEventListener("input", function () {
    render(search.value);
  });

  var tabButtons = tabs.querySelectorAll("[data-category]");
  tabButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      selectedCategory = button.getAttribute("data-category");
      tabButtons.forEach(function (other) {
        var isSelected = other === button;
        other.setAttribute("aria-selected", String(isSelected));
        other.classList.toggle("is-selected", isSelected);
      });
      render(search.value);
    });
  });

  var externalSources = CATEGORY_SOURCES.filter(function (source) {
    return !!source.url;
  });
  // 외부 카테고리는 아직 fetch가 끝나지 않았음을 초기 렌더 이전에 표시해야
  // "등록된 리포트 없음" 문구가 잘못 노출되지 않는다(#372).
  externalSources.forEach(function (source) {
    pendingCategories[source.key] = true;
  });

  render("");

  externalSources.forEach(loadExternalCategory);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    matchesArchiveEntry: matchesArchiveEntry,
    matchesCategory: matchesCategory,
    tagCategory: tagCategory,
    absolutizeReportUrl: absolutizeReportUrl,
    mergeAndSortReports: mergeAndSortReports,
    createReportCard: createReportCard,
    buildReportCardSafely: buildReportCardSafely,
  };
}
if (typeof window !== "undefined") {
  window.matchesArchiveEntry = matchesArchiveEntry;
}
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeArchive);
  } else {
    initializeArchive();
  }
}
