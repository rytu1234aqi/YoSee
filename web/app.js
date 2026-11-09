// The three bare module paths below are rewritten by server/web.py at serve
// time with the same content fingerprint as app.js itself. Keeping the source
// plain lets asset_version() stay stable, while the browser always requests
// URLs a stale WebView cache has never seen.
import { FeedStore, sortForStream, hideDismissed, filterByTag } from "/static/feed-store.mjs";
import { articleFragment, safeUrl, plainText } from "/static/article.mjs";
import { initResizableLayout } from "/static/layout.mjs";

const $ = (id) => document.getElementById(id);
const storedView = localStorage.getItem("yosee:view");
const state = {
  source: "", query: "", selected: null, status: "all", article: null,
  view: ["stream", "saved", "library"].includes(storedView) ? storedView : "stream",
  tag: "",
};
const dismissed = new Set();
const translations = {
  zh: { brand: "有得看", search: "搜索", searchPlaceholder: "搜索文章", library: "资料库", all: "全部文章", unread: "未读", saved: "已收藏", archived: "已归档", sources: "来源", allSources: "全部来源", addSource: "+ 添加来源", latest: "最新文章", searchTitle: "搜索", loaded: "已加载", loadMore: "加载更多", loading: "正在加载…", retry: "重试加载", end: "已到末尾", empty: "没有找到文章。", read: "标记为已读", unreadAction: "标记为未读", save: "收藏", unsave: "取消收藏", original: "打开原文 ↗", resizeSidebar: "拖动调整资料库宽度，双击恢复默认", resizeFeed: "拖动调整文章列表宽度，双击恢复默认", backupOk: "每日备份：", backupNone: "每日备份：尚未生成", backupError: "每日备份失败", viewStream: "刷流", viewSaved: "收藏夹", viewLibrary: "资料库", seen: "已刷过", dismiss: "划走", tagLike: "喜欢", tagLater: "回头再看", allTags: "全部", exportLinks: "导出链接清单", exportEmpty: "还没有收藏的链接", readerTitle: "读文章", keywordsLabel: "只要包含这些词", keywordsHint: "用逗号分开。留空表示都要。设了关键词后，只有标题或正文里出现这些词的文章才会收进来；只有标题、没有正文的会跳过。", cleanupOk: "旧链接清理：保留最近 ", cleanupNone: "旧链接清理：已关闭", sourceStale: "久未更新", noBody: "这篇只存了链接，没有存下正文。", openOriginal: "打开原文 ↗" },
  en: { brand: "Youdekan", search: "Search", searchPlaceholder: "Search articles", library: "Library", all: "All articles", unread: "Unread", saved: "Saved", archived: "Archived", sources: "Sources", allSources: "All sources", addSource: "+ Add source", latest: "Latest", searchTitle: "Search", loaded: "loaded", loadMore: "Load more", loading: "Loading…", retry: "Retry loading", end: "You’ve reached the end.", empty: "No articles found.", read: "Mark read", unreadAction: "Mark unread", save: "Save", unsave: "Remove saved", original: "Open original ↗", resizeSidebar: "Drag to resize the library, double-click to reset", resizeFeed: "Drag to resize the article list, double-click to reset", backupOk: "Daily backup: ", backupNone: "Daily backup: none yet", backupError: "Daily backup failed", viewStream: "Stream", viewSaved: "Collection", viewLibrary: "Library", seen: "Seen", dismiss: "Skip", tagLike: "Liked", tagLater: "Read later", allTags: "All", exportLinks: "Export links", exportEmpty: "No saved links yet", readerTitle: "Reading", keywordsLabel: "Only show me articles containing", keywordsHint: "Separate with commas. Leave empty to allow everything. When set, only articles whose title or body contains a keyword are kept, and title-only items are skipped.", cleanupOk: "Old-link cleanup: keeping the newest ", cleanupNone: "Old-link cleanup: disabled", sourceStale: "Quiet lately", noBody: "Only the link was saved — this site did not include the article text.", openOriginal: "Open original ↗" },
};
let language = localStorage.getItem("language") || "zh";
const t = (key) => translations[language][key] || key;
function applyLanguage() {
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((node) => { node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel)); });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => { node.title = t(node.dataset.i18nTitle); });
  $("language").textContent = language === "zh" ? "EN" : "中";
  $("sync-status").textContent = language === "zh" ? "正在检查更新…" : "Checking updates…";
  loadBackupStatus();
  loadCleanupStatus();
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

const localToken = document.querySelector('meta[name="local-token"]')?.content || "";
function withToken(headers = {}) {
  return localToken ? { ...headers, "X-Local-Token": localToken } : headers;
}

async function api(path, options = {}) {
  const headers = withToken(options.headers || {});
  const response = await fetch(path, { cache: "no-store", ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status}). Please retry.`);
  }
  return response.json();
}

function parseKeywords(value) {
  const words = String(value || "").split(/[,，\s]+/).map((word) => word.trim().toLowerCase()).filter(Boolean);
  return [...new Set(words)].slice(0, 20);
}

async function loadBackupStatus() {
  const node = $("backup-status");
  if (!node) return;
  try {
    const info = await api("/backup/status");
    if (!info.enabled) { node.textContent = ""; return; }
    if (info.last_error) { node.textContent = t("backupError"); node.dataset.state = "error"; return; }
    const day = info.latest_file ? info.latest_file.replace(/^daily-|\.json$/g, "") : "";
    node.textContent = day ? `${t("backupOk")}${day}` : t("backupNone");
    node.dataset.state = "ok";
  } catch (_) {
    node.textContent = t("backupError");
    node.dataset.state = "error";
  }
}

async function loadCleanupStatus() {
  const node = $("cleanup-status");
  if (!node) return;
  try {
    const info = await api("/cleanup/status");
    node.textContent = info.enabled
      ? `${t("cleanupOk")}${info.keep_recent}${language === "zh" ? " 条" : " links"}`
      : t("cleanupNone");
  } catch (_) { node.textContent = ""; }
}

async function loadSources() {
  try {
    const sources = await api("/sources");
    window.__sources = sources;
    $("sources").innerHTML = sources.map((source) =>
      `<div class="source-row"><button class="source" data-source="${escapeHtml(source.id)}"><span>${escapeHtml(source.name)}</span><small class="${source.stale ? "source-stale" : ""}">${source.stale ? `${escapeHtml(t("sourceStale"))}${source.days_since_update !== null ? ` · ${source.days_since_update}d` : ""}` : source.enabled ? (source.last_error ? "Error" : "Active") : "Paused"}</small></button><button class="source-refresh" data-refresh-source="${escapeHtml(source.id)}" title="Fetch now" aria-label="Fetch ${escapeHtml(source.name)}">↻</button><button class="source-edit" data-edit-source="${escapeHtml(source.id)}" title="Source settings" aria-label="Settings for ${escapeHtml(source.name)}">⚙</button><button class="source-history" data-history-source="${escapeHtml(source.id)}" title="Fetch history" aria-label="History for ${escapeHtml(source.name)}">≡</button>${source.id.startsWith("custom-") ? `<button class="source-remove" data-remove-source="${escapeHtml(source.id)}" title="Remove source" aria-label="Remove ${escapeHtml(source.name)}">×</button>` : ""}</div>`
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
  form.elements.keywords.value = (source.keywords || []).join(", ");
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
      keywords: parseKeywords(data.get("keywords")),
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
    const keywords = parseKeywords(data.get("keywords"));
    try {
      let feedUrl = String(data.get("feed_url"));
      const customHtml = !$("html-config").hidden;
      let result;
      if (customHtml) {
        result = await api("/sources/html", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          name: data.get("name"), url: feedUrl, item_selector: data.get("item_selector"),
          title_selector: data.get("title_selector"), link_selector: data.get("link_selector"),
          summary_selector: data.get("summary_selector") || null, fetch_interval_seconds: Number(data.get("interval")) * 60,
          keywords,
        }) });
      } else {
        const discovered = await api("/sources/discover", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: data.get("name"), url: feedUrl }) });
        result = await api("/sources", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          name: data.get("name"), feed_url: discovered.feed_url, fetch_interval_seconds: Number(data.get("interval")) * 60,
          keywords,
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

function setView(view, reload = true) {
  state.view = view;
  localStorage.setItem("yosee:view", view);
  document.body.dataset.view = view;
  document.querySelectorAll(".view-button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $("saved-filters").hidden = view !== "saved";
  // Only one reader may exist at a time: the classic pane and the full-screen
  // dialog both use #read / #save / #article-body ids.
  $("detail").innerHTML = "";
  closeReader();
  if (view === "saved") state.status = "saved";
  else if (view === "stream") state.status = "all";
  document.querySelectorAll(".filter").forEach((button) => button.classList.toggle("active", button.dataset.status === state.status));
  dismissed.clear();
  renderFeed();
  if (reload) return resetFeed();
  return Promise.resolve();
}

function resetFeed() {
  clearTimeout(searchTimer);
  $("updates").hidden = true;
  dismissed.clear();
  const filters = { status: state.status };
  if (state.source) filters.source_id = state.source;
  if (state.query) filters.q = state.query;
  return feed.reset(filters);
}

function coverMarkup(item) {
  const url = safeUrl(item.cover_url);
  const initial = (item.source_name || item.title || "•").trim().charAt(0).toUpperCase() || "•";
  const fallback = `<span class="card-cover-fallback" aria-hidden="true">${escapeHtml(initial)}</span>`;
  if (!url) return `<span class="card-cover" data-cover="placeholder">${fallback}</span>`;
  return `<span class="card-cover" data-cover="image"><img src="${escapeHtml(url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" />${fallback}</span>`;
}

function cardSummary(item) {
  const plain = plainText(item.summary);
  if (!plain || plain.length < 12) return "";
  return `<span class="card-summary">${escapeHtml(plain)}</span>`;
}

function bindCoverFallbacks() {
  $("feed").querySelectorAll(".card-cover img").forEach((img) => {
    img.addEventListener("error", () => {
      const cover = img.closest(".card-cover");
      if (cover) cover.dataset.cover = "placeholder";
    }, { once: true });
  });
}

function renderCard(item) {
  const stream = state.view !== "library";
  const saved = !!item.saved;
  return `<article class="feed-card ${item.is_read ? "read" : ""} ${item.id === state.selected ? "selected" : ""}" data-id="${escapeHtml(item.id)}">
      <button class="card-open" data-open="${escapeHtml(item.id)}" aria-label="${escapeHtml(item.title)}">
        ${stream ? coverMarkup(item) : ""}
        <span class="card-source">${escapeHtml(item.source_name)}${item.is_read ? ` · ${t("seen")}` : ""}</span>
        <strong>${escapeHtml(item.title)}</strong>
        ${cardSummary(item)}
        <time>${formatDate(item.published_at)}${saved ? ` · ${t("saved")}` : ""}</time>
      </button>
      <div class="card-actions">
        <button data-save="${escapeHtml(item.id)}" aria-pressed="${saved}">${saved ? t("unsave") : t("save")}</button>
        ${saved ? `<button data-tag="${escapeHtml(item.id)}" data-value="like" aria-pressed="${item.tag === "like"}">${t("tagLike")}</button><button data-tag="${escapeHtml(item.id)}" data-value="later" aria-pressed="${item.tag === "later"}">${t("tagLater")}</button>` : ""}
        ${stream ? `<button class="card-dismiss" data-dismiss="${escapeHtml(item.id)}">${t("dismiss")} ✕</button>` : ""}
      </div>
    </article>`;
}

function visibleItems() {
  const ordered = state.view === "library"
    ? feed.items
    : sortForStream(hideDismissed(feed.items, dismissed));
  return state.view === "saved" ? filterByTag(ordered, state.tag) : ordered;
}

function renderFeed() {
  const titles = { all: t("latest"), unread: t("unread"), saved: t("saved"), archived: t("archived") };
  $("feed-title").textContent = state.view === "saved" ? t("viewSaved")
    : state.view === "stream" ? t("viewStream")
      : state.query ? `${t("searchTitle")} · ${titles[state.status]}` : titles[state.status];
  const items = visibleItems();
  $("count").textContent = `${items.length} ${t("loaded")}`;
  $("feed").setAttribute("aria-busy", String(feed.loading));
  $("feed").innerHTML = items.map(renderCard).join("");
  bindCoverFallbacks();
  $("feed-error").textContent = feed.error;
  $("feed-error").hidden = !feed.error;
  $("feed-status").textContent = feed.loading ? t("loading")
    : feed.error ? "" : !items.length ? t("empty") : !feed.cursor ? t("end") : "";
  $("load-more").hidden = !feed.loading && !feed.cursor && !feed.error;
  $("load-more").disabled = feed.loading;
  $("load-more").textContent = feed.loading ? t("loading") : feed.error ? t("retry") : t("loadMore");
}

function articleContainer() {
  return state.view === "library" ? $("detail") : $("reader-body");
}

function openReader() {
  const dialog = $("reader-dialog");
  if (!dialog.open) dialog.showModal();
}

function closeReader() {
  const dialog = $("reader-dialog");
  if (dialog.open) dialog.close();
  $("reader-body").innerHTML = "";
}

async function showArticle(id) {
  articleRequest?.abort();
  const controller = new AbortController();
  articleRequest = controller;
  state.selected = id;
  state.article = null;
  renderFeed();
  articleContainer().innerHTML = '<p class="empty" role="status">Loading article…</p>';
  if (state.view !== "library") openReader();
  else if (window.matchMedia("(max-width: 900px)").matches) $("detail").scrollIntoView({ behavior: "smooth" });
  try {
    const item = await api(`/content/${encodeURIComponent(id)}`, { signal: controller.signal });
    if (controller !== articleRequest) return;
    state.article = item;
    renderArticle();
  } catch (error) {
    if (controller !== articleRequest || error.name === "AbortError") return;
    articleContainer().innerHTML = `<p class="error" role="alert">${escapeHtml(error.message)}</p><button id="retry-article">Retry article</button>`;
    $("retry-article").addEventListener("click", () => showArticle(id));
  }
}

function renderArticle() {
  const item = state.article;
  if (!item) return;
  articleContainer().innerHTML = `<div class="article-meta">${escapeHtml(item.source_name)} · ${formatDate(item.published_at)} · ${item.is_read ? t("seen") : t("unread")}</div>
    <h1>${escapeHtml(item.title)}</h1><p class="byline">${escapeHtml(item.author || "")}</p>
    <div class="article-actions">
      <button id="read" aria-pressed="${item.is_read}">${item.is_read ? t("unreadAction") : t("read")}</button>
      <button id="save" aria-pressed="${item.saved}">${item.saved ? t("unsave") : t("save")}</button>
      <a id="original" target="_blank" rel="noopener noreferrer">${t("original")}</a>
    </div><p id="action-error" class="error" role="alert" hidden></p><div id="article-body" class="article-body"></div>`;
  const original = safeUrl(item.canonical_url);
  if (original) $("original").href = original;
  else $("original").hidden = true;
  $("article-body").append(articleFragment(item, { noBody: t("noBody"), openOriginal: t("openOriginal") }));
  $("read").disabled = $("save").disabled = busy.has(item.id);
  $("read").addEventListener("click", () => updateState(item, { is_read: !item.is_read }));
  $("save").addEventListener("click", () => updateState(item, item.saved ? { saved: false } : { saved: true, tag: "later" }));
}

function findItem(id) {
  return feed.items.find((entry) => entry.id === id) || (state.selected === id ? state.article : null);
}

async function updateState(item, change) {
  const id = item.id;
  if (busy.has(id)) return;
  busy.add(id);
  const readButton = $("read");
  const saveButton = $("save");
  const errorNode = $("action-error");
  if (readButton) readButton.disabled = true;
  if (saveButton) saveButton.disabled = true;
  if (errorNode) errorNode.hidden = true;
  try {
    const values = await api(`/content/${encodeURIComponent(id)}/state`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(change),
    });
    const merged = { ...values };
    if (change.tag !== undefined) merged.tag = change.tag === "none" ? null : change.tag;
    if (change.saved === false) merged.tag = null;
    Object.assign(item, merged);
    feed.patch(id, merged);
    if (state.selected === id && state.article) {
      Object.assign(state.article, merged);
      renderArticle();
    }
  } catch (error) {
    if (errorNode) {
      errorNode.textContent = error.message;
      errorNode.hidden = false;
    }
  } finally {
    busy.delete(id);
    const read = $("read");
    const save = $("save");
    if (read) read.disabled = false;
    if (save) save.disabled = false;
  }
  renderFeed();
}

function toggleSaved(id) {
  const item = findItem(id);
  if (!item) return;
  updateState(item, item.saved ? { saved: false } : { saved: true, tag: "later" });
}

function toggleTag(id, value) {
  const item = findItem(id);
  if (!item) return;
  updateState(item, { tag: item.tag === value ? "none" : value });
}

function dismissCard(id) {
  const item = findItem(id);
  if (!item) return;
  dismissed.add(id);
  renderFeed();
  updateState(item, { is_read: true });
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
  const save = event.target.closest("button[data-save]");
  if (save) { toggleSaved(save.dataset.save); return; }
  const tag = event.target.closest("button[data-tag]");
  if (tag) { toggleTag(tag.dataset.tag, tag.dataset.value); return; }
  const skip = event.target.closest("button[data-dismiss]");
  if (skip) { dismissCard(skip.dataset.dismiss); return; }
  const open = event.target.closest("button[data-open]");
  if (open) showArticle(open.dataset.open);
});
$("view-switch").addEventListener("click", (event) => {
  const button = event.target.closest("button.view-button");
  if (button && button.dataset.view !== state.view) setView(button.dataset.view);
});
$("saved-filters").addEventListener("click", (event) => {
  const button = event.target.closest("button.tag-filter");
  if (!button) return;
  state.tag = button.dataset.tag;
  document.querySelectorAll(".tag-filter").forEach((node) => node.classList.toggle("active", node === button));
  renderFeed();
});
$("reader-close").addEventListener("click", closeReader);
$("reader-dialog").addEventListener("close", () => { $("reader-body").innerHTML = ""; });
$("export-saved").addEventListener("click", async () => {
  try {
    const links = await api("/export/saved-links");
    if (!links.length) { window.alert(t("exportEmpty")); return; }
    const text = links.map((link) => `${link.title}\n${link.url}`).join("\n\n");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(blob);
    anchor.download = `yosee-saved-${new Date().toISOString().slice(0, 10)}.txt`;
    anchor.click();
    URL.revokeObjectURL(anchor.href);
  } catch (error) { window.alert(error.message); }
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
    const items = visibleItems();
    const index = Math.max(0, items.findIndex((item) => item.id === state.selected) + 1);
    if (items[index]) showArticle(items[index].id);
    event.preventDefault();
  } else if (event.key === "k" || event.key === "ArrowUp") {
    const items = visibleItems();
    const index = Math.max(0, items.findIndex((item) => item.id === state.selected) - 1);
    if (items[index]) showArticle(items[index].id);
    event.preventDefault();
  } else if (event.key === "r" && state.article) updateState(state.article, { is_read: !state.article.is_read });
  else if (event.key === "s" && state.article) updateState(state.article, state.article.saved ? { saved: false } : { saved: true, tag: "later" });
});
$("import").addEventListener("click", () => $("import-file").click());
$("backup").addEventListener("click", async () => {
  try {
    const response = await fetch("/backup", { cache: "no-store", headers: withToken() });
    if (!response.ok) throw new Error("Backup failed");
    const blob = new Blob([JSON.stringify(await response.json(), null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `youdekan-backup-${new Date().toISOString().slice(0, 10)}.json`;
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
initResizableLayout();
loadBackupStatus();
loadCleanupStatus();
setView(state.view, false);
// Record the update revision before loading the first page.
pollSync().then(resetFeed);

// ?debug=1 shows the live viewport/grid geometry and records it in the server
// access log. Embedded preview panels can be much narrower than the desktop
// window, so this makes layout diagnosis possible without copying numbers out
// of the UI by hand.
if (new URLSearchParams(window.location.search).has("debug")) {
  const node = $("debug-info");
  const grid = document.querySelector(".layout");
  const detail = document.querySelector(".detail-pane");
  const feed = document.querySelector(".feed-pane");
  node.hidden = false;
  const report = () => {
    const columns = getComputedStyle(grid).gridTemplateColumns;
    const detailY = Math.round(detail.getBoundingClientRect().top);
    const feedY = Math.round(feed.getBoundingClientRect().top);
    const info = {
      w: window.innerWidth,
      h: window.innerHeight,
      dpr: window.devicePixelRatio || 1,
      cols: columns.replaceAll(" ", "_"),
      detailY,
      feedY,
      scrollW: document.body.scrollWidth,
      inline: (grid.getAttribute("style") || "-").replaceAll(" ", "_"),
    };
    node.textContent = `${info.w}px · ${columns} · ${detailY === feedY ? "三栏同排" : "详情换行"}`;
    // /health is public, so the query string lands in the uvicorn access log.
    fetch(`/health?${new URLSearchParams(info)}`, { cache: "no-store" }).catch(() => {});
  };
  window.addEventListener("resize", report);
  report();
}
