// Copy external HTML through an allowlist, never into the application DOM directly.
const allowed = new Set([
  "P", "BR", "H1", "H2", "H3", "H4", "H5", "H6", "UL", "OL", "LI", "STRONG", "EM",
  "B", "I", "U", "S", "BLOCKQUOTE", "PRE", "CODE", "HR", "A", "IMG", "FIGURE",
  "FIGCAPTION", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "DIV", "SPAN", "SUP", "SUB",
]);
const dropped = new Set([
  "SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "FORM", "INPUT", "BUTTON", "TEXTAREA",
  "SELECT", "SVG", "MATH", "LINK", "META", "BASE", "TEMPLATE", "NOSCRIPT",
]);

export function safeUrl(value, base) {
  if (!value) return null;
  try {
    const url = new URL(value, base);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

export function looksLikeHtml(value) {
  // Some feeds label a complete HTML article as text/plain.
  return /^\s*<(?:p|div|section|article|h[1-6]|ul|ol|blockquote)(?:\s[^>]*|)>/i.test(value || "");
}

export function summaryText(value) {
  return new DOMParser().parseFromString(value || "", "text/html").body.textContent.trim();
}

// Summaries from link-aggregator feeds are often raw HTML such as
// `<p><a href="...">Comments</a></p>`, or that same markup HTML-escaped.
// Never print those tags as prose. This version avoids DOMParser so it is
// also usable in Node unit tests.
export function plainText(value) {
  if (!value) return "";
  const raw = String(value).trim();
  if (!raw) return "";
  // Decode entities once so escaped markup (&lt;p&gt;) is cleaned too.
  // &amp; goes last: "&amp;lt;" must become "&lt;", not "<".
  const decoded = raw
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&(?:#39|#x27|apos);/gi, "'")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&");
  if (!/<[a-z][^>]*>/i.test(decoded)) return decoded.replace(/\s+/g, " ").trim();
  return decoded
    .replace(/<\s*(?:script|style)\b[^>]*>[\s\S]*?<\s*\/\s*(?:script|style)\s*>/gi, " ")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// A one-word link label is not article text; it means the feed only gave us
// a URL. Say so plainly instead of dumping markup or an orphan word.
const NO_BODY_LABELS = /^(?:comments?|read more|continue reading|link|discuss(?:ion)?|原文链接|查看原文|阅读全文)$/i;

function isEmptyBody(plain) {
  return !plain || plain.length < 24 || NO_BODY_LABELS.test(plain);
}

function noBodyNote(item, labels) {
  const box = document.createElement("div");
  box.className = "no-body";
  const line = document.createElement("p");
  line.textContent = labels.noBody || "这篇只存了链接，没有存下正文。";
  box.append(line);
  const url = safeUrl(item.canonical_url);
  if (url) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = labels.openOriginal || "打开原文 ↗";
    box.append(link);
  }
  return box;
}

export function articleFragment(item, labels = {}) {
  const fragment = document.createDocumentFragment();
  const html = item.content_html || (looksLikeHtml(item.content_text) ? item.content_text : null);
  if (!html) {
    const plain = plainText(item.content_text) || plainText(item.summary);
    if (isEmptyBody(plain)) {
      fragment.append(noBodyNote(item, labels));
      return fragment;
    }
    const text = document.createElement("p");
    text.className = "plain-body";
    text.textContent = plain;
    fragment.append(text);
    return fragment;
  }
  const parsed = new DOMParser().parseFromString(html, "text/html");
  function copy(node, parent) {
    if (node.nodeType === Node.TEXT_NODE) {
      parent.append(document.createTextNode(node.textContent));
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const tag = (node.tagName || "").toUpperCase();
    if (dropped.has(tag) || node.namespaceURI !== "http://www.w3.org/1999/xhtml") return;
    if (!allowed.has(tag)) {
      for (const child of node.childNodes) copy(child, parent);
      return;
    }
    const element = document.createElement(tag.toLowerCase());
    if (tag === "A") {
      const href = safeUrl(node.getAttribute("href"), item.canonical_url);
      if (href) {
        element.href = href;
        element.target = "_blank";
        element.rel = "noopener noreferrer";
      }
    }
    if (tag === "IMG") {
      const src = safeUrl(node.getAttribute("src"), item.canonical_url);
      if (!src) return;
      element.src = src;
      element.alt = node.getAttribute("alt") || "";
      element.loading = "lazy";
      element.referrerPolicy = "no-referrer";
    }
    for (const child of node.childNodes) copy(child, element);
    parent.append(element);
  }
  for (const child of parsed.body.childNodes) copy(child, fragment);
  return fragment;
}
