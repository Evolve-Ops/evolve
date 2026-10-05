/**
 * Tests for ModelRouter.reloadConfig — regression guards.
 *
 * Code review HIGH #3: reloadConfig used to rebuild this.config from
 * scratch with only tiers/routing/accountTiers/accountRouting, silently
 * dropping any `runawayRateCap` previously set via constructor or a
 * prior reload. The fix layers runaway-rate from
 *   tiersFile.runawayRateCap → network.runawayRateCap → this.config.runawayRateCap
 * so the cap survives reloads even when neither file carries the block.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.reloadConfig.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "mr-reload-test-"));
}

// Isolate HOME: reloadConfig without an explicit tiersJsonPath consults
// ~/.openclaw/evolve-tiers.json first (loadTiersFile #1), so a stray file in
// the developer's real home leaks into these tests — and a legacy-shaped one
// now poisons the router (LegacyTierShapeError refuse semantics) instead of
// being silently synthesized. Point HOME at an empty sandbox for the whole
// file (each test file runs in its own process).
process.env.HOME = tmpDir();

const BASE_CFG = {
  rungs: [
    { id: "haiku-class", models: ["grunt/model"], costClass: "low" },
    { id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" },
    { id: "opus-class", models: ["power/model"], costClass: "high" },
    { id: "judge-class", models: ["judge/model"], costClass: "medium" },
  ],
  roles: {
    fast: "haiku-class",
    standard: "sonnet-class",
    power: "opus-class",
    judge: { rung: "judge-class", provider: "not-standard" },
  },
  routing: { enabled: true },
  runawayRateCap: {
    enabled: true,
    dollarsPerWindow: 5.0,
    windowMinutes: 1,
    criticalTripsPer24h: 3,
  },
};

test("reloadConfig preserves runawayRateCap when neither file carries it", () => {
  const dir = tmpDir();
  try {
    // Network.json without runawayRateCap; tiers.json absent.
    const networkPath = path.join(dir, "network.json");
    fs.writeFileSync(networkPath, JSON.stringify({
      models: { rungs: BASE_CFG.rungs, roles: BASE_CFG.roles, routing: { enabled: true } },
    }));

    // Constructor receives the cap.
    const r = new ModelRouter(BASE_CFG, dir, "team_bot_a");

    // After reload (no tiers.json, network.json has no runawayRateCap),
    // the cap should still be in effect — verified end-to-end by the
    // trip behavior, not by reading private config.
    r.reloadConfig(networkPath);

    r.recordTurnCost("s1", 6.0, 1000); // > $5/1min threshold
    const result = r.checkRunawayRate("s1", 1000);
    assert.equal(result.tripped, true, "cap was dropped on reload");
    assert.equal(result.totalUsd, 6.0);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("reloadConfig: tiers.json runawayRateCap takes precedence", () => {
  const dir = tmpDir();
  try {
    const networkPath = path.join(dir, "network.json");
    fs.writeFileSync(networkPath, JSON.stringify({
      models: { rungs: BASE_CFG.rungs, roles: BASE_CFG.roles, routing: { enabled: true } },
      runawayRateCap: { enabled: true, dollarsPerWindow: 100.0, windowMinutes: 1 },
    }));
    const tiersPath = path.join(dir, "tiers.json");
    fs.writeFileSync(tiersPath, JSON.stringify({
      rungs: BASE_CFG.rungs,
      roles: BASE_CFG.roles,
      routing: { enabled: true },
      runawayRateCap: { enabled: true, dollarsPerWindow: 5.0, windowMinutes: 1 },
    }));

    const r = new ModelRouter({ ...BASE_CFG, runawayRateCap: undefined }, dir, "team_bot_a");
    r.reloadConfig(networkPath, tiersPath);

    // tiers.json said $5 → $6 trips.
    r.recordTurnCost("s1", 6.0, 1000);
    assert.equal(r.checkRunawayRate("s1", 1000).tripped, true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("reloadConfig: network.json runawayRateCap used when tiers.json silent", () => {
  const dir = tmpDir();
  try {
    const networkPath = path.join(dir, "network.json");
    fs.writeFileSync(networkPath, JSON.stringify({
      models: { rungs: BASE_CFG.rungs, roles: BASE_CFG.roles, routing: { enabled: true } },
      runawayRateCap: { enabled: true, dollarsPerWindow: 5.0, windowMinutes: 1 },
    }));
    const tiersPath = path.join(dir, "tiers.json");
    fs.writeFileSync(tiersPath, JSON.stringify({
      rungs: BASE_CFG.rungs,
      roles: BASE_CFG.roles,
      routing: { enabled: true },
      // no runawayRateCap → fall through to network.json
    }));

    const r = new ModelRouter({ ...BASE_CFG, runawayRateCap: undefined }, dir, "team_bot_a");
    r.reloadConfig(networkPath, tiersPath);

    r.recordTurnCost("s1", 6.0, 1000);
    assert.equal(r.checkRunawayRate("s1", 1000).tripped, true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
