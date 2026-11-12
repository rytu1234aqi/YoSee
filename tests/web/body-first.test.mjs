import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { sortForStream } from "../../web/feed-store.mjs";

const webRoot = new URL("../../web/", import.meta.url);
const read = (name) => readFileSync(new URL(name, webRoot), "utf8");
const repoRoot = new URL("../../", import.meta.url);
const readRepo = (name) => readFileSync(new URL(name, repoRoot), "utf8");

const item = (id, is_read, has_body) => ({ id, is_read, has_body });
const ids = (rows) => rows.map((row) => row.id);

test("without the flag, stream order is exactly the old seen-sinks behaviour", () => {
  const rows = [
    item("read-a", true, true),
    item("fresh-link", false, false),
    item("read-b", true, false),
    item("fresh-body", false, true),
  ];
  assert.deepEqual(ids(sortForStream(rows)), ["fresh-link", "fresh-body", "read-a", "read-b"]);
  assert.deepEqual(sortForStream([]), []);
});

test("the stream puts readable long articles ahead of link-only cards", () => {
  const rows = [
    item("link-1", false, false),
    item("body-1", false, true),
    item("link-2", false, false),
    item("body-2", false, true),
  ];
  assert.deepEqual(ids(sortForStream(rows, true)), ["body-1", "body-2", "link-1", "link-2"]);
});

test("seen cards still sink below every unread card, even readable ones", () => {
  const rows = [
    item("read-body", true, true),
    item("fresh-link", false, false),
    item("read-link", true, false),
    item("fresh-body", false, true),
  ];
  assert.deepEqual(
    ids(sortForStream(rows, true)),
    ["fresh-body", "fresh-link", "read-body", "read-link"],
  );
});

test("grouping is stable and never mutates the loaded page", () => {
  const rows = [
    item("a", false, true),
    item("b", false, false),
    item("c", false, true),
    item("d", false, false),
    item("e", true, true),
    item("f", true, false),
  ];
  const before = rows.map((row) => ({ ...row }));
  assert.deepEqual(ids(sortForStream(rows, true)), ["a", "c", "b", "d", "e", "f"]);
  assert.deepEqual(rows, before);
});

test("only has_body === true counts as readable; legacy rows stay conservative", () => {
  const rows = [
    { id: "legacy", is_read: false },
    item("explicit-false", false, false),
    item("truthy-string", false, "yes"),
    item("real-body", false, true),
  ];
  assert.deepEqual(
    ids(sortForStream(rows, true)),
    ["real-body", "legacy", "explicit-false", "truthy-string"],
  );
});

test("only the stream view asks for body-first ordering", () => {
  const js = read("app.js");
  assert.ok(
    js.includes('sortForStream(hideDismissed(feed.items, dismissed), state.view === "stream")'),
    "the flag must be tied to the stream view only",
  );
  const store = read("feed-store.mjs");
  assert.ok(store.includes("export function sortForStream(items, preferBody = false)"));
  assert.ok(store.includes("item.has_body === true ? readable : links"));
  const readme = readRepo("README.md");
  assert.ok(readme.includes("先刷能读的"));
  assert.ok(readme.includes("100 个来源"));
});
