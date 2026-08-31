/**
 * Tests for the operator default-tier path in ModelRouter
 * (audit #69 Phase A).
 *
 * userTierOverride.defaultTier lets the operator pick the tier that
 * user-turn / unknown / ambiguous sessions land on, without having
 * to know specific model names. Maps fast/standard/power → tier3/2/1
 * via the bot's tier config.
 *
 * Precedence ladder (high → low):
 *   0. Runaway-rate (sticky session)
 *   1. Spend-cap (per-bot per-day)
 *   2. User override (chip + session_set_tier)
 *   3. Cascade verdict (Phase 3)
 *   4. Classifier-driven (background → tier3, etc.)
 *   4a. OPERATOR DEFAULT (this PR)
 *   5. Bot default (OC's primary field)
 *
 * Operator default slots BELOW user override and cascade — those win
 * when set. Slots ABOVE the bot-default fallback so the operator's
 * tier choice beats OC's primary.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.operatorDefaultTier.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const RUNGS_CFG = [
  { id: "haiku-class", models: ["grunt/model"], costClass: "low" },
  { id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" },
  { id: "opus-class", models: ["power/model"], costClass: "high" },
];
const ROLES_CFG = {
  fast: "haiku-class",
  standard: "sonnet-class",
  power: "opus-class",
};

function newRouter(defaultTier = undefined) {
  const cfg = {
    rungs: RUNGS_CFG,
    roles: ROLES_CFG,
    routing: { enabled: true },
    ...(defaultTier !== undefined ? {
      userTierOverride: { defaultTier },
    } : {}),
  };
  return new ModelRouter(cfg, "", "");
}

// ── defaultTier maps cleanly to tier names ─────────────────────────────────

test("defaultTier='fast' → tier3 (grunt model) on unset session", () => {
  const r = newRouter("fast");
  // No session class set → classifier branch fires → operator default kicks in
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

test("defaultTier='standard' → tier2 (workhorse model)", () => {
  const r = newRouter("standard");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

test("defaultTier='power' → tier1 (power model)", () => {
  const r = newRouter("power");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

// ── auto / missing / unknown → fall through to bot default ─────────────────

test("defaultTier='auto' → no override (legacy bot-default behavior)", () => {
  const r = newRouter("auto");
  assert.equal(r.resolveModelOverride("s1"), null);
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

test("defaultTier missing → no override (legacy bot-default behavior)", () => {
  const r = newRouter();  // no userTierOverride at all
  assert.equal(r.resolveModelOverride("s1"), null);
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

test("defaultTier='unknown-future-value' → no override (conservative)", () => {
  // Operator typo or future enum value we don't recognize → fall
  // through to bot default rather than fail noisily.
  const r = newRouter("turbo-mega-power");
  assert.equal(r.resolveModelOverride("s1"), null);
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

// ── Empty tier safety ──────────────────────────────────────────────────────

test("defaultTier set but tier is empty → fall through to bot default", () => {
  // tier1.models=[] but defaultTier=power → can't fulfil; fall through
  // safely rather than returning null model with operator_default driver
  // (which would lie about which tier ran).
  const cfg = {
    rungs: [
      { id: "haiku-class", models: ["grunt/model"], costClass: "low" },
      { id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" },
    ],
    roles: {
      fast: "haiku-class",
      standard: "sonnet-class",
    },
    routing: { enabled: true },
    userTierOverride: { defaultTier: "power" },
  };
  const r = new ModelRouter(cfg, "", "");
  assert.equal(r.resolveModelOverride("s1"), null);
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

// ── Case-insensitive + whitespace tolerance ────────────────────────────────

test("defaultTier='FAST' (uppercase) is accepted", () => {
  const r = newRouter("FAST");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

test("defaultTier='  standard  ' (whitespace) is accepted", () => {
  const r = newRouter("  standard  ");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
});

// ── Precedence: user override wins over operator default ───────────────────

test("user override (chip / setUserTier) WINS over operator default", () => {
  // Operator says "everyone uses tier3 by default." User asks for power.
  // User's per-session request wins.
  const r = newRouter("fast");
  r.setUserTier("s1", "power", "ui_chip");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "user_request");
});

test("user override 'auto' (clear) → falls back to operator default", () => {
  // User sets then clears → operator default kicks back in.
  const r = newRouter("standard");
  r.setUserTier("s1", "power", "ui_chip");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  r.setUserTier("s1", "auto", "ui_chip");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

// ── Classifier-driven sessions still route through their own tier ──────────

test("classifier 'background' still routes to backgroundTier, ignores operator default", () => {
  // operator says "default to power" — but a heartbeat-anchored session
  // still goes to tier3 via routing.backgroundTier. Operator default
  // only applies when the classifier would have fallen to bot default.
  const r = newRouter("power");
  r.setSessionType("s1", "background");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

test("classifier 'maintenance' still routes to maintenanceTier, ignores operator default", () => {
  const r = newRouter("power");
  r.setSessionType("s1", "maintenance");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

test("classifier 'productive' triggers operator default fallback", () => {
  // Productive sessions normally route to bot default — operator
  // default replaces that fallback when configured.
  const r = newRouter("power");
  r.setSessionType("s1", "productive");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

test("classifier 'ambiguous' triggers operator default fallback", () => {
  const r = newRouter("standard");
  r.setSessionType("s1", "ambiguous");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

// ── Routing disabled wins over operator default (safety) ──────────────────

test("routing.enabled=false short-circuits even when operator default is set", () => {
  const cfg = {
    rungs: RUNGS_CFG,
    roles: ROLES_CFG,
    routing: { enabled: false },  // explicitly disabled
    userTierOverride: { defaultTier: "power" },
  };
  const r = new ModelRouter(cfg, "", "");
  assert.equal(r.resolveModelOverride("s1"), null);
});
