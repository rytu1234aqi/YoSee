// Keep pagination and request ordering independent of the DOM.
export class FeedStore {
  constructor(request, changed = () => {}) {
    this.request = request;
    this.changed = changed;
    this.filters = {};
    this.items = [];
    this.cursor = null;
    this.loading = false;
    this.loaded = false;
    this.error = "";
    this.generation = 0;
    this.controller = null;
  }

  reset(filters = this.filters) {
    this.controller?.abort();
    this.generation += 1;
    this.filters = { ...filters };
    this.items = [];
    this.cursor = null;
    this.loaded = false;
    this.loading = false;
    return this.loadMore();
  }

  async loadMore() {
    if (this.loading || (this.loaded && !this.cursor)) return;
    const generation = this.generation;
    this.controller = new AbortController();
    this.loading = true;
    this.error = "";
    this.changed();
    const params = new URLSearchParams({ limit: "50", ...this.filters });
    if (this.cursor) params.set("cursor", this.cursor);
    try {
      const page = await this.request(`/feed/page?${params}`, { signal: this.controller.signal });
      if (generation !== this.generation) return;
      const known = new Set(this.items.map((item) => item.id));
      this.items.push(...page.items.filter((item) => !known.has(item.id)));
      this.cursor = page.next_cursor;
      this.loaded = true;
    } catch (error) {
      if (generation === this.generation && error.name !== "AbortError") {
        this.error = error.message || "Could not load articles. Please retry.";
      }
    } finally {
      if (generation === this.generation) {
        this.loading = false;
        this.changed();
      }
    }
  }

  patch(id, values) {
    this.items = this.items.map((item) => item.id === id ? { ...item, ...values } : item)
      .filter((item) => (this.filters.status !== "unread" || !item.is_read)
        && (this.filters.status !== "saved" || item.saved));
    this.changed();
  }

  dismiss(id) {
    this.items = this.items.filter((item) => item.id !== id);
    this.changed();
  }
}

// "Seen" cards sink to the bottom of the single-column stream. Order inside
// each group is preserved so cursor paging never reshuffles what was loaded.
// When the stream view asks for it, cards with stored article text come first
// so link-only cards cannot crowd out the long reads. This is only a display
// order: nothing is removed, and the seen/unseen split still wins over it.
export function sortForStream(items, preferBody = false) {
  const fresh = [];
  const seen = [];
  for (const item of items) (item.is_read ? seen : fresh).push(item);
  if (!preferBody) return fresh.concat(seen);
  const bodyFirst = (group) => {
    const readable = [];
    const links = [];
    for (const item of group) (item.has_body === true ? readable : links).push(item);
    return readable.concat(links);
  };
  return bodyFirst(fresh).concat(bodyFirst(seen));
}

// Dismissed cards leave the current screen immediately; they are still in
// the database (just marked read) and may reappear on the next reload.
export function hideDismissed(items, dismissed) {
  return items.filter((item) => !dismissed.has(item.id));
}

// A link-only card has no stored article text. Clicking it should go straight
// to the original site instead of opening an empty reader.
export function opensOriginal(item) {
  return !!item && item.has_body === false;
}

// The collection page has a small "liked / read later" filter.
export function filterByTag(items, tag) {
  if (!tag) return items;
  return items.filter((item) => item.tag === tag);
}

// One stream round shows a random subset of sources. Fisher-Yates keeps the
// pick unbiased; sorting the result makes the query string stable for caching.
export function pickRoundSources(ids, size, random = Math.random) {
  const pool = [...ids];
  for (let i = pool.length - 1; i > 0; i -= 1) {
    const j = Math.floor(random() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  return pool.slice(0, Math.max(0, Math.min(size, pool.length))).sort();
}
