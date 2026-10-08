/**
 * D-DT4/D-DT2/D-DT3 — the pod-carried default conversation role.
 *
 * internal/decision-default-tier-power-2026-09-23.md. Three things, tested
 * together because they compose on one turn:
 *
 *   D-DT4 — a bot whose own userTierOverride.defaultTier is "auto"/absent
 *   falls through to the POD default (models.defaultRole, code default
 *   "power"), source-tagged "pod_default". An explicit per-bot default
 *   still wins.
 *
 *   D-DT2 — a TRUSTED default (the pod default, or an explicit per-bot
 *   choice EQUAL to it — see ModelRouter.ts _isTrustedDefault and
 *   _capDefaultRole's docstring for why any other per-bot/per-user default
 *   stays counted on this tree, per tier_prefs_acl.py's own "FILE
 *   GRANULARITY IS NOT NARROW" finding) is never degraded by the per-role daily counter: the
 *   11th session of the day still gets Power. An untrusted (per-bot)
 *   default keeps today's counted behavior.
 *
 *   D-DT3 — for a conversation, the resolved default is a FLOOR: a cascade
 *   verdict below it is overruled ("cascade_floor"), at/above it is
 *   honored ("cascade"). Scheduled/maintenance sessions are unaffected.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.podDefaultRole.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ModelRouter,
  sanitizeDefaultRole,
  _resetPodDefaultRoleWarnForTest,
} from "../dist/observer/ModelRouter.js";

const HAIKU = "anthropic/claude-haiku-4-5";
const SONNET = "anthropic/claude-sonnet-4-6";
const OPUS = "anthropic/claude-opus-4-8";

const RUNGS = [
  { id: "haiku-class", models: [HAIKU], costClass: "low" },
  { id: "sonnet-class", models: [SONNET], costClass: "medium" },
  { id: "opus-class", models: [OPUS], costClass: "high" },
];
const ROLES = { fast: "haiku-class", standard: "sonnet-class", power: "opus-class" };

function newRouter(extra = {}) {
  return new ModelRouter({
    rungs: RUNGS,
    roles: ROLES,
    routing: { enabled: true, maintenanceRole: "fast", backgroundRole: "fast", ambiguousRole: null },
    ...extra,
  }, "", "");
}

// ── D-DT4: auto/missing falls through to the pod default ───────────────────

test("a bot with no userTierOverride resolves conversations to the pod default", () => {
  const r = newRouter({ defaultRole: "power" });
  assert.equal(r.resolveModelOverride("s0"), OPUS);
  assert.equal(r.getLastDecisionDriver("s0"), "pod_default");
});

test("userTierOverride.defaultRole: 'auto' also falls through to the pod default", () => {
  const r = newRouter({ defaultRole: "power", userTierOverride: { defaultRole: "auto" } });
  assert.equal(r.resolveModelOverride("s0"), OPUS);
  assert.equal(r.getLastDecisionDriver("s0"), "pod_default");
});

test("an explicit per-bot default beats the pod default", () => {
  const r = newRouter({ defaultRole: "power", userTierOverride: { defaultRole: "standard" } });
  assert.equal(r.resolveModelOverride("s0"), SONNET);
  assert.equal(r.getLastDecisionDriver("s0"), "operator_default");
});

test("no pod default and no per-bot default falls all the way through to bot default", () => {
  const r = newRouter();
  assert.equal(r.resolveModelOverride("s0"), null);
  assert.equal(r.getLastDecisionDriver("s0"), "classifier");
});

test("a pod default whose rung has no models abstains (unusable → fall through)", () => {
  const r = newRouter({
    defaultRole: "power",
    rungs: [{ id: "opus-class", models: [], costClass: "high" }],
    roles: { power: "opus-class" },
  });
  assert.equal(r.resolveModelOverride("s0"), null);
  assert.equal(r.getLastDecisionDriver("s0"), "classifier");
});

// ── D-DT2: the trust boundary on the per-role daily counter ────────────────

test("a TRUSTED (pod) default is never degraded — the 11th session still gets Power", () => {
  const r = newRouter({ defaultRole: "power", roleCaps: { power: { maxPerDayPerBot: 2 } } });
  const models = [];
  for (let i = 0; i < 11; i++) models.push(r.resolveModelOverride(`s${i}`));
  assert.ok(models.every((m) => m === OPUS), "every session, including past the cap, resolves to Power");
  assert.equal(r.getLastDecisionDriver("s10"), "pod_default");
  assert.equal(r.getLastCapDegradedFrom("s10"), null);
});

test("an UNTRUSTED (per-bot) default keeps today's counted behavior", () => {
  // Same cap, but the default comes from the per-bot (bot-writable) layer —
  // still degrades past the cap, exactly as before D-DT4.
  const r = newRouter({
    userTierOverride: { defaultRole: "power" },
    roleCaps: { power: { maxPerDayPerBot: 2 } },
  });
  const models = [];
  for (let i = 0; i < 3; i++) models.push(r.resolveModelOverride(`s${i}`));
  assert.deepEqual(models, [OPUS, OPUS, SONNET]);
  assert.equal(r.getLastDecisionDriver("s2"), "role_cap");
  assert.equal(r.getLastCapDegradedFrom("s2"), "power");
});

test("an explicit per-bot 'power' EQUAL to a 'power' pod default is trusted — the 11th session still gets Power", () => {
  // D-DT2 as corrected 2026-09-24: a bot the operator set to Power
  // explicitly must not behave worse than one left on the pod default.
  const r = newRouter({
    defaultRole: "power",
    userTierOverride: { defaultRole: "power" },
    roleCaps: { power: { maxPerDayPerBot: 2 } },
  });
  const models = [];
  for (let i = 0; i < 11; i++) models.push(r.resolveModelOverride(`s${i}`));
  assert.ok(models.every((m) => m === OPUS), "every session, including past the cap, resolves to Power");
  assert.equal(r.getLastDecisionDriver("s10"), "operator_default");
  assert.equal(r.getLastCapDegradedFrom("s10"), null);
});

test("an explicit per-bot 'power' ABOVE a 'standard' pod default is still counted", () => {
  const r = newRouter({
    defaultRole: "standard",
    userTierOverride: { defaultRole: "power" },
    roleCaps: { power: { maxPerDayPerBot: 2 } },
  });
  const models = [];
  for (let i = 0; i < 11; i++) models.push(r.resolveModelOverride(`s${i}`));
  assert.deepEqual(models.slice(0, 3), [OPUS, OPUS, SONNET]);
  assert.equal(models[10], SONNET);
  assert.equal(r.getLastDecisionDriver("s10"), "role_cap");
  assert.equal(r.getLastCapDegradedFrom("s10"), "power");
});

// ── D-DT4: an unknown pod value warns once instead of reading silently ────

test("sanitizeDefaultRole warns once, with the raw value, on an unknown pod defaultRole", () => {
  _resetPodDefaultRoleWarnForTest();
  const warnings = [];
  const orig = console.warn;
  console.warn = (msg) => warnings.push(String(msg));
  try {
    assert.equal(sanitizeDefaultRole("standard"), "standard");
    assert.equal(sanitizeDefaultRole(undefined), "power"); // missing: no warning
    assert.equal(warnings.length, 0);
    assert.equal(sanitizeDefaultRole("max"), "power");
    assert.equal(sanitizeDefaultRole("turbo"), "power");
  } finally {
    console.warn = orig;
  }
  assert.equal(warnings.length, 1, "warned exactly once across two unknown values");
  assert.match(warnings[0], /"max"/);
  assert.match(warnings[0], /models\.defaultRole/);
});

// ── D-DT3: the default is a floor for conversations ─────────────────────────

function cascadeRouter(extra = {}) {
  return newRouter({
    defaultRole: "power",
    cascade: { enabled: true },
    ...extra,
  });
}

test("cascade_floor: a verdict BELOW the default is overruled up to the default", () => {
  const r = cascadeRouter();
  r.setCascadeVerdict("s0", { tier: "tier3" }); // fast
  assert.equal(r.resolveModelOverride("s0"), OPUS);
  assert.equal(r.getLastDecisionDriver("s0"), "cascade_floor");
});

test("cascade: a verdict AT the default is honored, tagged 'cascade'", () => {
  const r = cascadeRouter();
  r.setCascadeVerdict("s0", { tier: "tier1" }); // power — same as the default
  assert.equal(r.resolveModelOverride("s0"), OPUS);
  assert.equal(r.getLastDecisionDriver("s0"), "cascade");
});

test("cascade: a verdict ABOVE the default is honored, tagged 'cascade'", () => {
  const r = cascadeRouter({ defaultRole: "standard" });
  r.setCascadeVerdict("s0", { tier: "tier1" }); // power — above the "standard" default
  assert.equal(r.resolveModelOverride("s0"), OPUS);
  assert.equal(r.getLastDecisionDriver("s0"), "cascade");
});

test("cascade_floor respects the trust boundary — an untrusted default's cap still binds the floor", () => {
  // The per-bot default is "power" but untrusted (bot-writable layer) and
  // its cap is already exhausted, so the floor itself resolves to
  // "standard" (capped) — the cascade verdict "fast" is still overruled UP
  // to that capped floor, never past it.
  const r = newRouter({
    userTierOverride: { defaultRole: "power" },
    roleCaps: { power: { maxPerDayPerBot: 0 } },
    cascade: { enabled: true },
  });
  r.setCascadeVerdict("s0", { tier: "tier3" }); // fast
  assert.equal(r.resolveModelOverride("s0"), SONNET);
  assert.equal(r.getLastDecisionDriver("s0"), "cascade_floor");
});

test("scheduled/maintenance sessions are unaffected by the floor", () => {
  // Pod default is "power", cascade verdict is "fast" — for a CONVERSATION
  // this would floor up to Power (see the cascade_floor test above). A
  // background session is not "who is waiting: a person", so the floor
  // must not apply: the verdict is simply honored, tagged "cascade" (not
  // "cascade_floor"), and the session stays on fast.
  const r = cascadeRouter();
  r.setSessionType("s0", "background");
  r.setCascadeVerdict("s0", { tier: "tier3" }); // fast
  assert.equal(r.resolveModelOverride("s0"), HAIKU);
  assert.equal(r.getLastDecisionDriver("s0"), "cascade");
});
