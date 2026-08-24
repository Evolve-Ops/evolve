/**
 * Tests for the `max` role semantics (spec-model-rungs-and-roles-2026-06-09,
 * §max semantics — pull-only). These cover the Phase-1 invariants that the
 * legacy tier1-only cost-gate tests don't reach:
 *
 *   1. `max` resolves to the fable-class rung's model.
 *   2. `max` is bot-initiated-blocked by DEFAULT (allowBotInitiated.max
 *      defaults false) even when `power` is allowed.
 *   3. Per-role daily cap for `max` defaults to 5 (vs power's 10).
 *   4. The cap-degradation chain is max → power → standard → standard.
 *   5. The cascade can NEVER produce `max` (it has no cascade-tier mapping).
 *   6. `max` is excluded from classifier/operator-default routing
 *      (validated against {fast, standard, power}).
 *   7. `judge` prefers a provider-diverse model but falls back to the
 *      same-vendor model (still routes) when the rung has no provider !=
 *      standard's — diversity is a preference, not a hard constraint.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.maxRoleSemantics.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const RUNGS_CFG = {
  rungs: [
    { id: "haiku-class",  models: ["anthropic/claude-haiku-4-5"], costClass: "low" },
    { id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6", "openai/gpt-4o"], costClass: "medium" },
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
};

function newRouter(extra = {}) {
  return new ModelRouter({ ...RUNGS_CFG, ...extra }, "", "");
}

// ── Resolution ────────────────────────────────────────────────────────────

test("max resolves to the fable-class rung model", () => {
  const r = newRouter();
  assert.equal(r.resolveRoleToModel("max"), "anthropic/claude-fable-5");
  assert.equal(r.resolveRoleToModel("power"), "anthropic/claude-opus-4-8");
});

// ── §max semantics #4: bot_initiated + max blocked by default ──────────────

test("max is bot-initiated-blocked by default (power is not)", () => {
  const r = newRouter();
  const maxGate = r.canEscalateToRole("max");
  assert.equal(maxGate.allowed, false);
  assert.equal(maxGate.reason, "bot_initiated_disabled");
  // power has no per-role bot-initiated restriction by default.
  assert.equal(r.canEscalateToRole("power").allowed, true);
});

test("operator can opt in to bot-initiated max via allowBotInitiated.max", () => {
  const r = newRouter({
    userTierOverride: { enabled: true, allowBotInitiated: { power: true, max: true } },
  });
  assert.equal(r.canEscalateToRole("max").allowed, true);
});

// ── §max semantics #6: per-role caps + degradation chain ───────────────────

test("degradeRoleOnCap walks max → power → standard → standard", () => {
  const r = newRouter();
  assert.equal(r.degradeRoleOnCap("max"), "power");
  assert.equal(r.degradeRoleOnCap("power"), "standard");
  assert.equal(r.degradeRoleOnCap("standard"), "standard");
  assert.equal(r.degradeRoleOnCap("fast"), "standard");
});

test("max daily cap defaults to 5 and trips after 5 transitions into max", () => {
  // allow bot-initiated max so the gate falls through to the cap check.
  const r = newRouter({
    userTierOverride: { enabled: true, allowBotInitiated: { power: true, max: true } },
  });
  // Bump the per-day max counter through the canonical resolve path.
  for (let i = 0; i < 5; i++) {
    r.setUserTier(`sess-${i}`, "max", "ui_chip");
    r.resolveModelOverride(`sess-${i}`);
  }
  const gate = r.canEscalateToRole("max");
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
  assert.match(gate.detail, /5\/5/);
});

// ── §max semantics #2: cascade can never produce max ───────────────────────

test("a max-pinned session reads as no cascade-visible request", () => {
  // getUserTier() projects the user's role choice into the cascade
  // controller's tier vocabulary. `max` has no cascade tier (pull-only),
  // so a max-pinned session must read as null — the cascade never sees it.
  const r = newRouter();
  r.setUserTier("sess-max", "max", "ui_chip");
  assert.equal(r.getUserTier("sess-max"), null);
  // sanity: a power-pinned session DOES project to tier1.
  r.setUserTier("sess-pow", "power", "ui_chip");
  assert.equal(r.getUserTier("sess-pow"), "tier1");
});

// ── §max semantics #3: max excluded from classifier/operator defaults ──────

test("maintenanceRole=max is not honored as a classifier default", () => {
  // A misconfigured max classifier default must fall back to fast, not
  // route background turns to Fable.
  const r = newRouter({ routing: { ...RUNGS_CFG.routing, maintenanceRole: "max" } });
  // A maintenance-classified session resolves to fast's model, never fable.
  r.setSessionType("sess-m", "maintenance");
  const model = r.resolveModelOverride("sess-m");
  assert.notEqual(model, "anthropic/claude-fable-5");
});

// ── judge provider-diversity ───────────────────────────────────────────────

test("judge picks a provider-diverse model (not standard's provider)", () => {
  const r = newRouter();
  // standard's provider is anthropic; judge must skip to openai/gpt-4o.
  assert.equal(r.resolveRoleToModel("judge"), "openai/gpt-4o");
});

test("judge falls back to the same-vendor model when no provider-diverse option exists", () => {
  // Diversity is a PREFERENCE (2026-06-19): with only standard's vendor in the
  // rung, judge STILL ROUTES on that vendor rather than failing to null.
  const r = new ModelRouter(
    {
      rungs: [{ id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6"], costClass: "medium" }],
      roles: {
        standard: "sonnet-class",
        judge: { rung: "sonnet-class", provider: "not-standard" },
      },
      routing: { enabled: true },
    },
    "",
    "",
  );
  assert.equal(r.resolveRoleToModel("judge"), "anthropic/claude-sonnet-4-6");
});
