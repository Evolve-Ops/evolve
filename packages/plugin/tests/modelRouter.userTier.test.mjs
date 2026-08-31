/**
 * Tests for ModelRouter's user-tier override path.
 *
 * Covers the precedence stack defined in
 * internal/spec-user-tier-control-2026-05-26.md "Architecture →
 * ModelRouter changes":
 *
 *   1. Spend-cap safety net (already covered indirectly elsewhere; not
 *      re-asserted here — would require a real shared-dir file).
 *   2. Operator user-tier override   ← THIS FILE
 *   3. Classifier-driven routing
 *   4. Bot default
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.userTier.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

// Minimal config: each tier resolves to a single distinct model string
// so we can assert exactly which tier was selected by reading the
// returned model name.
const CFG = {
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
};

function newRouter() {
  // sharedDir + botId left empty → spend-cap check no-ops (fail open),
  // which is what we want for unit-testing the precedence stack.
  return new ModelRouter(CFG, "", "");
}

// ── User tier choice beats classifier ────────────────────────────────────────

test("userTier=power overrides productive classification → tier1", () => {
  const r = newRouter();
  r.setSessionType("s1", "productive");
  r.setUserTier("s1", "power");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
});

test("userTier=fast overrides productive → tier3", () => {
  const r = newRouter();
  r.setSessionType("s1", "productive");
  r.setUserTier("s1", "fast");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

test("userTier=standard overrides maintenance → tier2 (not the maintenance default)", () => {
  // Critical invariant: when user picks Standard, we must return tier2
  // EXPLICITLY rather than null. Otherwise on a bot whose default is
  // tier3 (forge etc.), maintenance + user-picks-Standard would fall
  // through to bot-default-tier3 and the user's pick would be a
  // silent no-op.
  const r = newRouter();
  r.setSessionType("s1", "maintenance");
  r.setUserTier("s1", "standard");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
});

test("userTier=power overrides maintenance → tier1", () => {
  const r = newRouter();
  r.setSessionType("s1", "maintenance");
  r.setUserTier("s1", "power");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
});

// ── Auto / unknown clears the override ───────────────────────────────────────

test("userTier='auto' clears the override → falls back to classifier", () => {
  const r = newRouter();
  r.setSessionType("s1", "productive");
  r.setUserTier("s1", "power");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  r.setUserTier("s1", "auto");
  // Productive → null (let OC use bot default)
  assert.equal(r.resolveModelOverride("s1"), null);
});

test("userTier=null clears the override (env var unset case)", () => {
  const r = newRouter();
  r.setSessionType("s1", "maintenance");
  r.setUserTier("s1", "power");
  r.setUserTier("s1", null);
  // Maintenance falls back to tier3 per classifier-driven routing
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

test("userTier=undefined (typeof env var) clears", () => {
  const r = newRouter();
  r.setSessionType("s1", "maintenance");
  r.setUserTier("s1", "power");
  r.setUserTier("s1", undefined);
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

test("userTier=garbage clears (unknown values must not pin a tier)", () => {
  const r = newRouter();
  r.setSessionType("s1", "productive");
  r.setUserTier("s1", "power");
  r.setUserTier("s1", "potato");
  assert.equal(r.resolveModelOverride("s1"), null);
});

// ── Case + whitespace tolerance ──────────────────────────────────────────────

test("userTier accepts mixed-case (POWER, Standard, fAsT)", () => {
  const r = newRouter();
  r.setSessionType("s1", "productive");
  r.setUserTier("s1", "POWER");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  r.setUserTier("s1", "  Standard ");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
  r.setUserTier("s1", "fAsT");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

// ── No interference between sessions ─────────────────────────────────────────

test("userTier is scoped per session — distinct sessions don't bleed", () => {
  const r = newRouter();
  r.setSessionType("s1", "productive");
  r.setSessionType("s2", "productive");
  r.setUserTier("s1", "power");
  // s2 has no user tier → classifier-driven (null for productive)
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.resolveModelOverride("s2"), null);
});

test("clearSession removes both classifier AND userTier state", () => {
  const r = newRouter();
  r.setSessionType("s1", "productive");
  r.setUserTier("s1", "power");
  r.clearSession("s1");
  assert.equal(r.resolveModelOverride("s1"), null);
  // Diagnostics should show clean state too
  const status = r.getRoutingStatus();
  assert.equal(status.sessions["s1"], undefined);
  assert.equal(status.userTiers["s1"], undefined);
});

// ── Behavior when target tier is misconfigured ───────────────────────────────

test("userTier=power on a config without a power role → returns null (fail open)", () => {
  const cfg = {
    rungs: [
      { id: "haiku-class", models: ["haiku"], costClass: "low" },
      { id: "sonnet-class", models: ["sonnet"], costClass: "medium" },
    ],
    roles: { fast: "haiku-class", standard: "sonnet-class" },
    routing: { enabled: true },
  };
  const r = new ModelRouter(cfg, "", "");
  r.setSessionType("s1", "productive");
  r.setUserTier("s1", "power");
  assert.equal(r.resolveModelOverride("s1"), null);
});

// ── Routing disabled in config ───────────────────────────────────────────────

test("routing.enabled=false beats userTier (returns null)", () => {
  const cfg = {
    rungs: [
      { id: "haiku-class", models: ["grunt/model"], costClass: "low" },
      { id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" },
      { id: "opus-class", models: ["power/model"], costClass: "high" },
    ],
    roles: {
      fast: "haiku-class",
      standard: "sonnet-class",
      power: "opus-class",
    },
    routing: { enabled: false },
  };
  const r = new ModelRouter(cfg, "", "");
  r.setSessionType("s1", "productive");
  r.setUserTier("s1", "power");
  assert.equal(r.resolveModelOverride("s1"), null);
});

// ── Diagnostics surface includes userTier map ────────────────────────────────

test("getRoutingStatus exposes the userTiers map", () => {
  const r = newRouter();
  r.setSessionType("s1", "productive");
  r.setUserTier("s1", "power");
  r.setUserTier("s2", "fast");
  const status = r.getRoutingStatus();
  assert.equal(status.userTiers["s1"], "power");
  assert.equal(status.userTiers["s2"], "fast");
});
