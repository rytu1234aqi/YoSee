import { FeedStore } from "/static/feed-store.mjs";
import { articleFragment, safeUrl, summaryText } from "/static/article.mjs";

const $ = (id) => document.getElementById(id);
const state = { source: "", query: "", selected: null, status: "all", article: null };
const translations = {
  zh: { brand: "有得看", search: "搜索", searchPlaceholder: "搜索文章", library: "资料库", all: "全部文章", unread: "未读", saved: "已收藏", archived: "已归档", sources: "来源", allSources: "全部来源", addSource: "+ 添加来源", latest: "最新文章", searchTitle: "搜索", loaded: "已加载", loadMore: "加载更多", loading: "正在加载…", retry: "重试加载", end: "已到末尾", empty: "没有找到文章。", read: "标记为已读", unreadAction: "标记为未读", save: "稍后阅读", unsave: "取消收藏", original: "打开原文 ↗" },
  en: { brand: "Youdekan", search: "Search", searchPlaceholder: "Search articles", library: "Library", all: "All articles", unread: "Unread", saved: "Saved", archived: "Archived", sources: "Sources", allSources: "All sources", addSource: "+ Add source", latest: "Latest", searchTitle: "Search", loaded: "loaded", loadMore: "Load more", loading: "Loading…", retry: "Retry loading", end: "You’ve reached the end.", empty: "No articles found.", read: "Mark read", unreadAction: "Mark unread", save: "Save for later", unsave: "Remove saved", original: "Open original ↗" },
};
let language = localStorage.getItem("language") || "zh";
const t = (key) => translations[language][key] || key;
function applyLanguage() {
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
  $("language").textContent = language === "zh" ? "EN" : "中";
  $("sync-status").textContent = language === "zh" ? "正在检查更新…" : "Checking updates…";
  renderFeed();
}
if (localStorage.getItem("theme") === "dark") document.documentElement.dataset.theme = "dark";
const busy = new Set();
let articleRequest;
let searchTimer;
let syncTimer;
let revision = null;
let syncRequest;
let syncGeneration = 0;
const feed = new FeedStore(api, renderFeed);

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status}). Please retry.`);
  }
  return response.json();
}

async function loadSources() {
  try {
    const sources = await api("/sources");
    window.__sources = sources;
    $("sources").innerHTML = sources.map((source) =>
      `<div class="source-row"><button class="source" data-source="${escapeHtml(source.id)}"><span>${escapeHtml(source.name)}</span><small>${source.enabled ? (source.last_error ? "Error" : "Active") : "Paused"}</small></button><button class="source-refresh" data-refresh-source="${escapeHtml(source.id)}" title="Fetch now" aria-label="Fetch ${escapeHtml(source.name)}">↻</button><button class="source-edit" data-edit-source="${escapeHtml(source.id)}" title="Source settings" aria-label="Settings for ${escapeHtml(source.name)}">⚙</button><button class="source-history" data-history-source="${escapeHtml(source.id)}" title="Fetch history" aria-label="History for ${escapeHtml(source.name)}">≡</button>${source.id.startsWith("custom-") ? `<button class="source-remove" data-remove-source="${escapeHtml(source.id)}" title="Remove source" aria-label="Remove ${escapeHtml(source.name)}">×</button>` : ""}</div>`
    ).join("");
  } catch (error) {
    $("sources").innerHTML = `<p class="error">${escapeHtml(error.message)}</p><button id="retry-sources">Retry sources</button>`;
    $("retry-sources").addEventListener("click", loadSources);
  }
}

function openSourceSettings(source) {
  const dialog = $("settings-dialog");
  const form = $("settings-form");
  form.elements.id.value = source.id;
  form.elements.name.value = source.name;
  form.elements.interval.value = Math.round(source.fetch_interval_seconds / 60);
  form.elements.enabled.checked = source.enabled;
  $("settings-form-error").hidden = true;
  dialog.showModal();
}

async function openSourceHistory(source) {
  $("runs-title").textContent = `Fetch history · ${source.name}`;
  $("runs-list").innerHTML = '<p class="empty">Loading history…</p>';
  $("runs-dialog").showModal();
  try {
    const runs = await api(`/sources/${encodeURIComponent(source.id)}/runs`);
    $("runs-list").innerHTML = runs.length ? runs.map((run) => `<div class="run-row"><div><strong>${escapeHtml(run.status)}</strong><span>${new Date(run.started_at).toLocaleString()}</span></div><div>${run.items_created} new · ${run.items_updated} updated${run.error_message ? `<p class="error">${escapeHtml(run.error_message)}</p>` : ""}</div></div>`).join("") : '<p class="empty">No fetch history yet.</p>';
  } catch (error) { $("runs-list").innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`; }
}

$("runs-close").addEventListener("click", () => $("runs-dialog").close());

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const button = $("settings-submit");
  button.disabled = true;
  try {
    await api(`/sources/${encodeURIComponent(data.get("id"))}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      name: data.get("name"), fetch_interval_seconds: Number(data.get("interval")) * 60, enabled: data.has("enabled"),
    }) });
    $("settings-dialog").close();
    await loadSources();
  } catch (error) {
    $("settings-form-error").textContent = error.message;
    $("settings-form-error").hidden = false;
  } finally { button.disabled = false; }
});

function setupSourceDialog() {
  const dialog = $("source-dialog");
  const form = $("source-form");
  $("add-source").addEventListener("click", () => { $("source-form-error").hidden = true; $("html-config").hidden = true; form.reset(); dialog.showModal(); });
  $("advanced-html-toggle").addEventListener("click", () => { $("html-config").hidden = !$("html-config").hidden; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("source-submit");
    button.disabled = true;
    $("source-form-error").hidden = true;
    const data = new FormData(form);
    try {
      let feedUrl = String(data.get("feed_url"));
      const customHtml = !$("html-config").hidden;
      let result;
      if (customHtml) {
        result = await api("/sources/html", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          name: data.get("name"), url: feedUrl, item_selector: data.get("item_selector"),
          title_selector: data.get("title_selector"), link_selector: data.get("link_selector"),
          summary_selector: data.get("summary_selector") || null, fetch_interval_seconds: Number(data.get("interval")) * 60,
        }) });
      } else {
        const discovered = await api("/sources/discover", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: data.get("name"), url: feedUrl }) });
        result = await api("/sources", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          name: data.get("name"), feed_url: discovered.feed_url, fetch_interval_seconds: Number(data.get("interval")) * 60,
        }) });
      }
      dialog.close();
      await loadSources();
      resetFeed();
      window.alert(result.ok ? `Source added: ${result.inserted} new article(s) fetched.` : `Source added, but first fetch failed: ${result.error || "unknown error"}`);
    } catch (error) {
      $("source-form-error").textContent = error.message;
      $("source-form-error").hidden = false;
    } finally { button.disabled = false; }
  });
}

function resetFeed() {
  clearTimeout(searchTimer);
  $("updates").hidden = true;
  const filters = { status: state.status };
  if (state.source) filters.source_id = state.source;
  if (state.query) filters.q = state.query;
  return feed.reset(filters);
}

function renderFeed() {
  const titles = { all: t("latest"), unread: t("unread"), saved: t("saved"), archived: t("archived") };
  $("feed-title").textContent = state.query ? `${t("searchTitle")} · ${titles[state.status]}` : titles[state.status];
  $("count").textContent = `${feed.items.length} ${t("loaded")}`;
  $("feed").setAttribute("aria-busy", String(feed.loading));
  $("feed").innerHTML = feed.items.map((item) =>
    `<button class="feed-card ${item.is_read ? "read" : ""} ${item.id === state.selected ? "selected" : ""}" data-id="${escapeHtml(item.id)}" aria-pressed="${item.id === state.selected}">
      <span class="card-source">${escapeHtml(item.source_name)}</span>
      <strong>${escapeHtml(item.title)}</strong>
      <span class="card-summary">${escapeHtml(summaryText(item.summary))}</span>
      <time>${formatDate(item.published_at)}${item.is_read ? " · Read" : ""}${item.saved ? " · Saved" : ""}</time>
    </button>`
  ).join("");
  $("feed-error").textContent = feed.error;
  $("feed-error").hidden = !feed.error;
  $("feed-status").textContent = feed.loading ? t("loading")
    : feed.error ? "" : !feed.items.length ? t("empty") : !feed.cursor ? t("end") : "";
  $("load-more").hidden = !feed.loading && !feed.cursor && !feed.error;
  $("load-more").disabled = feed.loading;
  $("load-more").textContent = feed.loading ? t("loading") : feed.error ? t("retry") : t("loadMore");
}

async function showArticle(id) {
  articleRequest?.abort();
  const controller = new AbortController();
  articleRequest = controller;
  state.selected = id;
  state.article = null;
  renderFeed();
  $("detail").innerHTML = '<p class="empty" role="status">Loading article…</p>';
  if (window.matchMedia("(max-width: 900px)").matches) $("detail").scrollIntoView({ behavior: "smooth" });
  try {
    const item = await api(`/content/${encodeURIComponent(id)}`, { signal: controller.signal });
    if (controller !== articleRequest) return;
    state.article = item;
    renderArticle();
  } catch (error) {
    if (controller !== articleRequest || error.name === "AbortError") return;
    $("detail").innerHTML = `<p class="error" role="alert">${escapeHtml(error.message)}</p><button id="retry-article">Retry article</button>`;
    $("retry-article").addEventListener("click", () => showArticle(id));
  }
}

function renderArticle() {
  const item = state.article;
  if (!item) return;
  $("detail").innerHTML = `<div class="article-meta">${escapeHtml(item.source_name)} · ${formatDate(item.published_at)} · ${item.is_read ? "Read" : "Unread"}</div>
    <h1>${escapeHtml(item.title)}</h1><p class="byline">${escapeHtml(item.author || "")}</p>
    <div class="article-actions">
      <button id="read" aria-pressed="${item.is_read}">${item.is_read ? "Mark unread" : "Mark read"}</button>
      <button id="save" aria-pressed="${item.saved}">${item.saved ? "Remove saved" : "Save for later"}</button>
      <a id="original" target="_blank" rel="noopener noreferrer">Open original ↗</a>
    </div><p id="action-error" class="error" role="alert" hidden></p><div id="article-body" class="article-body"></div>`;
  const original = safeUrl(item.canonical_url);
  if (original) $("original").href = original;
  else $("original").hidden = true;
  $("article-body").append(articleFragment(item));
  $("read").disabled = $("save").disabled = busy.has(item.id);
  $("read").addEventListener("click", () => updateState(item, { is_read: !item.is_read }));
  $("save").addEventListener("click", () => updateState(item, { saved: !item.saved }));
}

async function updateState(item, change) {
  const id = item.id;
  if (busy.has(id)) return;
  busy.add(id);
  $("read").disabled = $("save").disabled = true;
  $("action-error").hidden = true;
  try {
    const values = await api(`/content/${encodeURIComponent(id)}/state`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(change),
    });
    Object.assign(item, values);
    feed.patch(id, values);
    if (state.selected === id && state.article) {
      Object.assign(state.article, values);
      renderArticle();
    }
  } catch (error) {
    if (state.selected === id && $("action-error")) {
      $("action-error").textContent = error.message;
      $("action-error").hidden = false;
    }
  } finally {
    busy.delete(id);
    if (state.selected === id && $("read")) $("read").disabled = $("save").disabled = false;
  }
}

async function pollSync() {
  const generation = ++syncGeneration;
  clearTimeout(syncTimer);
  syncRequest?.abort();
  syncRequest = new AbortController();
  try {
    const sync = await api("/sync/status", { signal: syncRequest.signal });
    if (generation !== syncGeneration) return;
    if (revision !== null && revision !== sync.revision) $("updates").hidden = false;
    revision = sync.revision;
    const failure = sync.failed_sources ? ` · ${sync.failed_sources} source(s) failed; retry scheduled` : "";
    $("sync-status").textContent = !sync.enabled ? "Automatic updates paused"
      : sync.running ? `Updating ${sync.current_source}…${failure}`
        : sync.last_checked_at ? `Checked ${new Date(sync.last_checked_at).toLocaleTimeString()}${failure}`
          : sync.last_error ? "Update check failed · retry scheduled" : "Automatic updates starting…";
    $("sync-status").title = sync.last_error || "Sources update at their configured intervals while this server is running.";
  } catch (error) {
    if (generation === syncGeneration && error.name !== "AbortError") $("sync-status").textContent = "Update status unavailable · retrying";
  } finally {
    if (!document.hidden && generation === syncGeneration) syncTimer = setTimeout(pollSync, 5000);
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
}

function formatDate(value) {
  if (!value) return "undated";
  const utc = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`;
  return new Date(utc).toLocaleDateString();
}

$("sidebar").addEventListener("click", (event) => {
  const source = event.target.closest("button.source");
  const filter = event.target.closest("button.filter");
  const remove = event.target.closest("button.source-remove");
  const refresh = event.target.closest("button.source-refresh");
  const edit = event.target.closest("button.source-edit");
  const history = event.target.closest("button.source-history");
  if (history) {
    const source = (window.__sources || []).find((item) => item.id === history.dataset.historySource);
    if (source) openSourceHistory(source);
    return;
  }
  if (edit) {
    const source = (window.__sources || []).find((item) => item.id === edit.dataset.editSource);
    if (source) openSourceSettings(source);
    return;
  }
  if (refresh) {
    refresh.disabled = true;
    api(`/sources/${encodeURIComponent(refresh.dataset.refreshSource)}/fetch`, { method: "POST" })
      .then((result) => { loadSources(); resetFeed(); window.alert(result.ok ? `Fetched ${result.inserted} new article(s).` : `Fetch failed: ${result.error || "unknown error"}`); })
      .catch((error) => window.alert(error.message))
      .finally(() => { refresh.disabled = false; });
    return;
  }
  if (remove) {
    if (!window.confirm("Remove this source and its downloaded articles?")) return;
    api(`/sources/${encodeURIComponent(remove.dataset.removeSource)}`, { method: "DELETE" })
      .then(() => { state.source = ""; loadSources(); resetFeed(); })
      .catch((error) => window.alert(error.message));
    return;
  }
  if (source) {
    state.source = source.dataset.source;
    document.querySelectorAll(".source").forEach((button) => button.classList.toggle("active", button === source));
  } else if (filter) {
    state.status = filter.dataset.status;
    document.querySelectorAll(".filter").forEach((button) => button.classList.toggle("active", button === filter));
  } else return;
  resetFeed();
});
$("feed").addEventListener("click", (event) => {
  const card = event.target.closest(".feed-card");
  if (card) showArticle(card.dataset.id);
});
$("query").addEventListener("input", (event) => {
  state.query = event.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(resetFeed, 250);
});
$("query").addEventListener("keydown", (event) => { if (event.key === "Enter") resetFeed(); });
$("refresh").addEventListener("click", resetFeed);
$("theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme !== "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "";
  localStorage.setItem("theme", dark ? "dark" : "light");
});
$("language").addEventListener("click", () => { language = language === "zh" ? "en" : "zh"; localStorage.setItem("language", language); applyLanguage(); });
document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea")) return;
  if (event.key === "j" || event.key === "ArrowDown") {
    const index = Math.max(0, feed.items.findIndex((item) => item.id === state.selected) + 1);
    if (feed.items[index]) showArticle(feed.items[index].id);
    event.preventDefault();
  } else if (event.key === "k" || event.key === "ArrowUp") {
    const index = Math.max(0, feed.items.findIndex((item) => item.id === state.selected) - 1);
    if (feed.items[index]) showArticle(feed.items[index].id);
    event.preventDefault();
  } else if (event.key === "r" && state.article) updateState(state.article, { is_read: !state.article.is_read });
  else if (event.key === "s" && state.article) updateState(state.article, { saved: !state.article.saved });
});
$("import").addEventListener("click", () => $("import-file").click());
$("backup").addEventListener("click", async () => {
  try {
    const response = await fetch("/backup", { cache: "no-store" });
    if (!response.ok) throw new Error("Backup failed");
    const blob = new Blob([JSON.stringify(await response.json(), null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `gamedev-feed-backup-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (error) { window.alert(error.message); }
});
$("import-file").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const parsed = JSON.parse(await file.text());
    const body = Array.isArray(parsed) ? { items: parsed } : parsed;
    if (!body || typeof body !== "object") throw new Error("Invalid backup file.");
    const result = await api("/import/content", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    await loadSources();
    await resetFeed();
    window.alert(`Imported ${result.imported} article(s); skipped ${result.skipped}.`);
  } catch (error) { window.alert(error.message); }
  event.target.value = "";
});
$("updates").addEventListener("click", resetFeed);
$("load-more").addEventListener("click", () => feed.loadMore());
document.addEventListener("visibilitychange", () => {
  clearTimeout(syncTimer);
  syncRequest?.abort();
  if (!document.hidden) pollSync();
});
loadSources();
setupSourceDialog();
applyLanguage();
// Record the update revision before loading the first page.
pollSync().then(resetFeed);
