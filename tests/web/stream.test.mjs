import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  FeedStore,
  filterByTag,
  hideDismissed,
  sortForStream,
} from "../../web/feed-store.mjs";

const webRoot = new URL("../../web/", import.meta.url);
const read = (name) => readFileSync(new URL(name, webRoot), "utf8");

test("seen cards sink to the bottom without reshuffling either group", () => {
  const items = [
    { id: "a", is_read: true },
    { id: "b", is_read: false },
    { id: "c", is_read: true },
    { id: "d", is_read: false },
  ];
  assert.deepEqual(sortForStream(items).map((item) => item.id), ["b", "d", "a", "c"]);
  // The input array itself is not mutated: cursor paging still sees the
  // original server order.
  assert.deepEqual(items.map((item) => item.id), ["a", "b", "c", "d"]);
  assert.deepEqual(sortForStream([]), []);
});

test("dismissed cards leave the visible stream and tag filters are inclusive", () => {
  const items = [
    { id: "a", tag: "like" },
    { id: "b", tag: "later" },
    { id: "c", tag: null },
  ];
  assert.deepEqual(hideDismissed(items, new Set(["b"])).map((item) => item.id), ["a", "c"]);
  assert.deepEqual(filterByTag(items, "like").map((item) => item.id), ["a"]);
  assert.deepEqual(filterByTag(items, "").map((item) => item.id), ["a", "b", "c"]);
});

test("FeedStore.dismiss removes one card but keeps the paging cursor", async () => {
  const store = new FeedStore(async () => ({
    items: [{ id: "a" }, { id: "b" }],
    next_cursor: "next",
  }));
  await store.reset();
  store.dismiss("a");
  assert.deepEqual(store.items.map((item) => item.id), ["b"]);
  assert.equal(store.cursor, "next");
});

test("markup exposes three views, card actions and a full-screen reader", () => {
  const html = read("index.html");
  for (const view of ["stream", "saved", "library"]) {
    assert.match(html, new RegExp(`data-view="${view}"`));
  }
  assert.match(html, /id="saved-filters"/);
  assert.match(html, /id="export-saved"/);
  assert.match(html, /id="reader-dialog"/);
  assert.match(html, /id="reader-body"/);
  assert.match(html, /name="keywords"/);
  // The classic three-pane shell and its two separators stay intact.
  assert.equal((html.match(/role="separator"/g) || []).length, 2);
  assert.equal((html.match(/tabindex="0"/g) || []).length, 2);
});

test("CSS switches stream and collection to one centered column", () => {
  const css = read("app.css");
  assert.match(css, /body\[data-view="stream"\] \.layout/);
  assert.match(css, /body\[data-view="saved"\] \.layout/);
  assert.match(css, /body\[data-view="stream"\] \.sidebar[^}]*display: none/);
  assert.match(css, /max-width: 760px; margin: 0 auto/);
  assert.match(css, /\.card-actions/);
  assert.match(css, /\.reader-dialog/);
  // The original three-column grid and its separators are untouched.
  assert.match(css, /var\(--sidebar-width/);
  assert.match(css, /\.sash\s*\{[^}]*cursor: col-resize/);
});

test("app.js wires view switching, card actions, tags and saved-link export", () => {
  const source = read("app.js");
  for (const needle of [
    "sortForStream",
    "hideDismissed",
    "filterByTag",
    "setView(",
    "data-open",
    "data-save",
    "data-dismiss",
    "data-value=\"like\"",
    "data-value=\"later\"",
    "export-saved",
    "/export/saved-links",
    "/cleanup/status",
    "parseKeywords",
    "keywords: parseKeywords",
    "yosee:view",
  ]) {
    assert.ok(source.includes(needle), `missing ${needle}`);
  }
  assert.match(source, /^initResizableLayout\(\);$/m);
});

test("stream cards render a cover image or a lettered placeholder", () => {
  const js = read("app.js");
  for (const needle of [
    "coverMarkup",
    "card-cover",
    "card-cover-fallback",
    "bindCoverFallbacks()",
    'referrerpolicy="no-referrer"',
    'data-cover="image"',
    'data-cover="placeholder"',
    "plainText",
    "cardSummary",
    "articleFragment(item, {",
    "noBody",
    "openOriginal",
  ]) {
    assert.ok(js.includes(needle), `missing ${needle}`);
  }
  assert.ok(!js.includes("summaryText("), "stale summaryText call remains");
  assert.match(js, /<span class="card-cover"/);
  assert.ok(
    !js.includes('<div class="card-cover"'),
    "cover must be phrasing content inside a button"
  );
});

test("cover and link-only styles exist for both themes", () => {
  const css = read("app.css");
  for (const needle of [
    ".card-cover",
    "aspect-ratio",
    "object-fit",
    'data-cover="placeholder"',
    ".no-body",
    ':root[data-theme="dark"] .card-open .card-cover',
  ]) {
    assert.ok(css.includes(needle), `missing ${needle}`);
  }
  assert.match(css, /\.card-open \.card-cover-fallback/);
});
