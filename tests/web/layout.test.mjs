import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  DEFAULTS,
  LAYOUT_STORAGE_KEY,
  LIMITS,
  NARROW_BREAKPOINT,
  clamp,
  detailMinimum,
  fitWidths,
  initResizableLayout,
  readSavedWidths,
} from "../../web/layout.mjs";

const webRoot = new URL("../../web/", import.meta.url);
const read = (name) => readFileSync(new URL(name, webRoot), "utf8");

test("clamp bounds values and treats non-finite input as the minimum", () => {
  assert.equal(clamp(220.6, 160, 480), 221);
  assert.equal(clamp(50, 160, 480), 160);
  assert.equal(clamp(900, 160, 480), 480);
  assert.equal(clamp(Number.NaN, 160, 480), 160);
  assert.equal(clamp(Number.POSITIVE_INFINITY, 160, 480), 160);
});

test("default widths sit inside the configured limits", () => {
  assert.ok(DEFAULTS.sidebar >= LIMITS.sidebar.min && DEFAULTS.sidebar <= LIMITS.sidebar.max);
  assert.ok(DEFAULTS.feed >= LIMITS.feed.min && DEFAULTS.feed <= LIMITS.feed.max);
  assert.equal(LAYOUT_STORAGE_KEY, "yosee:layout:v2");
});

test("fitWidths keeps a roomy layout unchanged", () => {
  assert.deepEqual(fitWidths(220, 420, 1440), { sidebar: 220, feed: 420 });
});

test("fitWidths clamps each pane to its own min and max", () => {
  assert.deepEqual(fitWidths(1, 1, 1440), { sidebar: 160, feed: 280 });
  assert.deepEqual(fitWidths(9999, 9999, 1440), { sidebar: 480, feed: 628 });
});

test("fitWidths never lets the detail pane fall below its minimum", () => {
  for (const width of [1440, 1000, 800, 720]) {
    for (const [sidebar, feed] of [
      [480, 720],
      [220, 9999],
      [9999, 420],
      [400, 700],
    ]) {
      const fitted = fitWidths(sidebar, feed, width);
      const usable = width - LIMITS.gutter * 2;
      assert.ok(
        usable - fitted.sidebar - fitted.feed >= detailMinimum(width),
        `${width}px: ${JSON.stringify(fitted)} leaves too little detail space`,
      );
      assert.ok(Number.isFinite(fitted.sidebar) && Number.isFinite(fitted.feed));
    }
  }
});

test("detailMinimum scales down on narrow windows but keeps a usable floor", () => {
  assert.equal(detailMinimum(1440), LIMITS.detailMin);
  assert.equal(detailMinimum(800), 221);
  assert.equal(detailMinimum(500), LIMITS.detailNarrowMin);
  assert.equal(detailMinimum(0), LIMITS.detailNarrowMin);
});

test("a preview-width window still fits three side-by-side panes", () => {
  const fitted = fitWidths(180, 280, 800);
  assert.deepEqual(fitted, { sidebar: 180, feed: 280 });
  const usable = 800 - LIMITS.gutter * 2;
  assert.ok(usable - fitted.sidebar - fitted.feed >= 300);
  assert.equal(NARROW_BREAKPOINT, 560);
});

test("fitWidths degrades gracefully on very narrow viewports", () => {
  for (const width of [900, 700, 500, 320, 0]) {
    const fitted = fitWidths(220, 420, width);
    assert.ok(Number.isFinite(fitted.sidebar) && Number.isFinite(fitted.feed));
    assert.ok(fitted.sidebar >= LIMITS.sidebar.min);
    assert.ok(fitted.feed >= LIMITS.feed.min);
  }
});

test("readSavedWidths accepts only two positive numbers", () => {
  assert.deepEqual(readSavedWidths('{"sidebar":260,"feed":420}'), {
    sidebar: 260,
    feed: 420,
  });
  for (const raw of [
    "",
    "null",
    "not json",
    "{}",
    '{"sidebar":260}',
    '{"sidebar":-5,"feed":420}',
    '{"sidebar":null,"feed":420}',
    '{"sidebar":"wide","feed":420}',
    "[260,420]",
    undefined,
  ]) {
    assert.equal(readSavedWidths(raw), null, String(raw));
  }
});

test("markup exposes two keyboard-accessible separators", () => {
  const html = read("index.html");
  assert.match(html, /data-sash="sidebar"/);
  assert.match(html, /data-sash="feed"/);
  assert.equal((html.match(/role="separator"/g) || []).length, 2);
  assert.equal((html.match(/tabindex="0"/g) || []).length, 2);
  assert.match(html, /data-i18n-aria-label="resizeSidebar"/);
  assert.match(html, /data-i18n-aria-label="resizeFeed"/);
});

test("CSS keeps three panes at preview width and only stacks below the breakpoint", () => {
  const css = read("app.css");
  assert.match(css, /var\(--sidebar-width/);
  assert.match(css, /var\(--feed-width/);
  assert.match(css, /\.sash\s*\{[^}]*cursor: col-resize/);
  assert.match(css, /body\.resizing/);
  assert.match(css, /@media \(min-width: 561px\) and \(max-width: 1000px\)/);
  assert.match(css, /@media \(max-width: 560px\)[^@]*\.sash \{ display: none; \}/);
  assert.match(css, /@media \(max-width: 560px\)[^@]*\.layout \{ display: block; \}/);
  assert.doesNotMatch(css, /@media \(max-width: 900px\)/);
  assert.doesNotMatch(css, /@media \(max-width: 700px\)/);
  assert.match(css, /@media \(min-width: 561px\) and \(max-width: 1000px\)[^\n]*180px/);
  assert.match(css, /\.source-row \.source span \{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; \}/);
});

test("the JS narrow breakpoint matches the CSS stacking breakpoint", () => {
  const source = read("layout.mjs");
  assert.match(source, /NARROW_BREAKPOINT = 560/);
  assert.match(source, /max-width: \$\{NARROW_BREAKPOINT\}px/);
  assert.equal(NARROW_BREAKPOINT, 560);
});

test("app.js wires up the layout module and translates separator hints", () => {
  const source = read("app.js");
  assert.match(source, /import \{ initResizableLayout \} from "\/static\/layout\.mjs"/);
  assert.match(source, /^initResizableLayout\(\);$/m);
  assert.match(source, /data-i18n-aria-label/);
  assert.match(source, /data-i18n-title/);
  assert.match(source, /resizeSidebar/);
  assert.match(source, /resizeFeed/);
});

test("the shell fingerprints assets and ships the ?debug=1 geometry probe", () => {
  const html = read("index.html");
  assert.match(html, /href="\/static\/app\.css\?v=__ASSET_VERSION__"/);
  assert.match(html, /src="\/static\/app\.js\?v=__ASSET_VERSION__"/);
  assert.match(html, /id="debug-info" class="debug-info" hidden/);

  const css = read("app.css");
  assert.match(css, /\.debug-info \{[^}]*position: fixed/);

  const js = read("app.js");
  assert.match(js, /new URLSearchParams\(window\.location\.search\)\.has\("debug"\)/);
  assert.match(js, /getComputedStyle\(grid\)\.gridTemplateColumns/);
  assert.match(js, /detailY === feedY \? "三栏同排" : "详情换行"/);
  assert.match(js, /fetch\(`\/health\?\$\{new URLSearchParams\(info\)\}`/);
  assert.match(js, /window\.addEventListener\("resize", report\)/);

  const layout = read("layout.mjs");
  assert.match(layout, /LAYOUT_STORAGE_KEY = "yosee:layout:v2"/);
});

function fakeDom() {
  const listeners = () => {
    const handlers = new Map();
    return {
      add(type, fn) {
        if (!handlers.has(type)) handlers.set(type, []);
        handlers.get(type).push(fn);
      },
      emit(type, event) {
        for (const fn of handlers.get(type) || []) fn(event);
      },
    };
  };
  const make = (events, attrs, extra) => ({
    dataset: {},
    attrs,
    classList: { add() {}, remove() {} },
    setAttribute(key, value) { attrs[key] = value; },
    addEventListener: events.add,
    emit: events.emit,
    setPointerCapture() {},
    hasPointerCapture() { return true; },
    releasePointerCapture() {},
    focus() {},
    ...extra,
  });

  const layoutEvents = listeners();
  const styleValues = {};
  const layout = make(layoutEvents, {}, {
    clientWidth: 1440,
    styleValues,
    style: {
      setProperty(key, value) { styleValues[key] = value; },
      removeProperty(key) { delete styleValues[key]; },
    },
  });
  const sidebar = make(listeners(), {}, {
    getBoundingClientRect: () => ({
      width: styleValues["--sidebar-width"]
        ? Number.parseFloat(styleValues["--sidebar-width"])
        : 220,
    }),
  });
  const feedPane = make(listeners(), {}, {
    getBoundingClientRect: () => ({
      width: styleValues["--feed-width"]
        ? Number.parseFloat(styleValues["--feed-width"])
        : 420,
    }),
  });
  const left = make(listeners(), {}, {});
  const right = make(listeners(), {}, {});
  left.dataset.sash = "sidebar";
  right.dataset.sash = "feed";

  const data = new Map();
  const storage = {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
  const win = {
    localStorage: storage,
    matchMedia: () => ({ matches: false }),
    addEventListener() {},
    requestAnimationFrame: (fn) => { fn(); return 1; },
  };
  const doc = { body: { classList: { add() {}, remove() {} } } };
  return { layout, sidebar, feedPane, sashes: [left, right], storage, win, doc, styleValues, data };
}

test("dragging a separator updates CSS variables and persists the widths", () => {
  const dom = fakeDom();
  assert.ok(initResizableLayout(dom));

  const [left, right] = dom.sashes;
  const pointer = (clientX) => ({
    clientX, pointerId: 1, pointerType: "mouse", button: 0, preventDefault() {},
  });
  left.emit("pointerdown", pointer(300));
  left.emit("pointermove", pointer(360));
  left.emit("pointerup", pointer(360));
  assert.equal(dom.styleValues["--sidebar-width"], "280px");
  assert.equal(dom.styleValues["--feed-width"], "420px");
  assert.deepEqual(JSON.parse(dom.storage.getItem(LAYOUT_STORAGE_KEY)), {
    sidebar: 280,
    feed: 420,
  });

  right.emit("pointerdown", pointer(700));
  right.emit("pointermove", pointer(800));
  right.emit("pointerup", pointer(800));
  assert.equal(dom.styleValues["--feed-width"], "520px");
  assert.equal(dom.styleValues["--sidebar-width"], "280px");
});

test("arrow keys nudge a pane and double click restores the responsive default", () => {
  const dom = fakeDom();
  initResizableLayout(dom);
  const [left] = dom.sashes;

  left.emit("keydown", { key: "ArrowRight", shiftKey: false, preventDefault() {} });
  assert.equal(dom.styleValues["--sidebar-width"], "236px");
  assert.deepEqual(JSON.parse(dom.storage.getItem(LAYOUT_STORAGE_KEY)), {
    sidebar: 236,
    feed: 420,
  });

  left.emit("dblclick", { preventDefault() {} });
  assert.equal(dom.styleValues["--sidebar-width"], undefined);
  assert.equal(dom.styleValues["--feed-width"], undefined);
  assert.equal(dom.storage.getItem(LAYOUT_STORAGE_KEY), null);
});

test("saved widths are restored on the next init", () => {
  const dom = fakeDom();
  dom.storage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify({ sidebar: 300, feed: 500 }));
  initResizableLayout(dom);
  assert.equal(dom.styleValues["--sidebar-width"], "300px");
  assert.equal(dom.styleValues["--feed-width"], "500px");
  assert.equal(dom.sashes[0].attrs["aria-valuenow"], "300");
});

test("layout module supports pointer dragging, keyboard nudges and reset", () => {
  const source = read("layout.mjs");
  for (const needle of [
    "pointerdown",
    "pointermove",
    "pointerup",
    "pointercancel",
    "setPointerCapture",
    "dblclick",
    "keydown",
    "ArrowLeft",
    "ArrowRight",
    "localStorage",
    "removeProperty(\"--sidebar-width\")",
  ]) {
    assert.ok(source.includes(needle), `missing ${needle}`);
  }
});
