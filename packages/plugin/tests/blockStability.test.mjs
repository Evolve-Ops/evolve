/**
 * Tests for BlockStability — byte-stable injection blocks (post-mortem §2).
 *
 * Pins:
 *   1. StickyBlockCache: fresh-within-TTL, success replaces (including a
 *      legitimately-empty success), failure serves last-good, failure with
 *      no last-good serves "", failure past maxStale degrades to "".
 *   2. Failure re-anchors the TTL (persistent fault retries once per
 *      window, not every turn) WITHOUT refreshing the last-good age.
 *   3. NarrativeStableCache: identical text → byte-identical block even
 *      when generated_at changed; changed text → fresh render with the
 *      new timestamp.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/blockStability.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  StickyBlockCache,
  NarrativeStableCache,
  renderHomeNarrativeBlock,
} from "../dist/observer/BlockStability.js";

const TTL = 1000;
const MAX_STALE = 10_000;

test("fresh within TTL, null after", () => {
  const c = new StickyBlockCache(TTL, MAX_STALE);
  assert.equal(c.getFresh(0), null);
  c.storeSuccess("A", 0);
  assert.equal(c.getFresh(999), "A");
  assert.equal(c.getFresh(1000), null);
});

test("failure serves last-good instead of flapping to empty", () => {
  const c = new StickyBlockCache(TTL, MAX_STALE);
  c.storeSuccess("A", 0);
  assert.equal(c.storeFailure(1500), "A");
  // Re-anchored: fresh again for a TTL, so the fault isn't retried per-turn.
  assert.equal(c.getFresh(2000), "A");
});

test("legitimately-empty success replaces last-good", () => {
  const c = new StickyBlockCache(TTL, MAX_STALE);
  c.storeSuccess("A", 0);
  c.storeSuccess("", 1100);   // e.g. last skill uninstalled — "" is the truth
  assert.equal(c.storeFailure(2500), "");
});

test("failure with no prior success serves empty", () => {
  const c = new StickyBlockCache(TTL, MAX_STALE);
  assert.equal(c.storeFailure(0), "");
  assert.equal(c.getFresh(500), "");
});

test("stale bound: last-good older than maxStale degrades to empty", () => {
  const c = new StickyBlockCache(TTL, MAX_STALE);
  c.storeSuccess("A", 0);
  // Repeated failures re-anchor the TTL but must NOT refresh goodAt.
  assert.equal(c.storeFailure(4000), "A");
  assert.equal(c.storeFailure(8000), "A");
  assert.equal(c.staleAgeMs(8000), 8000);
  assert.equal(c.storeFailure(10_001), "");
});

test("narrative: identical text is byte-identical across generated_at bumps", () => {
  const n = new NarrativeStableCache();
  const a = n.render("pod is healthy", "2026-07-31T08:00:00Z");
  const b = n.render("pod is healthy", "2026-07-31T09:00:00Z");
  assert.equal(a, b);
  assert.match(a, /Generated 2026-07-31T08:00:00Z/);
});

test("narrative: changed text re-renders with the new timestamp", () => {
  const n = new NarrativeStableCache();
  n.render("pod is healthy", "2026-07-31T08:00:00Z");
  const c = n.render("pod has 1 alert", "2026-07-31T10:00:00Z");
  assert.match(c, /pod has 1 alert/);
  assert.match(c, /Generated 2026-07-31T10:00:00Z/);
});

test("narrative block format is the session_surface contract", () => {
  const block = renderHomeNarrativeBlock("body", "TS");
  assert.match(block, /^\[CURRENT POD REPORT — shown to admin above this chat on the home page\]/);
  assert.match(block, /answer from this text rather/);
  assert.match(block, /\(Generated TS\. May be moments older than the/);
});

test("narrative: empty generated_at omits the timestamp line", () => {
  const block = renderHomeNarrativeBlock("body", "");
  assert.doesNotMatch(block, /Generated/);
});
