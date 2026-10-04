import test from "node:test";
import assert from "node:assert/strict";
import { FeedStore } from "../../web/feed-store.mjs";
import { looksLikeHtml, safeUrl } from "../../web/article.mjs";

test("load more appends, sends the cursor, and stops at the end", async () => {
  const urls = [];
  const pages = [
    { items: [{ id: "a" }], next_cursor: "next" },
    { items: [{ id: "b" }], next_cursor: null },
  ];
  const store = new FeedStore(async (url) => { urls.push(url); return pages.shift(); });
  await store.reset({ status: "saved", source_id: "godot", q: "render" });
  await store.loadMore();
  await store.loadMore();
  assert.deepEqual(store.items.map((item) => item.id), ["a", "b"]);
  assert.equal(urls.length, 2);
  assert.equal(new URL(urls[1], "http://local").searchParams.get("cursor"), "next");
  assert.equal(new URL(urls[1], "http://local").searchParams.get("source_id"), "godot");
});

test("changing filters cancels old work and ignores late responses", async () => {
  const pending = [];
  const store = new FeedStore((url, options) => new Promise((resolve) => {
    pending.push({ url, options, resolve });
  }));
  const old = store.reset({ status: "all" });
  const fresh = store.reset({ status: "saved" });
  assert.equal(pending[0].options.signal.aborted, true);
  pending[1].resolve({ items: [{ id: "new" }], next_cursor: null });
  await fresh;
  pending[0].resolve({ items: [{ id: "old" }], next_cursor: "obsolete" });
  await old;
  assert.deepEqual(store.items, [{ id: "new" }]);
  assert.equal(store.cursor, null);
  assert.equal(store.loading, false);
});

test("load more failure preserves the page and retries the same cursor", async () => {
  let attempt = 0;
  const cursors = [];
  const store = new FeedStore(async (url) => {
    cursors.push(new URL(url, "http://local").searchParams.get("cursor"));
    attempt += 1;
    if (attempt === 1) return { items: [{ id: "first" }], next_cursor: "next" };
    if (attempt === 2) throw new Error("Network unavailable");
    return { items: [{ id: "last" }], next_cursor: null };
  });
  await store.reset();
  await store.loadMore();
  assert.equal(store.error, "Network unavailable");
  assert.equal(store.items.length, 1);
  assert.equal(store.cursor, "next");
  await store.loadMore();
  assert.equal(store.error, "");
  assert.equal(store.items.length, 2);
  assert.deepEqual(cursors, [null, "next", "next"]);
});

test("double clicks do not issue overlapping page requests", async () => {
  let resolve;
  let calls = 0;
  const store = new FeedStore(() => {
    calls += 1;
    return new Promise((done) => { resolve = done; });
  });
  const first = store.reset();
  await store.loadMore();
  assert.equal(calls, 1);
  resolve({ items: [], next_cursor: null });
  await first;
});

test("state changes update all views and preserve the next page cursor", async () => {
  const store = new FeedStore(async () => ({
    items: [{ id: "a", saved: true, is_read: false }, { id: "b", saved: true, is_read: false }],
    next_cursor: "next",
  }));
  await store.reset({ status: "all" });
  store.patch("a", { is_read: true });
  assert.equal(store.items[0].is_read, true);
  await store.reset({ status: "saved" });
  store.patch("a", { saved: false });
  assert.deepEqual(store.items.map((item) => item.id), ["b"]);
  assert.equal(store.cursor, "next");
  await store.reset({ status: "unread" });
  store.patch("a", { is_read: true });
  assert.deepEqual(store.items.map((item) => item.id), ["b"]);
});

test("external links only accept web URLs and resolve relative article links", () => {
  for (const value of ["javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "java\nscript:alert(1)"]) {
    assert.equal(safeUrl(value), null);
  }
  assert.equal(safeUrl("../guide", "https://example.test/blog/post"), "https://example.test/guide");
  assert.equal(safeUrl(null, "https://example.test"), null);
});

test("detects HTML articles mislabeled as plain text without treating ordinary prose as HTML", () => {
  assert.equal(looksLikeHtml('<p>Article <strong>body</strong></p>'), true);
  assert.equal(looksLikeHtml('  <section class="post">Text</section>'), true);
  assert.equal(looksLikeHtml('Use x < 5 and y > 3.'), false);
  assert.equal(looksLikeHtml(null), false);
});
