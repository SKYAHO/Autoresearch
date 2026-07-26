"use strict";

function matchesArchiveEntry(entry, rawQuery) {
  var query = String(rawQuery || "").trim().toLocaleLowerCase("ko-KR");
  if (!query) return true;
  var haystack = [String(entry.number), entry.title, entry.author]
    .join(" ")
    .toLocaleLowerCase("ko-KR");
  return haystack.indexOf(query) !== -1;
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

function initializeArchive() {
  var dataElement = document.getElementById("archive-data");
  var list = document.getElementById("report-list");
  var count = document.getElementById("report-count");
  var search = document.getElementById("archive-search");
  var empty = document.getElementById("empty-state");
  if (!dataElement || !list || !count || !search || !empty) return;

  var payload;
  try {
    payload = JSON.parse(dataElement.textContent);
  } catch (error) {
    empty.hidden = false;
    empty.textContent = "아카이브 데이터를 읽을 수 없습니다.";
    return;
  }

  var reports = Array.isArray(payload.reports) ? payload.reports : [];
  function render(rawQuery) {
    var visible = reports.filter(function (entry) {
      return matchesArchiveEntry(entry, rawQuery);
    });
    list.replaceChildren();
    visible.forEach(function (entry) {
      list.appendChild(createReportCard(entry));
    });
    count.textContent = String(visible.length);
    empty.hidden = visible.length !== 0;
    empty.textContent = rawQuery
      ? "검색 결과가 없습니다."
      : "아직 등록된 merge 리포트가 없습니다.";
  }

  search.addEventListener("input", function () {
    render(search.value);
  });
  render("");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { matchesArchiveEntry: matchesArchiveEntry };
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
