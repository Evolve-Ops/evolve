/**
 * Gateway-side daily-cap clamp (#3566 audit E-4).
 *
 * The enforcement point that actually gates Power/Max spend is
 * ModelRouter.canEscalateToRole (gate 3, via _roleCap) — and the file
 * its numbers come from (~/.openclaw/evolve-tiers.json) is bot-owned by
 * construction. Before this clamp, `typeof cap === "number"` was the
 * whole check: 1e9, -1, NaN and Infinity were all honoured verbatim at
 * the routing decision, while the admin reader
 * (home_chat_routes._read_user_tier_override) clamped 0–100 — the cap
 * was enforced only where nothing enforces.
 *
 * These tests prove the clamp applies AT the routing decision, through
 * the same counter-bump path production uses (setUserTier +
 * resolveModelOverride), for both the legacy `userTierOverride.dailyCap`
 * and the new-shape `roleCaps.<role>.maxPerDayPerBot`:
 *
 *   - under-cap / in-range values are honoured verbatim (untouched);
 *   - out-of-range / non-finite values read as the role default
 *     (power 10, max 5) — fallback-to-default, NOT boundary-clamp,
 *     mirroring the admin reader's contract;
 *   - 0 stays valid ("role disabled" sentinel).
 *
 * Single-resolver rule (#3498): sanitizeDailyCap is the ONE gateway-side
 * cap resolver; its unit contract is pinned here too.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.dailyCapClamp.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter, sanitizeDailyCap } from "../dist/observer/ModelRouter.js";

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
  },
  routing: { enabled: true },
};

function newRouter(extra = {}) {
  return new ModelRouter({ ...RUNGS_CFG, ...extra }, "", "");
}

/** Burn `n` turns of `role` through the canonical resolve path. */
function useRole(r, role, n, prefix = "sess") {
  for (let i = 0; i < n; i++) {
    r.setUserTier(`${prefix}-${role}-${i}`, role, "ui_chip");
    r.resolveModelOverride(`${prefix}-${role}-${i}`);
  }
}
const usePower = (r, n, prefix = "sess") => useRole(r, "power", n, prefix);

// ── sanitizeDailyCap unit contract (the ONE resolver) ─────────────────────

test("sanitizeDailyCap: in-range values pass through, truncated to int", () => {
  assert.equal(sanitizeDailyCap(0, 10), 0);      // "role disabled" sentinel
  assert.equal(sanitizeDailyCap(7, 10), 7);
  assert.equal(sanitizeDailyCap(100, 10), 100);
  assert.equal(sanitizeDailyCap(7.9, 10), 7);    // int(cap) truncation
});

test("sanitizeDailyCap: out-of-range / non-finite / non-number → fallback", () => {
  for (const bad of [-1, 101, 1e9, NaN, Infinity, -Infinity, "20", true, false, null, undefined, {}, []]) {
    assert.equal(sanitizeDailyCap(bad, 10), 10, `expected fallback for ${String(bad)}`);
    assert.equal(sanitizeDailyCap(bad, 5), 5, `expected fallback for ${String(bad)}`);
  }
});

// ── Routing decision — new-shape roleCaps.power.maxPerDayPerBot ───────────

test("under-cap: an in-range roleCaps.power cap is honoured verbatim", () => {
  const r = newRouter({ roleCaps: { power: { maxPerDayPerBot: 3 } } });
  usePower(r, 2);
  assert.equal(r.canEscalateToRole("power").allowed, true);
  usePower(r, 1, "more");
  const gate = r.canEscalateToRole("power");
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
  assert.match(gate.detail, /3\/3/);
});

test("over-cap: roleCaps.power.maxPerDayPerBot=1e9 enforces the default 10, not 1e9", () => {
  const r = newRouter({ roleCaps: { power: { maxPerDayPerBot: 1e9 } } });
  usePower(r, 9);
  assert.equal(r.canEscalateToRole("power").allowed, true);
  usePower(r, 1, "tenth");
  const gate = r.canEscalateToRole("power");
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
  assert.match(gate.detail, /10\/10/);
});

test("NaN cap cannot disable the gate (used >= NaN is never true unclamped)", () => {
  const r = newRouter({ roleCaps: { power: { maxPerDayPerBot: NaN } } });
  usePower(r, 10);
  const gate = r.canEscalateToRole("power");
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
});

test("negative cap cannot silently kill Power from turn one — reads as default", () => {
  const r = newRouter({ roleCaps: { power: { maxPerDayPerBot: -1 } } });
  // Unclamped, used(0) >= -1 would trip immediately.
  assert.equal(r.canEscalateToRole("power").allowed, true);
});

test("cap 0 stays valid: the documented 'role disabled' sentinel is untouched", () => {
  const r = newRouter({ roleCaps: { power: { maxPerDayPerBot: 0 } } });
  const gate = r.canEscalateToRole("power");
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
  assert.match(gate.detail, /0\/0/);
});

// ── Routing decision — legacy userTierOverride.dailyCap leg ───────────────
// A roleCaps block WITHOUT a power entry keeps _normalizeConfig from
// folding the legacy value away, so _roleCap's legacy leg is what runs.

test("legacy dailyCap: in-range value honoured verbatim at the gate", () => {
  const r = newRouter({
    roleCaps: { max: { maxPerDayPerBot: 5 } },
    userTierOverride: { enabled: true, dailyCap: 2 },
  });
  usePower(r, 1);
  assert.equal(r.canEscalateToRole("power").allowed, true);
  usePower(r, 1, "second");
  const gate = r.canEscalateToRole("power");
  assert.equal(gate.allowed, false);
  assert.match(gate.detail, /2\/2/);
});

test("legacy dailyCap=1e9 enforces the default 10 at the gate", () => {
  const r = newRouter({
    roleCaps: { max: { maxPerDayPerBot: 5 } },
    userTierOverride: { enabled: true, dailyCap: 1e9 },
  });
  usePower(r, 10);
  const gate = r.canEscalateToRole("power");
  assert.equal(gate.allowed, false);
  assert.match(gate.detail, /10\/10/);
});

test("legacy dailyCap=-1 reads as default — Power not silently dead", () => {
  const r = newRouter({
    roleCaps: { max: { maxPerDayPerBot: 5 } },
    userTierOverride: { enabled: true, dailyCap: -1 },
  });
  assert.equal(r.canEscalateToRole("power").allowed, true);
});

test("legacy-only config: _normalizeConfig folds a sanitized cap, gate agrees", () => {
  // No roleCaps at all — the constructor folds legacy dailyCap into
  // roleCaps.power. An invalid value must fold as the default, and the
  // gate must enforce that same number.
  const r = newRouter({ userTierOverride: { enabled: true, dailyCap: 1e9 } });
  usePower(r, 9);
  assert.equal(r.canEscalateToRole("power").allowed, true);
  usePower(r, 1, "tenth");
  assert.equal(r.canEscalateToRole("power").allowed, false);
});

// ── Routing decision — max role ───────────────────────────────────────────

test("max: roleCaps.max.maxPerDayPerBot=1e9 enforces the default 5", () => {
  const r = newRouter({
    roleCaps: { max: { maxPerDayPerBot: 1e9 } },
    userTierOverride: { enabled: true, allowBotInitiated: { power: true, max: true } },
  });
  useRole(r, "max", 5);
  const gate = r.canEscalateToRole("max");
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
  assert.match(gate.detail, /5\/5/);
});
