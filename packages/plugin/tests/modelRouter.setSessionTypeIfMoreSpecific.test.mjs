/**
 * Tests for ModelRouter.setSessionTypeIfMoreSpecific — the specificity-
 * guarded write used by the agent_end keyword-classifier path in
 * TurnObserver.processAgentEnd.
 *
 * THE BUG THIS GUARDS AGAINST (L4 P0 from the 2026-05-29 tier audit):
 *
 * Turn 1 of a heartbeat session:
 *   1. before_model_resolve fires → resolveModelRouting sets
 *      sessionType=background from the trigger anchor.
 *   2. resolveModelOverride reads sessionType=background → routes to
 *      tier3 (Haiku floor). Correct.
 *   3. Turn completes; agent_end runs the keyword classifier on the
 *      empty userMessage/assistantMessage. classifyTierByKeywords("","")
 *      returns class="ambiguous" (no information).
 *   4. Pre-fix: line 1505 unconditionally wrote sessionType=ambiguous,
 *      CLOBBERING the background anchor.
 *
 * Turn 2 of the same heartbeat session:
 *   5. resolveModelRouting checks getSessionType — it returns "ambiguous"
 *      (truthy) so the trigger-anchor branch skips ("real verdict wins").
 *   6. resolveModelOverride reads "ambiguous" → matches the
 *      `!sessionType || sessionType==="productive" || sessionType==="ambiguous"`
 *      branch → returns null → bot default (Sonnet).
 *
 * Net: every heartbeat session turn 2+ silently ran on primary instead of
 * the configured tier3 floor. Same symptom as PR #1737, recreated by the
 * post-hoc classifier overwriting the pre-classification anchor.
 *
 * setSessionTypeIfMoreSpecific encodes the specificity hierarchy:
 *   undefined < "ambiguous" < {"productive", "maintenance", "background"}
 * and refuses to downgrade a specific class to ambiguous.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.setSessionTypeIfMoreSpecific.test.mjs
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

// ── No existing classification → always write ──────────────────────────────

test("writes when no existing classification (specific new)", () => {
  const r = newRouter();
  r.setSessionTypeIfMoreSpecific("s1", "background");
  assert.equal(r.getSessionType("s1"), "background");
});

test("writes when no existing classification (ambiguous new)", () => {
  // No information beats no entry — at least the session is tracked.
  const r = newRouter();
  r.setSessionTypeIfMoreSpecific("s1", "ambiguous");
  assert.equal(r.getSessionType("s1"), "ambiguous");
});

// ── Existing ambiguous → upgrade only on specific ──────────────────────────

test("upgrades ambiguous → specific (productive)", () => {
  const r = newRouter();
  r.setSessionType("s1", "ambiguous");
  r.setSessionTypeIfMoreSpecific("s1", "productive");
  assert.equal(r.getSessionType("s1"), "productive");
});

test("upgrades ambiguous → specific (background)", () => {
  const r = newRouter();
  r.setSessionType("s1", "ambiguous");
  r.setSessionTypeIfMoreSpecific("s1", "background");
  assert.equal(r.getSessionType("s1"), "background");
});

test("ambiguous → ambiguous is a no-op (specificity unchanged)", () => {
  const r = newRouter();
  r.setSessionType("s1", "ambiguous");
  r.setSessionTypeIfMoreSpecific("s1", "ambiguous");
  assert.equal(r.getSessionType("s1"), "ambiguous");
});

// ── Existing specific → THE LOAD-BEARING REGRESSION TESTS ──────────────────

test("REGRESSION L4: specific → ambiguous is REFUSED (the bug)", () => {
  // This is the exact heartbeat-session-turn-1 scenario that produced
  // the cost bleed. The trigger anchor was 'background'; agent_end's
  // keyword classifier saw empty inputs and returned 'ambiguous'.
  const r = newRouter();
  r.setSessionType("s1", "background");
  r.setSessionTypeIfMoreSpecific("s1", "ambiguous");
  assert.equal(
    r.getSessionType("s1"),
    "background",
    "ambiguous must NOT overwrite a specific class — that's the L4 P0 bug",
  );
});

test("specific → specific allows lateral reclassification (productive → background)", () => {
  // Both specific — defer to the new verdict. This handles the case
  // where a session genuinely changes mode (rare but possible).
  const r = newRouter();
  r.setSessionType("s1", "productive");
  r.setSessionTypeIfMoreSpecific("s1", "background");
  assert.equal(r.getSessionType("s1"), "background");
});

test("specific → specific allows lateral reclassification (background → maintenance)", () => {
  const r = newRouter();
  r.setSessionType("s1", "background");
  r.setSessionTypeIfMoreSpecific("s1", "maintenance");
  assert.equal(r.getSessionType("s1"), "maintenance");
});

// ── Defensive: unknown / empty / non-string ────────────────────────────────

test("treats empty string and unknown labels as non-specific (won't overwrite specific)", () => {
  const r = newRouter();
  r.setSessionType("s1", "background");
  r.setSessionTypeIfMoreSpecific("s1", "");
  assert.equal(r.getSessionType("s1"), "background");
  r.setSessionTypeIfMoreSpecific("s1", "some-future-class");
  assert.equal(r.getSessionType("s1"), "background");
});

test("setSessionType (raw) still unconditionally overwrites — only the IfMoreSpecific variant is guarded", () => {
  // We deliberately keep the raw setter for callers that DO want to
  // overwrite (explicit reclassification paths). This test pins that
  // contract — if a future refactor adds the guard to the raw setter,
  // it breaks paths that intentionally clear/downgrade.
  const r = newRouter();
  r.setSessionType("s1", "background");
  r.setSessionType("s1", "ambiguous"); // raw — should overwrite
  assert.equal(r.getSessionType("s1"), "ambiguous");
});

// ── Multi-session isolation ────────────────────────────────────────────────

test("guard is per-session — one session's specific class doesn't protect another", () => {
  const r = newRouter();
  r.setSessionType("s1", "background");
  r.setSessionTypeIfMoreSpecific("s2", "ambiguous"); // s2 has no existing
  assert.equal(r.getSessionType("s1"), "background");
  assert.equal(r.getSessionType("s2"), "ambiguous");
});
