import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const webRoot = new URL("../../web/", import.meta.url);
const read = (name) => readFileSync(new URL(name, webRoot), "utf8");

test("the five maintenance tools live behind one drawer in the topbar", () => {
  const html = read("index.html");
  assert.ok(html.includes('id="tools-menu"'));
  assert.ok(html.includes('id="tools-panel"'));
  assert.match(html, /<summary class="tools-trigger"/);

  const header = html.slice(html.indexOf("<header"), html.indexOf("</header>"));
  const panel = html.slice(html.indexOf('id="tools-panel"'), html.indexOf("</details>"));
  for (const id of ["refresh", "import", "backup", "theme", "language"]) {
    assert.ok(panel.includes(`id="${id}"`), `${id} should be inside the drawer`);
    assert.ok(!header.includes(`class="icon-button" title`) || !header.includes(`id="${id}" class="icon-button"`), `${id} should not be a loose topbar icon`);
  }
  // The two draggable separators must remain the only tabbable non-controls.
  assert.equal((html.match(/tabindex="0"/g) || []).length, 2);
  assert.equal((html.match(/role="separator"/g) || []).length, 2);

  const js = read("app.js");
  assert.ok(js.includes("toolsMenu.open = false"));
  assert.ok(js.includes('event.key === "Escape"'));
  assert.ok(js.includes("toolsMenu.contains(event.target)"));
  assert.ok(js.includes('$("language-code").textContent'));
  assert.ok(!js.includes('$("language").textContent'));

  const css = read("app.css");
  assert.ok(css.includes(".tools-panel"));
  assert.ok(css.includes(".tool-button"));
  assert.match(css, /@media \(max-width: 560px\) \{ \.tools-trigger/);
});

test("every data-i18n key has both a Chinese and an English label", () => {
  const html = read("index.html");
  const js = read("app.js");
  const keys = new Set();
  for (const match of html.matchAll(/data-i18n(?:-placeholder|-aria-label|-title)?="([A-Za-z][A-Za-z0-9]*)"/g)) {
    keys.add(match[1]);
  }
  assert.ok(keys.size >= 30, `expected many i18n keys, got ${keys.size}`);
  for (const key of keys) {
    const occurrences = js.split(new RegExp(`\\b${key}\\s*:`)).length - 1;
    assert.ok(occurrences >= 2, `i18n key "${key}" is missing a zh or en label (${occurrences})`);
  }
  assert.ok(js.includes("Object.assign(translations.zh, uiLabels.zh)"));
  assert.ok(js.includes("Object.assign(translations.en, uiLabels.en)"));
});

test("no hard-coded English left on Chinese screens", () => {
  const js = read("app.js");
  // The zh/en tables legitimately contain English. Only runtime code counts.
  const code = js.slice(js.indexOf("let language"));
  for (const stale of [
    'role="status">Loading article…',
    'id="retry-article">Retry article',
    '"Automatic updates paused"',
    '"Automatic updates starting…"',
    '"Update status unavailable · retrying"',
    '"No fetch history yet."',
    '"Remove this source and its downloaded articles?"',
    'new Error("Backup failed")',
    'new Error("Invalid backup file.")',
    'return "undated"',
    'source(s) failed; retry scheduled`',
  ]) {
    assert.ok(!code.includes(stale), `stale English UI text remains: ${stale}`);
  }
  for (const key of ["undated", "loadingArticle", "syncPaused", "removeConfirm", "invalidBackup", "historyEmpty"]) {
    assert.ok(code.includes(`t("${key}")`), `missing translated call for ${key}`);
  }
  const html = read("index.html");
  for (const stale of [">Add source</h2>", ">Source settings</h2>", ">Cancel</button>", ">Save</button>", ">Select an article to read<", ">Loading history…<"]) {
    assert.ok(!html.includes(stale), `stale English markup remains: ${stale}`);
  }
});

test("stream cards keep only one primary action and two summary lines", () => {
  const js = read("app.js");
  assert.ok(js.includes('const canTag = stream && state.view === "saved";'));
  assert.ok(js.includes("card-primary"));
  assert.ok(js.includes('time class="card-meta"'));
  // Tag buttons still exist for the collection, just not in the stream.
  assert.ok(js.includes('data-value="like"'));
  assert.ok(js.includes('data-value="later"'));
  assert.ok(js.includes("${canTag && saved ?"));

  const css = read("app.css");
  assert.ok(css.includes("-webkit-line-clamp: 2"));
  assert.ok(!css.includes("max-height: 3em"));
  assert.ok(css.includes("aspect-ratio: 3.2 / 1"));
  assert.ok(css.includes("max-height: 150px"));
  assert.ok(css.includes(".card-actions .card-primary"));
  assert.match(css, /body\[data-view="stream"\] \.card-dismiss,[\s\S]*?position: absolute/);
});

test("cover artwork uses a small fixed palette instead of 360 hues", () => {
  const js = read("app.js");
  assert.match(js, /const COVER_HUES = \[205, 168, 262, 28\]/);
  assert.ok(js.includes("COVER_HUES[hash % COVER_HUES.length]"));
  assert.ok(!js.includes("% 360"), "per-degree hashing should be gone");
  assert.ok(js.includes("Math.imul(hash, 31)"), "hash should stay stable across sources");

  const css = read("app.css");
  assert.ok(css.includes("hsl(var(--cover-hue, 200) 30% 94%)"));
  assert.ok(css.includes("hsl(var(--cover-hue, 200) 38% 88%)"));
});
