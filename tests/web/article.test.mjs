import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { plainText } from "../../web/article.mjs";

const webRoot = new URL("../../web/", import.meta.url);
const read = (name) => readFileSync(new URL(name, webRoot), "utf8");

test("plainText strips raw HTML tags instead of printing them as prose", () => {
  assert.equal(
    plainText('<p><a href="https://lobste.rs/s/x">Comments</a></p>'),
    "Comments"
  );
  assert.equal(plainText("<p>Hello <strong>world</strong></p>"), "Hello world");
  assert.equal(plainText("<div>A</div><div>B</div>"), "A B");
});

test("plainText also handles HTML-escaped markup and entities", () => {
  assert.equal(plainText("&lt;p&gt;Comments&lt;/p&gt;"), "Comments");
  assert.equal(plainText("Fish &amp; chips"), "Fish & chips");
  assert.equal(plainText("a&nbsp;b"), "a b");
  // A doubly escaped value stays literal text; do not turn it into a tag.
  assert.equal(plainText("&amp;lt;p&amp;gt;"), "&lt;p&gt;");
});

test("plainText ignores markup hidden in scripts and styles", () => {
  assert.equal(
    plainText('<style>img{}</style><script>var x = "<img>";</script><p>Real text here.</p>'),
    "Real text here."
  );
});

test("plainText keeps ordinary prose unchanged and never throws", () => {
  assert.equal(plainText("  一篇正常的中文摘要，讲了点东西。  "), "一篇正常的中文摘要，讲了点东西。");
  assert.equal(plainText(""), "");
  assert.equal(plainText(null), "");
  assert.equal(plainText(undefined), "");
  assert.equal(plainText("<"), "<");
  assert.equal(plainText("<>"), "<>");
});

test("a link-only article gets a plain-language note instead of orphan markup", () => {
  const source = read("article.mjs");
  assert.match(source, /export function articleFragment\(item, labels = \{\}\)/);
  assert.match(source, /className = "no-body"/);
  assert.match(source, /isEmptyBody\(plain\)/);
  assert.match(source, /NO_BODY_LABELS/);
  assert.match(source, /labels\.noBody/);
  assert.match(source, /labels\.openOriginal/);
  assert.match(source, /rel = "noopener noreferrer"/);
});
