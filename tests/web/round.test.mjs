import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pickRoundSources } from "../../web/feed-store.mjs";

const webRoot = new URL("../../web/", import.meta.url);
const read = (name) => readFileSync(new URL(name, webRoot), "utf8");

test("pickRoundSources returns a bounded, sorted, non-mutating subset", () => {
  const ids = ["a", "b", "c", "d", "e"];
  const pick = pickRoundSources(ids, 3, () => 0);
  assert.equal(pick.length, 3);
  assert.deepEqual([...pick].sort(), pick);
  assert.equal(new Set(pick).size, 3);
  assert.ok(pick.every((id) => ids.includes(id)));
  assert.deepEqual(ids, ["a", "b", "c", "d", "e"]);
  assert.deepEqual(pickRoundSources([], 15), []);
  assert.deepEqual(pickRoundSources(ids, 99), [...ids].sort());
  assert.deepEqual(pickRoundSources(ids, 0), []);
});

test("the round is chosen from sources, persisted, and sent as source_ids", () => {
  const html = read("index.html");
  assert.ok(html.includes('id="shuffle-round"'));
  assert.ok(html.includes('id="round-bar"'));
  assert.ok(html.includes('id="round-label"'));
  assert.ok(html.includes('data-i18n="shuffleRound"'));

  const js = read("app.js");
  for (const needle of [
    "pickRoundSources", "ROUND_SIZE = 15", 'ROUND_KEY = "yosee:round"',
    "roundSources", "source_ids", "shuffleRound", "roundPool", "readRound",
    "saveRound", "ensureRound", "renderRoundLabel", "sourcesReady",
  ]) {
    assert.ok(js.includes(needle), `missing ${needle}`);
  }
  // Only the stream view is scoped to a round; library/collection/today stay
  // on the old unfiltered query.
  assert.ok(js.includes('if (state.view === "stream") {\n    const round = ensureRound();'));
  assert.ok(js.includes('filters.source_ids = round.join(",")'));
  assert.match(js, /\$\("round-bar"\)\.hidden = view !== "stream"/);
  assert.ok(js.includes('$("shuffle-round").addEventListener("click", shuffleRound);'));
  // The first feed request must wait for /sources, or the round would be empty.
  assert.ok(js.includes("const sourcesReady = loadSources();"));
  assert.ok(js.includes("sourcesReady.then(() => pollSync().then(resetFeed));"));

  const css = read("app.css");
  assert.ok(css.includes(".round-bar"));
  assert.ok(css.includes(".shuffle-button"));
  assert.ok(css.includes('.shuffle-button:hover'));
});

test("round labels exist in both languages", () => {
  const js = read("app.js");
  for (const key of ["shuffleRound", "roundLabel", "roundLabelTail", "roundEmpty", "roundEmptyFeed"]) {
    const count = js.split(new RegExp(`\\b${key}\\s*:`)).length - 1;
    assert.equal(count, 2, `${key} needs one zh and one en label, got ${count}`);
  }
  assert.ok(js.includes('const emptyText = state.view === "stream" ? t("roundEmptyFeed") : t("empty");'));
});
