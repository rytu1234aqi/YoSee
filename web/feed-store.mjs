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
}
