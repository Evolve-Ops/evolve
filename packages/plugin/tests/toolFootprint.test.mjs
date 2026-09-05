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
 *   4. (CE-2a) the v2 record weighs the same tools under every tool profile,
 *      and every profile's kept + trimmed accounts for the whole tool set.
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

// ── CE-2a: per-profile weights (schema v2) ───────────────────────────────────
test("flush records what each tool profile weighs and what it trims", () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-fp-"));
  const mk = (name) => () => ({
    name,
    description: "D".repeat(500),
    parameters: { type: "object", properties: {} },
  });
  const fp = new ToolFootprint({ sharedDir, botId: "bot-x", tier: "full", logger: quiet });
  const api = { registerTool: () => {} };
  fp.wrap(api);
  api.registerTool(mk("defer"));        // kept by no_live_speaker
  api.registerTool(mk("roster_block")); // trimmed by no_live_speaker
  fp.flush();

  const rec = JSON.parse(
    fs.readFileSync(path.join(sharedDir, "bot-x", "turns", "context-footprint.json"), "utf8"));
  assert.equal(rec.schema_version, 2);
  const full = rec.profiles.full;
  const bg = rec.profiles.no_live_speaker;
  const dispatch = rec.profiles.evolve_dispatch;
  // Every profile accounts for every tool: kept + trimmed == the full set.
  // The FULL-WEIGHT partition is over kept_chars, not total_chars — total_chars
  // carries the stubs on top, so it does not partition.
  for (const p of [full, bg, dispatch]) {
    assert.equal(p.tools.length + p.trimmed.length, rec.tool_count);
    assert.equal(p.kept_chars + p.trimmed_chars, rec.total_chars);
    assert.equal(p.total_chars, p.kept_chars + p.stub_chars);
    assert.equal(p.saved_chars, p.trimmed_chars - p.stub_chars);
  }
  assert.equal(full.trimmed_chars, 0, "the full profile trims nothing");
  assert.equal(full.stub_chars, 0, "a profile that trims nothing has no stubs");
  assert.equal(full.total_chars, rec.total_chars);
  assert.deepEqual(bg.tools.map((t) => t.name), ["defer"]);
  assert.deepEqual(bg.trimmed.map((t) => t.name), ["roster_block"]);
  // A trimmed tool is REGISTERED, as a stub. It is cheaper than the real
  // definition and it is not free — an accounting that reports it as free is
  // the defect this asserts against.
  assert.ok(bg.stub_chars > 0, "a trimmed tool still costs its stub");
  assert.ok(bg.stub_chars < bg.trimmed_chars, "a stub must be cheaper than the definition");
  assert.equal(bg.total_chars, bg.kept_chars + bg.stub_chars);
  // evolve_dispatch keeps NO tool, but it still registers all of them as stubs,
  // so what it puts on the wire is > 0. The profile's `why` says so too.
  assert.equal(dispatch.kept_chars, 0, "the evolve dispatch profile keeps nothing");
  assert.equal(dispatch.trimmed_chars, rec.total_chars);
  assert.ok(dispatch.total_chars > 0, "evolve_dispatch still registers every tool as a stub");
  assert.equal(dispatch.total_chars, dispatch.stub_chars);
  assert.ok(dispatch.saved_chars < dispatch.trimmed_chars, "trimming is not removal");
  // The kind -> profile map ships with the measurement so a reader needs no
  // second source to say which sessions pay which column.
  assert.equal(rec.kind_profiles.scheduled, "no_live_speaker");
  assert.equal(rec.kind_profiles.user, undefined);
  fs.rmSync(sharedDir, { recursive: true, force: true });
});
