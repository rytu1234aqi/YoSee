// Three-pane layout with draggable separators.
//
// The pure helpers at the top are unit tested in Node. initResizableLayout owns
// the DOM: Pointer Events for mouse/touch/pen, arrow keys for keyboards,
// double click to reset, and localStorage so the widths survive a reload.

// v2 drops widths saved while a stale cached stylesheet still laid the markup
// out as three columns; those drags measured the wrong panes and could persist
// values as extreme as 160px / 720px.
export const LAYOUT_STORAGE_KEY = "yosee:layout:v2";

export const LIMITS = {
  sidebar: { min: 160, max: 480 },
  feed: { min: 280, max: 720 },
  detailMin: 320,
  detailNarrowMin: 220,
  gutter: 6,
};

export const NARROW_BREAKPOINT = 560;

export const DEFAULTS = { sidebar: 220, feed: 420 };

export function clamp(value, min, max) {
  if (!Number.isFinite(value)) return min;
  return Math.min(Math.max(Math.round(value), min), Math.max(min, max));
}

/** The detail pane needs less room on a narrow window, but never below 220px. */
export function detailMinimum(layoutWidth) {
  const usable = Math.max(0, layoutWidth - LIMITS.gutter * 2);
  const scaled = Math.max(LIMITS.detailNarrowMin, Math.round(usable * 0.28));
  return Math.min(LIMITS.detailMin, scaled);
}

/** Clamp both pane widths so the detail pane keeps its share of the window.

``layoutWidth`` is the full width of the grid, including both separator gutters.
The feed pane gives way first, then the sidebar, which keeps the last dragged
pane as close as possible to the pointer.
*/
export function fitWidths(sidebar, feed, layoutWidth) {
  const usable = Math.max(0, layoutWidth - LIMITS.gutter * 2);
  const budget = Math.max(
    LIMITS.sidebar.min + LIMITS.feed.min,
    usable - detailMinimum(layoutWidth),
  );
  const sidebarMax = Math.min(LIMITS.sidebar.max, budget - LIMITS.feed.min);
  const nextSidebar = clamp(sidebar, LIMITS.sidebar.min, sidebarMax);
  const feedMax = Math.min(LIMITS.feed.max, budget - nextSidebar);
  const nextFeed = clamp(feed, LIMITS.feed.min, feedMax);
  return { sidebar: nextSidebar, feed: nextFeed };
}

/** Parse saved widths, rejecting anything that is not two positive numbers. */
export function readSavedWidths(raw) {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const sidebar = Number(parsed.sidebar);
    const feed = Number(parsed.feed);
    if (!Number.isFinite(sidebar) || !Number.isFinite(feed)) return null;
    if (sidebar <= 0 || feed <= 0) return null;
    return { sidebar, feed };
  } catch {
    return null;
  }
}

export function initResizableLayout(options = {}) {
  const dom = typeof document === "undefined" ? null : document;
  const view = typeof window === "undefined" ? null : window;
  const {
    layout = dom?.querySelector(".layout"),
    sidebar = dom?.querySelector(".sidebar"),
    feedPane = dom?.querySelector(".feed-pane"),
    sashes = dom ? Array.from(dom.querySelectorAll(".sash")) : [],
    storage = view?.localStorage,
    win = view,
    doc = dom,
  } = options;
  if (!layout || !sidebar || !feedPane || sashes.length !== 2) return null;

  const narrow = () => Boolean(
    win?.matchMedia?.(`(max-width: ${NARROW_BREAKPOINT}px)`).matches,
  );
  const layoutWidth = () => layout.clientWidth;
  const measure = () => ({
    sidebar: sidebar.getBoundingClientRect().width,
    feed: feedPane.getBoundingClientRect().width,
  });

  const readSaved = () => {
    try {
      return readSavedWidths(storage?.getItem(LAYOUT_STORAGE_KEY));
    } catch {
      return null;
    }
  };
  const persist = (widths) => {
    try {
      storage?.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(widths));
    } catch {
      // Storage can be unavailable in private browsing; dragging still works.
    }
  };
  const updateAria = (widths) => {
    for (const sash of sashes) {
      const limits = LIMITS[sash.dataset.sash];
      if (!limits) continue;
      sash.setAttribute("aria-valuemin", String(limits.min));
      sash.setAttribute("aria-valuemax", String(limits.max));
      sash.setAttribute("aria-valuenow", String(Math.round(widths[sash.dataset.sash])));
    }
  };
  const apply = (widths, save = true) => {
    layout.style.setProperty("--sidebar-width", `${widths.sidebar}px`);
    layout.style.setProperty("--feed-width", `${widths.feed}px`);
    updateAria(widths);
    if (save) persist(widths);
  };
  const reset = () => {
    layout.style.removeProperty("--sidebar-width");
    layout.style.removeProperty("--feed-width");
    try {
      storage?.removeItem(LAYOUT_STORAGE_KEY);
    } catch {
      // Ignore unavailable storage.
    }
    updateAria(measure());
  };

  const restored = readSaved();
  if (restored && !narrow()) {
    apply(fitWidths(restored.sidebar, restored.feed, layoutWidth()), false);
  } else {
    // No saved widths: leave the responsive CSS fallback untouched.
    updateAria(measure());
  }

  let drag = null;
  let pending = null;
  for (const sash of sashes) {
    const which = sash.dataset.sash;

    sash.addEventListener("pointerdown", (event) => {
      if (narrow()) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      event.preventDefault();
      const start = measure();
      drag = {
        sash,
        startX: event.clientX,
        sidebar: start.sidebar,
        feed: start.feed,
      };
      try {
        sash.setPointerCapture(event.pointerId);
      } catch {
        // Capture is an optimisation; the pointer events still reach the sash.
      }
      sash.classList.add("dragging");
      sash.focus({ preventScroll: true });
      doc?.body?.classList.add("resizing");
    });

    sash.addEventListener("pointermove", (event) => {
      if (!drag || drag.sash !== sash) return;
      const delta = event.clientX - drag.startX;
      const desired = which === "sidebar"
        ? { sidebar: drag.sidebar + delta, feed: drag.feed }
        : { sidebar: drag.sidebar, feed: drag.feed + delta };
      pending = fitWidths(desired.sidebar, desired.feed, layoutWidth());
      apply(pending, false);
    });

    const finish = (event) => {
      if (!drag || drag.sash !== sash) return;
      drag = null;
      sash.classList.remove("dragging");
      doc?.body?.classList.remove("resizing");
      try {
        if (sash.hasPointerCapture(event.pointerId)) {
          sash.releasePointerCapture(event.pointerId);
        }
      } catch {
        // Ignore capture that already ended.
      }
      if (pending) {
        persist(pending);
        pending = null;
      }
    };
    sash.addEventListener("pointerup", finish);
    sash.addEventListener("pointercancel", finish);

    sash.addEventListener("dblclick", (event) => {
      event.preventDefault();
      reset();
    });

    sash.addEventListener("keydown", (event) => {
      if (narrow()) return;
      const step = event.shiftKey ? 48 : 16;
      let delta;
      if (event.key === "ArrowLeft") delta = -step;
      else if (event.key === "ArrowRight") delta = step;
      else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        reset();
        return;
      } else return;
      event.preventDefault();
      const now = measure();
      const desired = which === "sidebar"
        ? { sidebar: now.sidebar + delta, feed: now.feed }
        : { sidebar: now.sidebar, feed: now.feed + delta };
      apply(fitWidths(desired.sidebar, desired.feed, layoutWidth()));
    });
  }

  let frame = 0;
  win?.addEventListener?.("resize", () => {
    if (frame || !win.requestAnimationFrame) return;
    frame = win.requestAnimationFrame(() => {
      frame = 0;
      if (narrow()) return;
      const saved = readSaved();
      if (saved) apply(fitWidths(saved.sidebar, saved.feed, layoutWidth()), false);
    });
  });

  return { reset };
}
