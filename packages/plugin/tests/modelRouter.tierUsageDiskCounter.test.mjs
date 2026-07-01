/**
 * Round-trip tests for the disk-backed per-day role counter
 * (spec-model-rungs-and-roles §595-599 "converge both paths on the
 * disk-backed counter"). These exercise the REAL filesystem write/seed
 * paths — no mocks — so they protect the contract the Python reader
 * (models.get_tier_usage_today) parses:
 *
 *   1. A transition into `max` appends a parseable JSONL record with
 *      tier="max" under {sharedDir}/cost/tier-usage/{botId}/{date}.jsonl.
 *   2. A transition into `power` appends a record with tier="tier1"
 *      (the field name the server queries for the Power cap).
 *   3. Boot-seeding reads today's JSONL back into the in-memory counters,
 *      so a "restarted" router (new instance, same sharedDir) sees the
 *      prior turns and the cap stays tripped across restart.
 *   4. A missing tier-usage file seeds count=0 without throwing.
 *
 * The Python side (test_model_rungs_roles_phase2_surfaces.py) copies the
 * record shape asserted here as a fixture so the two ends can't drift.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.tierUsageDiskCounter.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const RUNGS_CFG = {
  rungs: [
    { id: "haiku-class",  models: ["anthropic/claude-haiku-4-5"], costClass: "low" },
    { id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6"], costClass: "medium" },
    { id: "opus-class",   models: ["anthropic/claude-opus-4-8"], costClass: "high" },
    { id: "fable-class",  models: ["anthropic/claude-fable-5"], costClass: "premium" },
  ],
  roles: {
    fast: "haiku-class",
    standard: "sonnet-class",
    power: "opus-class",
    max: "fable-class",
    judge: { rung: "sonnet-class", provider: "not-standard" },
  },
  roleCaps: { power: { maxPerDayPerBot: 10 }, max: { maxPerDayPerBot: 5 } },
  routing: { enabled: true, maintenanceRole: "fast", backgroundRole: "fast", ambiguousRole: null },
  // allow bot-initiated max so resolveModelOverride actually picks max.
  userTierOverride: { enabled: true, allowBotInitiated: { power: true, max: true } },
};

function ymd(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function mkTmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "evolve-tierusage-"));
}

function usagePath(shared, botId) {
  return path.join(shared, "cost", "tier-usage", botId, `${ymd()}.jsonl`);
}

function newRouter(shared, botId, extra = {}) {
  return new ModelRouter({ ...RUNGS_CFG, ...extra }, shared, botId);
}

// ── 1. max transition writes tier="max" record ─────────────────────────────

test("transition into max appends a parseable tier='max' JSONL record", () => {
  const shared = mkTmp();
  const botId = "evo";
  const r = newRouter(shared, botId);
  r.setUserTier("s1", "max", "ui_chip");
  r.resolveModelOverride("s1");

  const lines = fs.readFileSync(usagePath(shared, botId), "utf8")
    .split("\n").filter(Boolean);
  assert.equal(lines.length, 1);
  const rec = JSON.parse(lines[0]);
  assert.equal(rec.tier, "max");                 // field the server counts
  assert.equal(rec.bot_id, botId);
  assert.equal(rec.model, "anthropic/claude-fable-5");
  assert.equal(rec.context, "plugin_session_tier");
  // ts must be a Z-suffixed second-precision ISO (matches the Python writer's
  // "%Y-%m-%dT%H:%M:%SZ" shape so cost.py callers don't choke).
  assert.match(rec.ts, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
});

// ── 2. power transition writes tier="tier1" ────────────────────────────────

test("transition into power appends tier='tier1' (server's Power-cap key)", () => {
  const shared = mkTmp();
  const botId = "evo";
  const r = newRouter(shared, botId);
  r.setUserTier("p1", "power", "ui_chip");
  r.resolveModelOverride("p1");

  const lines = fs.readFileSync(usagePath(shared, botId), "utf8")
    .split("\n").filter(Boolean);
  assert.equal(lines.length, 1);
  assert.equal(JSON.parse(lines[0]).tier, "tier1");
});

// ── 3. boot-seeding reads the file back; cap survives restart ──────────────

test("boot-seeding reads prior turns; max cap stays tripped across restart", () => {
  const shared = mkTmp();
  const botId = "evo";

  // First router: drive 5 max transitions (cap=5) → file has 5 records.
  const r1 = newRouter(shared, botId);
  for (let i = 0; i < 5; i++) {
    r1.setUserTier(`m-${i}`, "max", "ui_chip");
    r1.resolveModelOverride(`m-${i}`);
  }
  assert.equal(r1.canEscalateToRole("max").allowed, false);
  const lines = fs.readFileSync(usagePath(shared, botId), "utf8")
    .split("\n").filter(Boolean);
  assert.equal(lines.length, 5);

  // Simulate a plugin restart: brand-new router, same sharedDir/botId.
  // Without seeding, _maxCallsToday would be 0 and the cap would let a
  // 6th max turn through. With seeding it must read 5 and stay tripped.
  const r2 = newRouter(shared, botId);
  const gate = r2.canEscalateToRole("max");
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
  assert.match(gate.detail, /5\/5/);
});

test("boot-seeding reads power records back into the power counter", () => {
  const shared = mkTmp();
  const botId = "evo";
  const r1 = newRouter(shared, botId, { roleCaps: { power: { maxPerDayPerBot: 3 }, max: { maxPerDayPerBot: 5 } } });
  for (let i = 0; i < 3; i++) {
    r1.setUserTier(`p-${i}`, "power", "ui_chip");
    r1.resolveModelOverride(`p-${i}`);
  }
  const r2 = newRouter(shared, botId, { roleCaps: { power: { maxPerDayPerBot: 3 }, max: { maxPerDayPerBot: 5 } } });
  const gate = r2.canEscalateToRole("power");
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
});

// ── 4. missing file seeds 0, no throw ──────────────────────────────────────

test("missing tier-usage file seeds count=0 without throwing", () => {
  const shared = mkTmp();   // empty — no cost/ dir at all
  const botId = "evo";
  const r = newRouter(shared, botId);
  // Fresh day, empty disk → both caps open.
  assert.equal(r.canEscalateToRole("max").allowed, true);
  assert.equal(r.canEscalateToRole("power").allowed, true);
});

// ── empty sharedDir is a no-op (back-compat with router used w/o shared) ────

test("empty sharedDir disables disk counter without error", () => {
  const r = new ModelRouter({ ...RUNGS_CFG }, "", "");
  r.setUserTier("s", "max", "ui_chip");
  // Must not throw despite no sharedDir to write to.
  assert.doesNotThrow(() => r.resolveModelOverride("s"));
});
