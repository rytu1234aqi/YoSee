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

export function articleFragment(item) {
  const fragment = document.createDocumentFragment();
  const html = item.content_html || (looksLikeHtml(item.content_text) ? item.content_text : null);
  if (!html) {
    const text = document.createElement("p");
    text.className = "plain-body";
    text.textContent = item.content_text || item.summary || "No saved text. Open the original page.";
    fragment.append(text);
    return fragment;
  }
  const parsed = new DOMParser().parseFromString(html, "text/html");
  function copy(node, parent) {
    if (node.nodeType === Node.TEXT_NODE) {
      parent.append(document.createTextNode(node.textContent));
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE || dropped.has(node.tagName)) return;
    if (!allowed.has(node.tagName)) {
      for (const child of node.childNodes) copy(child, parent);
      return;
    }
    const element = document.createElement(node.tagName.toLowerCase());
    if (node.tagName === "A") {
      const href = safeUrl(node.getAttribute("href"), item.canonical_url);
      if (href) {
        element.href = href;
        element.target = "_blank";
        element.rel = "noopener noreferrer";
      }
    }
    if (node.tagName === "IMG") {
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
