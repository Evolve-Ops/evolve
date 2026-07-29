/**
 * Tests for the Phase 3 cascade routing branch in ModelRouter.
 *
 * Spec: docs/spec-tier-cascade-2026-05-26.md § 2.6 precedence ladder.
 *
 * The cascade controller's verdict drives routing ONLY when:
 *   - `config.cascade.enabled` is true (per-bot opt-in flag), AND
 *   - a verdict has been stashed via setCascadeVerdict() for this session
 *
 * Precedence: runaway > spend_cap > user_request > user_model_override >
 *             cascade > classifier > bot_default
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.cascadeRouting.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const BASE_CFG = {
  tiers: {
    tier0: { models: ["judge/model"] },
    tier1: { models: ["power/model"] },
    tier2: { models: ["workhorse/model"] },
    tier3: { models: ["grunt/model"] },
  },
  routing: { enabled: true },
};

function _cfg(extras = {}) {
  return { ...BASE_CFG, ...extras };
}

// ── Default (cascade.enabled absent or false) ───────────────────────────────

test("cascade routing: disabled by default — verdict ignored", () => {
  const r = new ModelRouter(_cfg(), "", "");
  // Even with a verdict stashed, no enabled flag means no apply.
  r.setCascadeVerdict("s1", { tier: "tier1" });
  // Classifier classifies productive → bot default → null.
  r.setSessionType("s1", "productive");
  const model = r.resolveModelOverride("s1");
  assert.equal(model, null, "no cascade route when enabled=false");
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

test("cascade routing: explicit enabled=false equals absent", () => {
  const r = new ModelRouter(_cfg({ cascade: { enabled: false } }), "", "");
  r.setCascadeVerdict("s1", { tier: "tier1" });
  r.setSessionType("s1", "productive");
  assert.equal(r.resolveModelOverride("s1"), null);
  assert.equal(r.isCascadeEnabled(), false);
});

// ── Enabled, no verdict yet ─────────────────────────────────────────────────

test("cascade routing: enabled but no verdict yet → falls through to classifier", () => {
  // First turn of a session: the cascade controller can't have decided
  // anything (no struggle signal yet). resolveModelOverride must fall
  // through to the classifier rather than crash or guess.
  const r = new ModelRouter(_cfg({ cascade: { enabled: true } }), "", "");
  r.setSessionType("s1", "maintenance");
  const model = r.resolveModelOverride("s1");
  // Classifier routes maintenance to tier3 → "grunt/model"
  assert.equal(model, "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

// ── Enabled + verdict → cascade drives ──────────────────────────────────────

test("cascade routing: enabled + verdict tier1 → power/model + chosenBy cascade", () => {
  const r = new ModelRouter(_cfg({ cascade: { enabled: true } }), "", "");
  r.setSessionType("s1", "productive");
  r.setCascadeVerdict("s1", { tier: "tier1" });
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "cascade");
});

test("cascade routing: enabled + verdict tier2 → workhorse/model", () => {
  const r = new ModelRouter(_cfg({ cascade: { enabled: true } }), "", "");
  r.setSessionType("s1", "background");
  r.setCascadeVerdict("s1", { tier: "tier2" });
  // Cascade beats classifier's background→tier3 mapping.
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
  assert.equal(r.getLastDecisionDriver("s1"), "cascade");
});

test("cascade routing: enabled + verdict tier3 → grunt/model", () => {
  const r = new ModelRouter(_cfg({ cascade: { enabled: true } }), "", "");
  r.setSessionType("s1", "productive");
  r.setCascadeVerdict("s1", { tier: "tier3" });
  // Cascade demoted productive → tier3. Classifier wouldn't have.
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "cascade");
});

// ── Precedence ladder — cascade is BELOW safety nets and user_request ───────

test("cascade routing: user_request beats cascade verdict", () => {
  // Operator picked Power; cascade said tier3. user wins.
  const r = new ModelRouter(_cfg({ cascade: { enabled: true } }), "", "");
  r.setSessionType("s1", "productive");
  r.setCascadeVerdict("s1", { tier: "tier3" });
  r.setUserTier("s1", "power");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "user_request");
});

test("cascade routing: runaway-rate trip beats cascade verdict", () => {
  // Runaway tripped (broken loop) — cascade can't override the safety net.
  const r = new ModelRouter(_cfg({
    cascade: { enabled: true },
    runawayRateCap: { enabled: true, dollarsPerWindow: 5, windowMinutes: 1 },
  }), "", "");
  r.setSessionType("s1", "productive");
  r.setCascadeVerdict("s1", { tier: "tier1" });
  r.recordTurnCost("s1", 10.0, 1000);
  r.checkRunawayRate("s1", 1000);  // → tripped
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");  // forced tier3
  assert.equal(r.getLastDecisionDriver("s1"), "runaway");
});

// ── Verdict expires on session end ──────────────────────────────────────────

test("cascade routing: clearSession drops the verdict", () => {
  const r = new ModelRouter(_cfg({ cascade: { enabled: true } }), "", "");
  r.setSessionType("s1", "productive");
  r.setCascadeVerdict("s1", { tier: "tier1" });
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  r.clearSession("s1");
  // After clear: verdict gone, classifier wins.
  assert.equal(r.getCascadeVerdict("s1"), null);
  // sessionType is also cleared, so resolve falls through to bot default.
  assert.equal(r.resolveModelOverride("s1"), null);
});

test("cascade routing: setCascadeVerdict(null) clears the verdict", () => {
  const r = new ModelRouter(_cfg({ cascade: { enabled: true } }), "", "");
  r.setCascadeVerdict("s1", { tier: "tier1" });
  assert.equal(r.getCascadeVerdict("s1").tier, "tier1");
  r.setCascadeVerdict("s1", null);
  assert.equal(r.getCascadeVerdict("s1"), null);
});

// ── Verdict is per-session ──────────────────────────────────────────────────

test("cascade routing: verdicts don't leak across sessions", () => {
  const r = new ModelRouter(_cfg({ cascade: { enabled: true } }), "", "");
  r.setSessionType("s1", "productive");
  r.setSessionType("s2", "productive");
  r.setCascadeVerdict("s1", { tier: "tier1" });
  // s2 has no verdict — should fall through to classifier (productive
  // → bot default → null).
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.resolveModelOverride("s2"), null);
  assert.equal(r.getLastDecisionDriver("s1"), "cascade");
  assert.equal(r.getLastDecisionDriver("s2"), "classifier");
});

// ── reloadConfig preserves the cascade flag ────────────────────────────────

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

test("reloadConfig: cascade.enabled survives a reload", () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "mr-cascade-test-"));
  try {
    // Drop a tiers.json with cascade.enabled: true.
    const tiersPath = path.join(sharedDir, "tiers.json");
    fs.writeFileSync(tiersPath, JSON.stringify({
      tiers: BASE_CFG.tiers,
      routing: { enabled: true },
      cascade: { enabled: true },
    }));
    const networkPath = path.join(sharedDir, "network.json");
    fs.writeFileSync(networkPath, "{}");

    const r = new ModelRouter(_cfg(), sharedDir, "team_bot_a");
    assert.equal(r.isCascadeEnabled(), false, "constructor used the config arg, not tiers.json");

    r.reloadConfig(networkPath, tiersPath, sharedDir, "team_bot_a");
    assert.equal(r.isCascadeEnabled(), true, "reloadConfig must pick up tiers.json::cascade.enabled");

    // Now flip it off in tiers.json and reload.
    fs.writeFileSync(tiersPath, JSON.stringify({
      tiers: BASE_CFG.tiers,
      routing: { enabled: true },
      cascade: { enabled: false },
    }));
    r.reloadConfig(networkPath, tiersPath, sharedDir, "team_bot_a");
    assert.equal(r.isCascadeEnabled(), false, "rollback via flag flip works");
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});
