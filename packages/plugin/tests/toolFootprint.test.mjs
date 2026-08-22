/**
 * Tests for ToolFootprint — per-bot boot-time tool-schema weights (B2/A1).
 *
 * Pins:
 *   1. wrap() records every registerTool factory and passes through to the
 *      original (return value + arguments intact).
 *   2. measureFactory sizes {name, description, parameters} and reports a
 *      throwing factory as chars: -1 instead of raising.
 *   3. flush() writes atomic JSON with totals; a throwing factory does not
 *      poison the file; unwritable dir never throws.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/toolFootprint.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  ToolFootprint,
  measureFactory,
  TOOL_FOOTPRINT_SCHEMA_VERSION,
} from "../dist/observer/ToolFootprint.js";

const quiet = { warn: () => {}, info: () => {} };

const okFactory = () => ({
  name: "sample_tool",
  description: "Does a sample thing.",
  parameters: { type: "object", properties: { q: { type: "string" } } },
  execute: async () => "x",
});

function badFactory() { throw new Error("needs live deps"); }

test("measureFactory sizes the prompt-riding surface", () => {
  const row = measureFactory(okFactory);
  assert.equal(row.name, "sample_tool");
  assert.ok(row.chars > 50);
});

test("measureFactory reports a throwing factory as -1", () => {
  const row = measureFactory(badFactory);
  assert.equal(row.chars, -1);
  assert.equal(row.name, "badFactory");
});

test("wrap records factories and passes through", () => {
  const calls = [];
  const api = { registerTool: (f, extra) => { calls.push([f, extra]); return "reg-ok"; } };
  const fp = new ToolFootprint({ sharedDir: "/tmp", botId: "b", tier: "full", logger: quiet });
  fp.wrap(api);
  const out = api.registerTool(okFactory, "extra-arg");
  assert.equal(out, "reg-ok");
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1], "extra-arg");
});

test("flush writes totals and tolerates a bad factory", () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-fp-"));
  const api = { registerTool: () => {} };
  const fp = new ToolFootprint({ sharedDir, botId: "bot-x", tier: "manage", logger: quiet });
  fp.wrap(api);
  api.registerTool(okFactory);
  api.registerTool(badFactory);
  fp.flush();

  const rec = JSON.parse(
    fs.readFileSync(path.join(sharedDir, "bot-x", "turns", "context-footprint.json"), "utf8"));
  assert.equal(rec.schema_version, TOOL_FOOTPRINT_SCHEMA_VERSION);
  assert.equal(rec.tier, "manage");
  assert.equal(rec.tool_count, 2);
  const byName = Object.fromEntries(rec.tools.map((t) => [t.name, t.chars]));
  assert.ok(byName.sample_tool > 50);
  assert.equal(byName.badFactory, -1);
  // Failed factory contributes 0, not -1, to the total.
  assert.equal(rec.total_chars, byName.sample_tool);
  fs.rmSync(sharedDir, { recursive: true, force: true });
});

test("flush on unwritable dir never throws", () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-fp-"));
  fs.writeFileSync(path.join(sharedDir, "bot-x"), "file blocks mkdir");
  const fp = new ToolFootprint({ sharedDir, botId: "bot-x", tier: "full", logger: quiet });
  assert.doesNotThrow(() => fp.flush());
  fs.rmSync(sharedDir, { recursive: true, force: true });
});

test("a stale foreign tmp does not block the flush (poison-pill regression)", () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-fp-"));
  const turnsDir = path.join(sharedDir, "bot-x", "turns");
  fs.mkdirSync(turnsDir, { recursive: true });
  // Legacy fixed-name orphan (as left by a pre-fix cross-user CLI load).
  fs.writeFileSync(path.join(turnsDir, "context-footprint.json.tmp"), "stale");
  const fp = new ToolFootprint({ sharedDir, botId: "bot-x", tier: "full", logger: quiet });
  fp.wrap({ registerTool: () => {} });
  fp.flush();
  const rec = JSON.parse(
    fs.readFileSync(path.join(turnsDir, "context-footprint.json"), "utf8"));
  assert.equal(rec.tool_count, 0);
  // The orphan was swept and no new pid-tmp remains.
  const leftovers = fs.readdirSync(turnsDir).filter((f) => f.endsWith(".tmp"));
  assert.deepEqual(leftovers, []);
  fs.rmSync(sharedDir, { recursive: true, force: true });
});
