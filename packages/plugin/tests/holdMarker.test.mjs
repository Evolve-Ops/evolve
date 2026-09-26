/**
 * Interactive-hold evidence marker (D-CS13;
 * internal/decision-cost-single-turn-2026-09-18.md).
 *
 * Chip: internal/dispatch/done/checkpoint-hold-is-evidenced-on-the-trip-record.md.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/holdMarker.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { recordInteractiveHold, PLUGIN_VERSION } from "../dist/breakers/HoldMarker.js";

function tmpSharedDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "evolve-holdmarker-"));
}

function readRows(shared, botId) {
  const p = path.join(shared, "breakers", botId, "holds.jsonl");
  if (!fs.existsSync(p)) return [];
  return fs.readFileSync(p, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

test("appends one row with the expected fields", () => {
  const shared = tmpSharedDir();
  recordInteractiveHold({
    sharedDir: shared, botId: "team_bot_a", tripId: "trip-1",
    session: "slack:C123", pluginVersion: PLUGIN_VERSION,
    now: new Date("2026-09-17T19:52:47.000Z"),
  });
  const rows = readRows(shared, "team_bot_a");
  assert.equal(rows.length, 1);
  assert.deepEqual(rows[0], {
    trip_id: "trip-1", ts: "2026-09-17T19:52:47.000Z",
    session: "slack:C123", plugin_version: PLUGIN_VERSION,
  });
});

test("a second hold appends rather than overwrites", () => {
  const shared = tmpSharedDir();
  recordInteractiveHold({
    sharedDir: shared, botId: "team_bot_a", tripId: "trip-1",
    session: "slack:C1", pluginVersion: PLUGIN_VERSION,
  });
  recordInteractiveHold({
    sharedDir: shared, botId: "team_bot_a", tripId: "trip-1",
    session: "slack:C2", pluginVersion: PLUGIN_VERSION,
  });
  assert.equal(readRows(shared, "team_bot_a").length, 2);
});

test("an empty trip_id is a no-op — nothing to correlate a marker against", () => {
  const shared = tmpSharedDir();
  recordInteractiveHold({
    sharedDir: shared, botId: "team_bot_a", tripId: "",
    session: null, pluginVersion: PLUGIN_VERSION,
  });
  assert.equal(fs.existsSync(path.join(shared, "breakers", "team_bot_a", "holds.jsonl")), false);
});

test("a write failure is swallowed, not thrown", () => {
  const shared = tmpSharedDir();
  // Make the bot's breaker dir a file, not a directory, so mkdirSync fails.
  fs.mkdirSync(path.join(shared, "breakers"), { recursive: true });
  fs.writeFileSync(path.join(shared, "breakers", "team_bot_a"), "not a dir");
  const logs = [];
  assert.doesNotThrow(() => recordInteractiveHold({
    sharedDir: shared, botId: "team_bot_a", tripId: "trip-1",
    session: null, pluginVersion: PLUGIN_VERSION,
    logger: { warn: (m) => logs.push(m) },
  }));
  assert.ok(logs.length >= 1);
});

test("two different bots get two different marker files", () => {
  const shared = tmpSharedDir();
  recordInteractiveHold({
    sharedDir: shared, botId: "team_bot_a", tripId: "trip-1",
    session: null, pluginVersion: PLUGIN_VERSION,
  });
  recordInteractiveHold({
    sharedDir: shared, botId: "team_bot_b", tripId: "trip-1",
    session: null, pluginVersion: PLUGIN_VERSION,
  });
  assert.equal(readRows(shared, "team_bot_a").length, 1);
  assert.equal(readRows(shared, "team_bot_b").length, 1);
});
