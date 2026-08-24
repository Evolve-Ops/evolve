/**
 * Tests for CascadeController's pressure-flag gates.
 *
 * Spec: internal/spec-tier-cascade-2026-05-26.md § pressure watchdog.
 *
 * The pressure watchdog daemon writes pod-wide flags every 60s. The
 * controller consults them at decision time to:
 *   - suppress tier1 ask-hint emission when pod is at tier1 cap
 *   - suppress autonomous tier2→tier1 background escalation
 *   - hold current tier during an escalation storm
 *   - apply ALL of the above when watchdog is dead (operating blind)
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/cascadeController.pressureGates.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CascadeController,
  DEFAULT_CASCADE_CONFIG,
} from "../dist/observer/CascadeController.js";

function fakeLogger() {
  return {
    debug: () => {},
    info: () => {},
    warn: () => {},
    error: () => {},
  };
}

// Run N persistent-struggle user-facing turns, return whether ANY
// turn fired an ask-hint. Necessary because ask-hint emits once then
// enters cooldown; checking only the last turn misses the emission.
function _runStruggleSession(pressureFlags, turnCount = 5) {
  const ctrl = new CascadeController(DEFAULT_CASCADE_CONFIG, fakeLogger());
  let anyAskHint = null;
  let lastDecision;
  for (let i = 0; i < turnCount; i++) {
    lastDecision = ctrl.decide({
      sessionKey: "s1",
      triggerKind: "user_turn",
      turnIndex: i,
      // Sustained high struggle drives the ask-hint emission path.
      struggle: { score: 0.85, features: {}, raw: {}, payload_drift: null },
      triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
      pressureFlags,
    });
    if (lastDecision.askHint && !anyAskHint) {
      anyAskHint = lastDecision.askHint;
    }
  }
  return { ...lastDecision, askHint: anyAskHint ?? undefined };
}

// ── Ask-hint suppression ────────────────────────────────────────────────────


test("user-facing: ask-hint emitted when no pressure (baseline)", () => {
  // Persistent-struggle sessions should normally fire ask-hint.
  // Pin the baseline so the next tests prove the gate REMOVES it.
  const d = _runStruggleSession(undefined);
  assert.ok(d.askHint, "baseline must emit ask-hint when struggle is sustained");
  assert.equal(d.askHint.kind, "consider_tier1_escalation");
});


test("user-facing: ask-hint suppressed when pod_tier1_concurrency_cap fired", () => {
  // Pod is at tier1 cap → controller must NOT ask the user "want
  // Power?" because Power is unreachable right now. Asking and then
  // refusing the upgrade is worse UX than not asking.
  const d = _runStruggleSession({
    pod_tier1_concurrency_cap: true,
  });
  assert.equal(d.askHint, undefined, "ask-hint must be suppressed at tier1 cap");
});


test("user-facing: ask-hint suppressed when tier1_pod_spend_burst fired", () => {
  // Spend-burst is the same shape of constraint — pod-wide tier1
  // resources are scarce; don't tease the user with Power they can't get.
  const d = _runStruggleSession({
    tier1_pod_spend_burst: true,
  });
  assert.equal(d.askHint, undefined);
});


test("user-facing: ask-hint suppressed when watchdog is dead", () => {
  // Operating blind → conservative defaults. The controller doesn't
  // know whether pressure is present, so behave as if it is.
  const d = _runStruggleSession({
    _watchdog_dead: true,
  });
  assert.equal(d.askHint, undefined);
});

// ── Hold-tier on escalation_storm ───────────────────────────────────────────


test("user-facing: escalation_storm holds current tier even with high struggle", () => {
  // Persistent struggle would normally drive demote/promote behavior
  // depending on tier. With escalation_storm, ALL tier transitions
  // suspend until pressure clears.
  const ctrl = new CascadeController(DEFAULT_CASCADE_CONFIG, fakeLogger());
  // Turn 0 starts at tier2 user-facing default.
  ctrl.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0,
    struggle: { score: 0.0, features: {}, raw: {}, payload_drift: null },
    triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
  });
  // Turn 1 with pressure: should hold at tier2 regardless of struggle.
  const d = ctrl.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    struggle: { score: 0.95, features: {}, raw: {}, payload_drift: null },
    triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
    pressureFlags: { escalation_storm: true },
  });
  assert.equal(d.tier, "tier2", "tier must hold during escalation_storm");
  assert.equal(d.escalation_event, "held");
  assert.equal(d.askHint, undefined, "ask-hint also suppressed during storm");
});


test("background: escalation_storm blocks tier3→tier2 escalation", () => {
  // tier3 → tier2 normally fires on high struggle in background.
  // Pressure-hold blocks that too.
  const ctrl = new CascadeController(DEFAULT_CASCADE_CONFIG, fakeLogger());
  ctrl.decide({
    sessionKey: "bg1", triggerKind: "heartbeat", turnIndex: 0,
    struggle: { score: 0.0, features: {}, raw: {}, payload_drift: null },
    triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
  });
  const d = ctrl.decide({
    sessionKey: "bg1", triggerKind: "heartbeat", turnIndex: 1,
    struggle: { score: 0.9, features: {}, raw: {}, payload_drift: null },
    triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
    pressureFlags: { escalation_storm: true },
  });
  assert.equal(d.tier, "tier3", "tier3 must hold during escalation_storm");
  assert.equal(d.escalation_event, "held");
});

// ── Tier2 → tier1 background escalation gate ────────────────────────────────


test("background: tier2→tier1 escalation suppressed at tier1 concurrency cap", () => {
  // Setup: bot with tier1_enabled in background; persistent high
  // struggle would normally drive tier2→tier1. Pressure cap blocks it.
  const cfg = JSON.parse(JSON.stringify(DEFAULT_CASCADE_CONFIG));
  cfg.background.tier1_enabled = true;
  const ctrl = new CascadeController(cfg, fakeLogger());

  // Climb to tier2 first (turn 0 lands on tier3 default; turn 1 escalates).
  for (let i = 0; i < 2; i++) {
    ctrl.decide({
      sessionKey: "bg1", triggerKind: "heartbeat", turnIndex: i,
      struggle: { score: 0.85, features: {}, raw: {}, payload_drift: null },
      triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
    });
  }
  // Now at tier2 with persistent struggle. Pressure cap → no tier1.
  const d = ctrl.decide({
    sessionKey: "bg1", triggerKind: "heartbeat", turnIndex: 2,
    struggle: { score: 0.85, features: {}, raw: {}, payload_drift: null },
    triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
    pressureFlags: { pod_tier1_concurrency_cap: true },
  });
  assert.notEqual(d.tier, "tier1", "must not escalate to tier1 under cap");
});


test("background: tier2→tier1 escalation works when pressure clears", () => {
  // Inverse of above — confirms the gate doesn't suppress legitimate
  // escalation in the no-pressure case (would be a false negative).
  const cfg = JSON.parse(JSON.stringify(DEFAULT_CASCADE_CONFIG));
  cfg.background.tier1_enabled = true;
  const ctrl = new CascadeController(cfg, fakeLogger());
  for (let i = 0; i < 2; i++) {
    ctrl.decide({
      sessionKey: "bg1", triggerKind: "heartbeat", turnIndex: i,
      struggle: { score: 0.85, features: {}, raw: {}, payload_drift: null },
      triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
    });
  }
  // No pressureFlags → behaves normally.
  const d = ctrl.decide({
    sessionKey: "bg1", triggerKind: "heartbeat", turnIndex: 2,
    struggle: { score: 0.85, features: {}, raw: {}, payload_drift: null },
    triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
  });
  assert.equal(d.tier, "tier1", "no pressure → tier1 escalation succeeds");
  assert.equal(d.escalation_event, "escalated");
});

// ── Back-compat: undefined pressureFlags = no pressure ──────────────────────


test("absent pressureFlags = pre-pressure-aware behavior", () => {
  // The controller worked without pressureFlags before this PR. Ensure
  // back-compat: omitted pressureFlags must behave exactly the same as
  // pressureFlags={} (no flags fired).
  const ctrlA = new CascadeController(DEFAULT_CASCADE_CONFIG, fakeLogger());
  const ctrlB = new CascadeController(DEFAULT_CASCADE_CONFIG, fakeLogger());

  for (let i = 0; i < 4; i++) {
    const input = {
      sessionKey: "s1", triggerKind: "user_turn", turnIndex: i,
      struggle: { score: 0.85, features: {}, raw: {}, payload_drift: null },
      triviality: { score: 0.0, features: {}, raw: {}, payload_drift: null },
    };
    const a = ctrlA.decide({ ...input });
    const b = ctrlB.decide({ ...input, pressureFlags: {} });
    assert.equal(a.tier, b.tier, `turn ${i}: tier mismatch`);
    assert.equal(a.escalation_event, b.escalation_event, `turn ${i}: event mismatch`);
    assert.equal(
      Boolean(a.askHint),
      Boolean(b.askHint),
      `turn ${i}: askHint mismatch`,
    );
  }
});

// ── User-tier override pre-empts pressure gates ─────────────────────────────


test("user-tier override still wins over pressure gates", () => {
  // Operator picks Power → user_requested tier1 → controller honors
  // immediately, even under pressure. Pressure-gate is for AUTONOMOUS
  // escalations; operator choices pre-empt cascade entirely (spec
  // § 2.6 precedence: spend_cap → user_request → cascade → classifier).
  const ctrl = new CascadeController(DEFAULT_CASCADE_CONFIG, fakeLogger());
  const d = ctrl.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    userRequestedTier: "tier1",
    consentSource: "ui_chip",
    pressureFlags: { pod_tier1_concurrency_cap: true },
  });
  assert.equal(d.tier, "tier1", "user_requested tier wins over pressure");
});


// ── _shouldBlockTier1 / _shouldHoldTier helpers ─────────────────────────────


test("helper: _shouldBlockTier1 false on no/empty flags", async () => {
  const { _shouldBlockTier1 } = await import("../dist/observer/CascadeController.js");
  assert.equal(_shouldBlockTier1(undefined), false);
  assert.equal(_shouldBlockTier1({}), false);
  assert.equal(_shouldBlockTier1({ escalation_storm: true }), false,
    "escalation_storm alone doesn't block tier1");
});


test("helper: _shouldBlockTier1 true when any tier1-pressure flag set", async () => {
  const { _shouldBlockTier1 } = await import("../dist/observer/CascadeController.js");
  assert.equal(_shouldBlockTier1({ pod_tier1_concurrency_cap: true }), true);
  assert.equal(_shouldBlockTier1({ tier1_pod_spend_burst: true }), true);
  assert.equal(_shouldBlockTier1({ _watchdog_dead: true }), true);
});


test("helper: _shouldHoldTier triggers on escalation_storm OR watchdog_dead", async () => {
  const { _shouldHoldTier } = await import("../dist/observer/CascadeController.js");
  assert.equal(_shouldHoldTier(undefined), false);
  assert.equal(_shouldHoldTier({}), false);
  assert.equal(_shouldHoldTier({ escalation_storm: true }), true);
  assert.equal(_shouldHoldTier({ _watchdog_dead: true }), true);
  // tier1-cap alone does NOT trigger hold — only blocks tier1.
  assert.equal(_shouldHoldTier({ pod_tier1_concurrency_cap: true }), false);
});
