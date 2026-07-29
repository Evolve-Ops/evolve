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

const BASE_CFG = {
  tiers: {
    tier0: { models: ["judge/model"] },
    tier1: { models: ["power/model"] },
    tier2: { models: ["workhorse/model"] },
    tier3: { models: ["grunt/model"] },
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
      models: { tiers: BASE_CFG.tiers, routing: { enabled: true } },
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
      models: { tiers: BASE_CFG.tiers, routing: { enabled: true } },
      runawayRateCap: { enabled: true, dollarsPerWindow: 100.0, windowMinutes: 1 },
    }));
    const tiersPath = path.join(dir, "tiers.json");
    fs.writeFileSync(tiersPath, JSON.stringify({
      tiers: BASE_CFG.tiers,
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
      models: { tiers: BASE_CFG.tiers, routing: { enabled: true } },
      runawayRateCap: { enabled: true, dollarsPerWindow: 5.0, windowMinutes: 1 },
    }));
    const tiersPath = path.join(dir, "tiers.json");
    fs.writeFileSync(tiersPath, JSON.stringify({
      tiers: BASE_CFG.tiers,
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
