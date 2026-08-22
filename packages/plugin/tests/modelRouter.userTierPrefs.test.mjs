/**
 * Tests for the per-user-per-bot tier-prefs path in ModelRouter
 * (audit #69 Phase C).
 *
 * userTierPrefs.users[<user_key>].defaultTier holds each user's
 * personal default. The plugin reads it BEFORE the operator's
 * bot-wide userTierOverride.defaultTier (Phase A) so a team member
 * can opt into Power without dragging everyone else's defaults along.
 *
 * Precedence ladder (high → low) with both phases live:
 *   0. Runaway-rate (sticky)
 *   1. Spend-cap
 *   2. User override (chip + `evo tier X`)
 *   3. Cascade verdict (Phase 3)
 *   4. Classifier (background → tier3 etc.)
 *   4a-i.  Per-user pref  (Phase C — THIS PR)
 *   4a-ii. Operator default (Phase A)
 *   5. Bot default
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.userTierPrefs.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const TIERS_CFG = {
  tier1: { models: ["power/model"] },
  tier2: { models: ["workhorse/model"] },
  tier3: { models: ["grunt/model"] },
};

function newRouter({
  operatorDefault,
  userTierPrefs,
} = {}) {
  const cfg = {
    tiers: TIERS_CFG,
    routing: { enabled: true },
    ...(operatorDefault !== undefined ? {
      userTierOverride: { defaultTier: operatorDefault },
    } : {}),
    ...(userTierPrefs !== undefined ? { userTierPrefs } : {}),
  };
  return new ModelRouter(cfg, "", "");
}

// ── Happy path: per-user pref applies when user_key is pinned ──────────────

test("per-user 'power' pref applies for the pinned user", () => {
  const r = newRouter({
    userTierPrefs: {
      users: {
        "ext:telegram:alice": { defaultTier: "power" },
      },
    },
  });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "user_default");
});

test("per-user 'fast' pref applies; tier3 model returned", () => {
  const r = newRouter({
    userTierPrefs: {
      users: { "ext:slack:bob": { defaultTier: "fast" } },
    },
  });
  r.setSessionUserKey("s1", "ext:slack:bob");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "user_default");
});

test("per-user 'standard' pref applies; tier2 model returned", () => {
  const r = newRouter({
    userTierPrefs: {
      users: { "ext:discord:carol": { defaultTier: "standard" } },
    },
  });
  r.setSessionUserKey("s1", "ext:discord:carol");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
  assert.equal(r.getLastDecisionDriver("s1"), "user_default");
});

// ── Per-user pref beats operator default ──────────────────────────────────

test("per-user pref wins over operator default", () => {
  // Operator: everyone gets Fast. Alice personally wants Power.
  const r = newRouter({
    operatorDefault: "fast",
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "power" } },
    },
  });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "user_default");
});

test("user without a pref falls through to operator default on multi-user bot", () => {
  // Operator: Fast. Only Alice has a pref (Power). Bob (no pref) → Fast.
  const r = newRouter({
    operatorDefault: "fast",
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "power" } },
    },
  });
  r.setSessionUserKey("s1", "ext:telegram:bob");  // not in prefs
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

test("user with 'auto' pref falls through to operator default", () => {
  // Alice set evo tier-default auto — clears her pref. Plugin should
  // fall through to whatever the operator set.
  const r = newRouter({
    operatorDefault: "standard",
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "auto" } },
    },
  });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

// ── Missing user identity ─────────────────────────────────────────────────

test("no user_key pinned → falls through to operator default", () => {
  // Heartbeats and other identity-less surfaces should still pick up
  // the operator default, just not the per-user one.
  const r = newRouter({
    operatorDefault: "power",
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "fast" } },
    },
  });
  // sessionKey set but no user_key pinned
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

test("user_key pinned then cleared → falls through", () => {
  const r = newRouter({
    operatorDefault: "fast",
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "power" } },
    },
  });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  assert.equal(r.resolveModelOverride("s1"), "power/model");

  // Subsequent turn binds to no user (e.g. anon flow) — should clear
  // and fall back to operator default.
  r.setSessionUserKey("s1", null);
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

// ── Precedence: user override (chip / evo tier X) beats per-user pref ─────

test("session-scoped user override beats per-user persistent pref", () => {
  // Persistent: alice wants Fast. This session she typed `evo tier power`.
  // The session override (level 2) beats the persistent pref (level 4a).
  const r = newRouter({
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "fast" } },
    },
  });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  r.setUserTier("s1", "power", "evo_keyword");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "user_request");
});

// ── Background/maintenance still owned by classifier ──────────────────────

test("classifier 'background' beats per-user pref", () => {
  // Background work always routes to tier3 via the trigger anchor —
  // no operator/user default applies. Same shape as Phase A's test.
  const r = newRouter({
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "power" } },
    },
  });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  r.setSessionType("s1", "background");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

// ── Edge cases / safety ────────────────────────────────────────────────────

test("missing tier model → fall through (same as Phase A)", () => {
  // alice's pref is Power but tier1 has no models configured. The
  // helper falls through to operator default (which here is also
  // missing) → null → bot default. No crash.
  const cfg = {
    tiers: {
      tier1: { models: [] },
      tier2: { models: ["workhorse/model"] },
      tier3: { models: ["grunt/model"] },
    },
    routing: { enabled: true },
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "power" } },
    },
  };
  const r = new ModelRouter(cfg, "", "");
  r.setSessionUserKey("s1", "ext:telegram:alice");
  assert.equal(r.resolveModelOverride("s1"), null);
  assert.equal(r.getLastDecisionDriver("s1"), "classifier");
});

test("user pref with unknown choice → fall through to operator default", () => {
  // Defense in depth: a corrupted file or future-enum entry shouldn't
  // hard-fail. Falls through to operator default, then bot default.
  const r = newRouter({
    operatorDefault: "fast",
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "turbo-mega-power" } },
    },
  });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

test("empty userTierPrefs.users → falls through to operator default", () => {
  // Bot freshly installed; no users have set personal prefs yet.
  // Plugin still routes per operator default (Phase A behavior).
  const r = newRouter({
    operatorDefault: "standard",
    userTierPrefs: { users: {} },
  });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

test("missing userTierPrefs block entirely → Phase A behavior preserved", () => {
  // Verify Phase C is fully additive — a bot with only the Phase A
  // block (no userTierPrefs) routes exactly as Phase A always did.
  const r = newRouter({ operatorDefault: "power" });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  assert.equal(r.resolveModelOverride("s1"), "power/model");
  assert.equal(r.getLastDecisionDriver("s1"), "operator_default");
});

// ── setSessionUserKey accessors ───────────────────────────────────────────

test("setSessionUserKey + getSessionUserKey round-trip", () => {
  const r = newRouter();
  assert.equal(r.getSessionUserKey("s1"), null);
  r.setSessionUserKey("s1", "ext:slack:alice");
  assert.equal(r.getSessionUserKey("s1"), "ext:slack:alice");
  r.setSessionUserKey("s1", null);
  assert.equal(r.getSessionUserKey("s1"), null);
});

test("clearSession drops the pinned user_key", () => {
  // Important so a session-recycle (e.g. OC reaps the session id and
  // OPENS a NEW one with the same key) doesn't reuse the prior user's
  // identity. Verified by binding, clearing, and confirming the prior
  // user's pref no longer applies.
  const r = newRouter({
    userTierPrefs: {
      users: { "ext:telegram:alice": { defaultTier: "power" } },
    },
  });
  r.setSessionUserKey("s1", "ext:telegram:alice");
  assert.equal(r.resolveModelOverride("s1"), "power/model");

  r.clearSession("s1");
  assert.equal(r.getSessionUserKey("s1"), null);
  // After clear, no override pinned, no operator default — bot default.
  assert.equal(r.resolveModelOverride("s1"), null);
});

test("empty user_key string treated as no binding (defense in depth)", () => {
  const r = newRouter();
  r.setSessionUserKey("s1", "");
  assert.equal(r.getSessionUserKey("s1"), null);
});

test("empty sessionKey is silently ignored", () => {
  // Should not throw. Same shape as setUserTier's empty-key guard.
  const r = newRouter();
  r.setSessionUserKey("", "ext:telegram:alice");
  // No state mutated; another session shouldn't see this key.
  assert.equal(r.getSessionUserKey("s1"), null);
});
