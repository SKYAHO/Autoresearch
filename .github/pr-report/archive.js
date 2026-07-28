"use strict";

var CATEGORY_SOURCES = [
  { key: "application", label: "Application", repoFull: "Autoresearch", light: "#1f7a5c", dark: "#4fbf95" },
  {
    key: "airflow",
    label: "Airflow",
    repoFull: "Autoresearch-airflow",
    light: "#5a4cc0",
    dark: "#a394f2",
    url: "https://skyaho.github.io/Autoresearch-airflow/archive.json",
    base: "https://skyaho.github.io/Autoresearch-airflow/",
  },
  {
    key: "infra",
    label: "Infra",
    repoFull: "Autoresearch-infra",
    light: "#a9701f",
    dark: "#e0a85c",
    url: "https://skyaho.github.io/Autoresearch-infra/archive.json",
    base: "https://skyaho.github.io/Autoresearch-infra/",
  },
];
var FETCH_TIMEOUT_MS = 6000;
var CATEGORY_LABELS = CATEGORY_SOURCES.reduce(function (labels, source) {
  labels[source.key] = source.label;
  return labels;
}, {});
var CATEGORY_META = CATEGORY_SOURCES.reduce(function (meta, source) {
  meta[source.key] = source;
  return meta;
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

function fmtDate(iso) {
  var datePart = String(iso).slice(0, 10);
  var segments = datePart.split("-");
  var y = parseInt(segments[0], 10);
  var m = parseInt(segments[1], 10);
  var d = parseInt(segments[2], 10);
  return y + "년 " + m + "월 " + d + "일";
}

function highlightParts(text, rawQuery) {
  var source = String(text == null ? "" : text);
  var query = String(rawQuery || "").trim();
  if (!query) return [{ text: source, hit: false }];
  var lower = source.toLocaleLowerCase("ko-KR");
  var nq = query.toLocaleLowerCase("ko-KR");
  var out = [];
  var i = 0;
  while (i < source.length) {
    var at = lower.indexOf(nq, i);
    if (at === -1) {
      out.push({ text: source.slice(i), hit: false });
      break;
    }
    if (at > i) out.push({ text: source.slice(i, at), hit: false });
    out.push({ text: source.slice(at, at + nq.length), hit: true });
    i = at + nq.length;
  }
  return out.length ? out : [{ text: source, hit: false }];
}

function sortEntries(entries, sortMode) {
  return entries.slice().sort(function (a, b) {
    if (sortMode === "repo" && a.category !== b.category) {
      return a.category < b.category ? -1 : 1;
    }
    if (a.merged_at === b.merged_at) return b.number - a.number;
    return a.merged_at < b.merged_at ? 1 : -1;
  });
}

function groupEntries(sortedEntries, sortMode) {
  var groups = [];
  sortedEntries.forEach(function (entry) {
    var key = sortMode === "repo" ? entry.category : String(entry.merged_at).slice(0, 10);
    var last = groups[groups.length - 1];
    if (last && last.key === key) {
      last.items.push(entry);
      return;
    }
    groups.push({ key: key, items: [entry] });
  });
  return groups;
}

function pickSelectedId(sortedEntries, currentId) {
  var stillPresent = sortedEntries.some(function (entry) {
    return entry.number === currentId;
  });
  if (stillPresent) return currentId;
  return sortedEntries.length ? sortedEntries[0].number : null;
}

function sanitizeEntry(entry) {
  var isValid =
    !!entry &&
    typeof entry.number === "number" &&
    typeof entry.title === "string" &&
    typeof entry.author === "string" &&
    typeof entry.merged_at === "string" &&
    Array.isArray(entry.summary_ko);
  if (!isValid) {
    console.warn(
      "[archive] dropped malformed entry (number=" + (entry && entry.number) + "):",
      entry
    );
    return null;
  }
  return entry;
}

function appendHighlighted(parentEl, text, query) {
  highlightParts(text, query).forEach(function (part) {
    if (part.hit) {
      var mark = document.createElement("mark");
      mark.textContent = part.text;
      parentEl.appendChild(mark);
    } else {
      parentEl.appendChild(document.createTextNode(part.text));
    }
  });
}

function buildRailItem(entry, query, isSelected, color) {
  var item = document.createElement("div");
  item.className = "rail-item";
  if (isSelected) {
    var bar = document.createElement("span");
    bar.className = "bar";
    bar.style.background = color;
    item.appendChild(bar);
  }
  var num = document.createElement("span");
  num.className = "num";
  num.textContent = "#" + entry.number;
  item.appendChild(num);

  var body = document.createElement("div");
  var titleEl = document.createElement("div");
  titleEl.className = "title";
  appendHighlighted(titleEl, entry.title, query);
  body.appendChild(titleEl);

  var meta = document.createElement("div");
  meta.className = "item-meta";
  var dot = document.createElement("span");
  dot.className = "dot";
  dot.style.background = color;
  meta.appendChild(dot);
  var repoLabelEl = document.createElement("span");
  repoLabelEl.textContent = CATEGORY_LABELS[entry.category] || entry.category;
  meta.appendChild(repoLabelEl);
  var authorEl = document.createElement("span");
  authorEl.textContent = "@" + entry.author;
  meta.appendChild(authorEl);
  body.appendChild(meta);

  item.appendChild(body);
  return item;
}

function buildRailItemSafely(entry, query, isSelected, color) {
  try {
    return buildRailItem(entry, query, isSelected, color);
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

function buildDetailContent(entry, query, color, repoFull, onBack) {
  var inner = document.createElement("div");
  inner.className = "detail-inner";

  var backBtn = document.createElement("button");
  backBtn.type = "button";
  backBtn.className = "detail-back";
  backBtn.textContent = "← 목록으로";
  backBtn.addEventListener("click", onBack);
  inner.appendChild(backBtn);

  var metaRow = document.createElement("div");
  metaRow.className = "detail-meta";
  var tag = document.createElement("span");
  tag.className = "repo-tag";
  tag.style.color = color;
  tag.textContent = CATEGORY_LABELS[entry.category] || entry.category;
  metaRow.appendChild(tag);
  var numEl = document.createElement("span");
  numEl.className = "num";
  numEl.textContent = "#" + entry.number;
  metaRow.appendChild(numEl);
  var authorEl = document.createElement("span");
  authorEl.className = "author";
  authorEl.textContent = "@" + entry.author;
  metaRow.appendChild(authorEl);
  var dateEl = document.createElement("span");
  dateEl.className = "date";
  dateEl.textContent = fmtDate(entry.merged_at);
  metaRow.appendChild(dateEl);
  inner.appendChild(metaRow);

  var h2 = document.createElement("h2");
  appendHighlighted(h2, entry.title, query);
  inner.appendChild(h2);

  var bulletsWrap = document.createElement("div");
  bulletsWrap.className = "detail-bullets";
  entry.summary_ko.forEach(function (line) {
    var bullet = document.createElement("div");
    bullet.className = "bullet";
    var dot = document.createElement("span");
    dot.className = "dot";
    bullet.appendChild(dot);
    var p = document.createElement("p");
    appendHighlighted(p, line, query);
    bullet.appendChild(p);
    bulletsWrap.appendChild(bullet);
  });
  inner.appendChild(bulletsWrap);

  var foot = document.createElement("div");
  foot.className = "detail-foot";
  var repoFullEl = document.createElement("span");
  repoFullEl.className = "repo-full";
  repoFullEl.textContent = repoFull || "";
  foot.appendChild(repoFullEl);
  var link = document.createElement("a");
  link.href = entry.report_url;
  link.textContent = "리포트 전문 보기 →";
  foot.appendChild(link);
  inner.appendChild(foot);

  return inner;
}

function buildDetailContentSafely(entry, query, color, repoFull, onBack) {
  try {
    return buildDetailContent(entry, query, color, repoFull, onBack);
  } catch (error) {
    console.warn(
      "[archive] failed to render detail for entry (number=" +
        (entry && entry.number) +
        "):",
      error
    );
    return null;
  }
}

function initializeArchive() {
  var dataElement = document.getElementById("archive-data");
  var app = document.getElementById("app");
  var countNum = document.getElementById("count-num");
  var totalNum = document.getElementById("total-num");
  var repoStatsEl = document.getElementById("repo-stats");
  var qInput = document.getElementById("q");
  var authorSelect = document.getElementById("author-select");
  var sortSelect = document.getElementById("sort-select");
  var railList = document.getElementById("rail-list");
  var railStatus = document.getElementById("rail-status");
  var detail = document.getElementById("detail");
  var themeToggle = document.getElementById("theme-toggle");
  if (
    !dataElement || !app || !countNum || !totalNum || !repoStatsEl || !qInput ||
    !authorSelect || !sortSelect || !railList || !railStatus || !detail || !themeToggle
  ) {
    return;
  }

  var payload;
  try {
    payload = JSON.parse(dataElement.textContent);
  } catch (error) {
    var errEl = document.createElement("p");
    errEl.className = "rail-empty";
    errEl.textContent = "아카이브 데이터를 읽을 수 없습니다.";
    railList.appendChild(errEl);
    return;
  }

  var reports = tagCategory(Array.isArray(payload.reports) ? payload.reports : [], "application");
  var pendingCategories = {};
  var failedCategories = {};
  var state = { repo: "all", author: "all", sort: "recent", selectedId: null, dark: true };

  try {
    var stored = window.localStorage.getItem("archive-theme");
    if (stored === "light" || stored === "dark") {
      state.dark = stored === "dark";
    } else if (typeof matchMedia !== "undefined") {
      state.dark = matchMedia("(prefers-color-scheme: dark)").matches;
    }
  } catch (error) {
    /* localStorage unavailable — keep default */
  }

  function applyTheme() {
    document.documentElement.setAttribute("data-theme", state.dark ? "dark" : "light");
    themeToggle.textContent = state.dark ? "LIGHT" : "DARK";
  }

  function toggleTheme() {
    state.dark = !state.dark;
    try {
      window.localStorage.setItem("archive-theme", state.dark ? "dark" : "light");
    } catch (error) {
      /* localStorage unavailable — theme choice just won't persist */
    }
    applyTheme();
    render();
  }

  function categoryColor(key) {
    var meta = CATEGORY_META[key];
    if (!meta) return state.dark ? "#8fd3c0" : "#1f7a5c";
    return state.dark ? meta.dark : meta.light;
  }

  function renderRepoStats(valid) {
    repoStatsEl.textContent = "";
    CATEGORY_SOURCES.forEach(function (source) {
      var count = valid.filter(function (entry) {
        return entry.category === source.key;
      }).length;
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "repo-stat" + (state.repo === source.key ? " on" : "");
      var dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = categoryColor(source.key);
      btn.appendChild(dot);
      var label = document.createElement("span");
      label.textContent = source.label + " " + count;
      btn.appendChild(label);
      btn.addEventListener("click", function () {
        state.repo = state.repo === source.key ? "all" : source.key;
        render();
      });
      repoStatsEl.appendChild(btn);
    });
    if (state.repo !== "all") {
      var reset = document.createElement("button");
      reset.type = "button";
      reset.className = "repo-stat";
      reset.textContent = "필터 해제";
      reset.addEventListener("click", function () {
        state.repo = "all";
        render();
      });
      repoStatsEl.appendChild(reset);
    }
  }

  function renderAuthorOptions(valid) {
    var authors = ["all"].concat(
      Array.from(
        new Set(
          valid.map(function (entry) {
            return entry.author;
          })
        )
      ).sort()
    );
    authorSelect.textContent = "";
    authors.forEach(function (author) {
      var opt = document.createElement("option");
      opt.value = author;
      opt.textContent = author === "all" ? "작성자 전체" : "@" + author;
      authorSelect.appendChild(opt);
    });
    authorSelect.value = authors.indexOf(state.author) !== -1 ? state.author : "all";
  }

  function renderRailStatus() {
    var pendingKeys = Object.keys(pendingCategories);
    var failedKeys = Object.keys(failedCategories);
    if (!pendingKeys.length && !failedKeys.length) {
      railStatus.hidden = true;
      return;
    }
    var parts = [];
    if (pendingKeys.length) {
      parts.push(
        pendingKeys
          .map(function (key) {
            return CATEGORY_LABELS[key] || key;
          })
          .join(", ") + " 불러오는 중…"
      );
    }
    if (failedKeys.length) {
      parts.push(
        failedKeys
          .map(function (key) {
            return CATEGORY_LABELS[key] || key;
          })
          .join(", ") + " 불러오지 못했습니다"
      );
    }
    railStatus.hidden = false;
    railStatus.textContent = parts.join(" · ");
  }

  function closeMobileDetail() {
    detail.classList.remove("is-open");
  }

  function renderRailList(groups, query) {
    railList.textContent = "";
    if (!groups.length) {
      var empty = document.createElement("p");
      empty.className = "rail-empty";
      empty.textContent = query ? "검색 결과가 없습니다." : "아직 등록된 merge 리포트가 없습니다.";
      railList.appendChild(empty);
    } else {
      groups.forEach(function (group) {
        var labelEl = document.createElement("div");
        labelEl.className = "rail-group-label";
        labelEl.textContent =
          state.sort === "repo" ? CATEGORY_LABELS[group.key] || group.key : fmtDate(group.key + "T00:00:00Z");
        railList.appendChild(labelEl);
        group.items.forEach(function (entry) {
          var isSelected = entry.number === state.selectedId;
          var item = buildRailItemSafely(entry, query, isSelected, categoryColor(entry.category));
          if (!item) return;
          item.addEventListener("click", function () {
            state.selectedId = entry.number;
            render();
            if (typeof matchMedia !== "undefined" && matchMedia("(max-width: 760px)").matches) {
              detail.classList.add("is-open");
            }
          });
          railList.appendChild(item);
        });
      });
    }
    var tail = document.createElement("div");
    tail.className = "rail-list-tail";
    railList.appendChild(tail);
  }

  function renderDetail(sorted, query) {
    detail.textContent = "";
    var selected = null;
    for (var i = 0; i < sorted.length; i++) {
      if (sorted[i].number === state.selectedId) {
        selected = sorted[i];
        break;
      }
    }
    if (!selected) {
      var emptyDetail = document.createElement("div");
      emptyDetail.className = "detail-empty";
      emptyDetail.textContent = "선택된 리포트가 없습니다.";
      detail.appendChild(emptyDetail);
      return;
    }
    var meta = CATEGORY_META[selected.category];
    var inner = buildDetailContentSafely(
      selected,
      query,
      categoryColor(selected.category),
      meta ? meta.repoFull : "",
      closeMobileDetail
    );
    if (!inner) {
      var errDetail = document.createElement("div");
      errDetail.className = "detail-empty";
      errDetail.textContent = "이 리포트를 표시할 수 없습니다.";
      detail.appendChild(errDetail);
      return;
    }
    detail.appendChild(inner);
  }

  function render() {
    var query = qInput.value;
    var valid = reports.map(sanitizeEntry).filter(Boolean);
    var scoped = valid.filter(function (entry) {
      return matchesCategory(entry, state.repo) && (state.author === "all" || entry.author === state.author);
    });
    var matched = scoped.filter(function (entry) {
      return matchesArchiveEntry(entry, query);
    });
    var sorted = sortEntries(matched, state.sort);
    var groups = groupEntries(sorted, state.sort);

    state.selectedId = pickSelectedId(sorted, state.selectedId);

    countNum.textContent = String(sorted.length);
    totalNum.textContent = String(valid.length);

    renderRepoStats(valid);
    renderAuthorOptions(valid);
    renderRailStatus();
    renderRailList(groups, query);
    renderDetail(sorted, query);
  }

  function loadExternalCategory(source) {
    pendingCategories[source.key] = true;
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
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
        var entries = Array.isArray(externalPayload.reports) ? externalPayload.reports : [];
        entries = tagCategory(entries, source.key);
        entries = absolutizeReportUrl(entries, source.base);
        reports = mergeAndSortReports([reports, entries]);
        render();
      })
      .catch(function (error) {
        if (timeoutId) clearTimeout(timeoutId);
        delete pendingCategories[source.key];
        failedCategories[source.key] = true;
        console.warn("[archive] failed to load category '" + source.key + "':", error);
        render();
      });
  }

  applyTheme();
  themeToggle.addEventListener("click", toggleTheme);
  qInput.addEventListener("input", function () {
    render();
  });
  authorSelect.addEventListener("change", function () {
    state.author = authorSelect.value;
    render();
  });
  sortSelect.addEventListener("change", function () {
    state.sort = sortSelect.value;
    render();
  });

  var externalSources = CATEGORY_SOURCES.filter(function (source) {
    return !!source.url;
  });
  externalSources.forEach(function (source) {
    pendingCategories[source.key] = true;
  });

  render();

  externalSources.forEach(loadExternalCategory);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    matchesArchiveEntry: matchesArchiveEntry,
    matchesCategory: matchesCategory,
    tagCategory: tagCategory,
    absolutizeReportUrl: absolutizeReportUrl,
    mergeAndSortReports: mergeAndSortReports,
    fmtDate: fmtDate,
    highlightParts: highlightParts,
    sortEntries: sortEntries,
    groupEntries: groupEntries,
    pickSelectedId: pickSelectedId,
    sanitizeEntry: sanitizeEntry,
    buildRailItem: buildRailItem,
    buildRailItemSafely: buildRailItemSafely,
    buildDetailContent: buildDetailContent,
    buildDetailContentSafely: buildDetailContentSafely,
  };
}
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeArchive);
  } else {
    initializeArchive();
  }
}
