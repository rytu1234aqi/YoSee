import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { safeUrl } from "../../web/article.mjs";

const webRoot = new URL("../../web/", import.meta.url);
const read = (name) => readFileSync(new URL(name, webRoot), "utf8");

test("rejects dangerous URL schemes even when obfuscated", () => {
  const payloads = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "blob:https://example.test/evil",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "\u0000javascript:alert(1)",
  ];
  for (const value of payloads) assert.equal(safeUrl(value), null, value);
});

test("accepts only http(s) absolute URLs and resolves relative article links", () => {
  assert.equal(safeUrl("https://example.test/x"), "https://example.test/x");
  assert.equal(safeUrl("http://example.test/x"), "http://example.test/x");
  assert.equal(
    safeUrl("../guide", "https://example.test/blog/post"),
    "https://example.test/guide"
  );
  assert.equal(safeUrl("//evil.test/x", "https://example.test/"), "https://evil.test/x");
  assert.equal(safeUrl(""), null);
});

test("the renderer normalizes tag names and rejects non-HTML namespaces", () => {
  const source = read("article.mjs");
  assert.match(source, /toUpperCase\(\)/);
  assert.match(source, /namespaceURI/);
  assert.match(source, /dropped\.has/);
});

test("every API request carries the per-process local token", () => {
  const source = read("app.js");
  assert.match(source, /X-Local-Token/);
  assert.match(source, /name="local-token"/);
  assert.match(source, /withToken\(\)/);
});
