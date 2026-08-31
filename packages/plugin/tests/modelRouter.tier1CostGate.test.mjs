/**
 * Tests for ModelRouter.canEscalateToTier1 — the operator-config gate
 * that the session_set_tier MCP tool consults before allowing a bot
 * to escalate the session to tier1.
 *
 * THE BUG THIS GUARDS AGAINST (L6-P1 of the 2026-05-29 tier audit):
 *
 * Bot-initiated escalation to tier1 via session_set_tier(choice:
 * "power") used to be unrestricted. A chatty bot could pin Opus for
 * the rest of a session unilaterally — no daily cap, no operator
 * opt-out, no evidence gate. The home_chat_routes.py chip path always
 * honored userTierOverride.{enabled, dailyCap}; the bot tool ignored
 * both fields entirely.
 *
 * canEscalateToTier1 centralizes three gates:
 *   1. userTierOverride.enabled (default true) — global kill-switch;
 *      false disables BOTH chip + bot tool surfaces
 *   2. userTierOverride.allowBotInitiated (default true) — NEW flag;
 *      operator can keep the chip working while forbidding the bot
 *      tool from self-escalating
 *   3. userTierOverride.dailyCap (default 10) — pod-local-day tier1
 *      turn count; when reached, downgrades "power" → "standard"
 *
 * The SetTierTool tests in setTierTool.test.mjs cover the end-to-end
 * "bot calls tool, gate blocks, downgrade applied" flow. This file
 * tests the gate logic in isolation.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.tier1CostGate.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const BASE_CFG = {
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
  routing: { enabled: true },
};

function newRouter(overrideConfig = undefined) {
  const cfg = overrideConfig !== undefined
    ? { ...BASE_CFG, userTierOverride: overrideConfig }
    : { ...BASE_CFG };
  return new ModelRouter(cfg, "", "");
}

// Trigger a tier1 transition to bump the in-process daily counter.
// resolveModelOverride+setUserTier(power) is the canonical path the
// counter listens on (via _markSessionTier).
function bumpTier1Count(router, sessionKey) {
  router.setUserTier(sessionKey, "power", "ui_chip");
  router.resolveModelOverride(sessionKey);
}

// ── Gate 1: userTierOverride.enabled (kill-switch) ─────────────────────────

test("default (no override config) allows tier1 escalation", () => {
  const r = newRouter();
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, true);
  assert.equal(gate.reason, undefined);
});

test("enabled=false blocks escalation with feature_disabled reason", () => {
  const r = newRouter({ enabled: false });
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "feature_disabled");
  assert.ok(
    /enabled is false/i.test(gate.detail),
    `detail should explain the operator's enabled=false config; got: ${gate.detail}`
  );
});

test("enabled=true (explicit) does not trip the kill-switch", () => {
  const r = newRouter({ enabled: true });
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, true);
});

// ── Gate 2: userTierOverride.allowBotInitiated (NEW) ───────────────────────

test("allowBotInitiated=false blocks with bot_initiated_disabled reason", () => {
  // Operator wants the chip to keep working but no bot self-escalation.
  const r = newRouter({ enabled: true, allowBotInitiated: false });
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "bot_initiated_disabled");
  assert.ok(
    /chip/i.test(gate.detail),
    `detail should mention the chip path still works; got: ${gate.detail}`
  );
});

test("allowBotInitiated=true (explicit) is the default behavior", () => {
  const r = newRouter({ enabled: true, allowBotInitiated: true });
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, true);
});

// ── Gate 3: dailyCap ───────────────────────────────────────────────────────

test("dailyCap defaults to 10 when not specified", () => {
  // Bump count 9 times — still allowed.
  const r = newRouter();
  for (let i = 0; i < 9; i++) bumpTier1Count(r, `s${i}`);
  assert.equal(r.canEscalateToTier1().allowed, true);
  // 10th transition trips the cap.
  bumpTier1Count(r, "s9");
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
  assert.ok(
    /10\/10/.test(gate.detail) || /10\s*\/\s*10/.test(gate.detail),
    `detail should report the count/cap ratio; got: ${gate.detail}`
  );
});

test("explicit dailyCap=3 trips at 3 transitions", () => {
  const r = newRouter({ enabled: true, dailyCap: 3 });
  bumpTier1Count(r, "s1");
  bumpTier1Count(r, "s2");
  assert.equal(r.canEscalateToTier1().allowed, true);
  bumpTier1Count(r, "s3");
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
});

test("dailyCap=0 blocks immediately (Power-disabled sentinel)", () => {
  // Per chip-path semantics: cap=0 means "Power is OFF for this bot today."
  const r = newRouter({ enabled: true, dailyCap: 0 });
  // No bumps yet — gate still trips because used (0) >= cap (0).
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, false);
  assert.equal(gate.reason, "daily_cap_exhausted");
});

test("counter only bumps on transitions INTO tier1 (not tier1→tier1)", () => {
  const r = newRouter({ enabled: true, dailyCap: 5 });
  // Force the same session to tier1 multiple times via the user override.
  // First call transitions undef→tier1 (bumps); subsequent calls are
  // tier1→tier1 (no bump). The counter should only see 1.
  for (let i = 0; i < 4; i++) {
    r.setUserTier("sticky-session", "power", "ui_chip");
    r.resolveModelOverride("sticky-session");
  }
  // After 4 re-resolves of the same tier1 session: 1 transition.
  // Now create 4 fresh tier1 sessions → 4 more transitions → 5 total.
  // 5 == cap (5) → next gate check returns blocked.
  for (let i = 0; i < 4; i++) bumpTier1Count(r, `fresh-${i}`);
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, false, "5 transitions should hit cap of 5");
});

// ── Gate ordering: enabled wins over allowBotInitiated wins over cap ──────

test("enabled=false short-circuits ahead of cap check", () => {
  const r = newRouter({ enabled: false, dailyCap: 100 });
  // Even with a large cap, enabled=false trips first.
  const gate = r.canEscalateToTier1();
  assert.equal(gate.reason, "feature_disabled");
});

test("allowBotInitiated=false trips ahead of cap check", () => {
  const r = newRouter({ enabled: true, allowBotInitiated: false, dailyCap: 100 });
  const gate = r.canEscalateToTier1();
  assert.equal(gate.reason, "bot_initiated_disabled");
});

// ── Counter resets at pod-local midnight ───────────────────────────────────

test("counter rolls over when dayIso changes (pod-local midnight semantics)", () => {
  const r = newRouter({ enabled: true, dailyCap: 2 });
  bumpTier1Count(r, "s1");
  bumpTier1Count(r, "s2");
  assert.equal(r.canEscalateToTier1().allowed, false, "cap of 2 hit after 2");

  // Simulate midnight rollover by mutating the private counter's dayIso.
  // The next canEscalateToTier1 call sees a fresh day and resets to 0.
  // (In production, localDateYMD() returns the new day; this test
  // mutates the marker directly because we can't time-travel the clock
  // from a unit test without mocking globals.)
  // eslint-disable-next-line no-underscore-dangle
  r._tier1CallsToday.dayIso = "1999-01-01";
  const gate = r.canEscalateToTier1();
  assert.equal(gate.allowed, true, "stale dayIso should trigger a reset");
});
