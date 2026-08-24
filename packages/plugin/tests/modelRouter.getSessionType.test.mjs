/**
 * Tests for ModelRouter.getSessionType — the read-side companion to
 * setSessionType. Used by the trigger-kind pre-classification anchor
 * in TurnObserver.resolveModelRouting to check whether a prior turn's
 * classifier has already labeled the session (in which case the
 * trigger anchor is skipped — real classification wins).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.getSessionType.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const CFG = {
  tiers: {
    tier1: { models: ["power/model"] },
    tier2: { models: ["workhorse/model"] },
    tier3: { models: ["grunt/model"] },
  },
  routing: { enabled: true },
};

function newRouter() {
  return new ModelRouter(CFG, "", "");
}

test("getSessionType returns undefined for unknown session", () => {
  const r = newRouter();
  assert.equal(r.getSessionType("never-classified"), undefined);
});

test("getSessionType returns the value set by setSessionType", () => {
  const r = newRouter();
  r.setSessionType("s1", "background");
  assert.equal(r.getSessionType("s1"), "background");
  r.setSessionType("s1", "productive");  // overwrite
  assert.equal(r.getSessionType("s1"), "productive");
});

test("getSessionType keeps sessions isolated by key", () => {
  const r = newRouter();
  r.setSessionType("s1", "maintenance");
  r.setSessionType("s2", "productive");
  assert.equal(r.getSessionType("s1"), "maintenance");
  assert.equal(r.getSessionType("s2"), "productive");
  assert.equal(r.getSessionType("s3"), undefined);
});

// ── End-to-end: pre-classification on the new heartbeat path ─────────────────
//
// Simulates what TurnObserver.resolveModelRouting now does:
//   1. before_model_resolve fires for a brand-new heartbeat session
//   2. classifier hasn't run yet — getSessionType returns undefined
//   3. caller anchors via setSessionType(..., "background")
//   4. resolveModelOverride sees "background" → returns tier3 model
//
// This is the chain that was broken before: step 4 returned null and
// OC fell back to agents.defaults.model.primary.

test("heartbeat anchor flow: pre-classify before resolve → tier3 model", () => {
  const r = newRouter();
  const key = "heartbeat-session-123";
  // Before anchor: no override
  assert.equal(r.getSessionType(key), undefined);
  assert.equal(r.resolveModelOverride(key), null,
    "no classification yet → null override (legacy chain break)");
  // Apply trigger anchor (what TurnObserver now does)
  r.setSessionType(key, "background");
  // After anchor: tier3 routed via routing.backgroundTier default
  assert.equal(r.resolveModelOverride(key), "grunt/model",
    "after trigger anchor, before_model_resolve routes to tier3");
});

test("subagent anchor flow: maintenance routes via maintenanceTier default", () => {
  const r = newRouter();
  const key = "subagent-session-456";
  r.setSessionType(key, "maintenance");
  assert.equal(r.resolveModelOverride(key), "grunt/model");
});

test("user_turn that later turns productive is NOT pre-anchored — classifier wins", () => {
  // Documents the precedence rule: when the caller has chosen NOT to
  // anchor (user_turn / unknown), and later setSessionType is called
  // by the agent_end classifier with "productive", that wins and
  // routing stays on bot-default (null) — matching the historical
  // pre-fix behavior for user-driven sessions.
  const r = newRouter();
  const key = "user-session-789";
  assert.equal(r.getSessionType(key), undefined);
  // Anchor skipped (simulating triggerKind=user_turn → null cls)
  // Classifier runs in agent_end, marks productive
  r.setSessionType(key, "productive");
  assert.equal(r.resolveModelOverride(key), null,
    "productive sessions still fall through to bot default (Sonnet/whatever primary is)");
});
