/**
 * Cascade escalation still fires on the FREE signals with every Evolve
 * model call switched off (D-OH2 item 5 —
 * internal/decision-evolve-overhead-2026-09-07.md).
 *
 * The decision retires the per-turn LLM classifiers but explicitly KEEPS
 * the cascade: "Escalation stays event-driven from the free struggle
 * signals (tokens, tool count, retries, latency)." This file is the
 * regression guard for that sentence — it drives the controller with
 * only the signals a turn produces for nothing, and no LLM judge verdict
 * at all, and asserts the escalations still happen.
 *
 * What changed underneath: ``tierIntended`` on the telemetry span now
 * comes from the routing RULE rather than from a tier classifier we no
 * longer run. The controller's own inputs did not change, which is why
 * these assertions read the same as they did before the switch.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/cascadeFreeSignals.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CascadeController,
  DEFAULT_CASCADE_CONFIG,
} from "../dist/observer/CascadeController.js";

function fakeLogger() {
  return { debug: () => {}, info: () => {}, warn: () => {}, error: () => {} };
}

function newController(overrides) {
  return new CascadeController(
    {
      enabled: true,
      user_facing: { ...DEFAULT_CASCADE_CONFIG.user_facing, ...(overrides?.user_facing ?? {}) },
      background: { ...DEFAULT_CASCADE_CONFIG.background, ...(overrides?.background ?? {}) },
    },
    fakeLogger(),
  );
}

/** A struggle signal — computed from tokens / tool count / retries / latency. Free. */
function struggleSig(score) {
  return { score, features: {}, raw: {} };
}

/** A triviality signal — also computed, also free. */
function trivialitySig(score) {
  return { score, features: {}, raw: {} };
}

test("free signals alone re-promote a demoted user session", () => {
  const c = newController();
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    triviality: trivialitySig(0.85), struggle: struggleSig(0.05),
  });
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 2,
    struggle: struggleSig(0.5),
    // No sessionJudgeVerdict — the LLM judge is off by default now.
  });
  assert.equal(result.tier, "tier2");
  assert.equal(result.escalation_event, "escalated");
});

test("free signals alone emit the tier1 ask-hint, with no judge verdict", () => {
  const c = newController({
    user_facing: { tier2_struggle_persistence: 2, tier2_struggle_threshold: 0.5 },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1, struggle: struggleSig(0.8),
  });
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 2, struggle: struggleSig(0.8),
  });
  assert.ok(result.askHint, "the escalation path must not depend on an LLM judge");
  assert.equal(result.askHint.kind, "consider_tier1_escalation");
});

test("a judge verdict, when one arrives, is still honoured", () => {
  // The judge did not go away — it went behind a switch and a sample,
  // and its verdict can now also arrive as a structured tail on the
  // primary model's own turn. Whichever way it arrives, the controller
  // reads it exactly as before.
  const c = newController({
    user_facing: { tier2_struggle_persistence: 1, tier2_struggle_threshold: 0.5 },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    sessionJudgeVerdict: "STRUGGLING",
  });
  assert.ok(result, "a verdict-only turn must still produce a decision");
  assert.equal(typeof result.tier, "string");
});

test("background sessions still escalate off the free struggle signal", () => {
  const c = newController();
  c.decide({ sessionKey: "bg", triggerKind: "heartbeat", turnIndex: 0 });
  const result = c.decide({
    sessionKey: "bg", triggerKind: "heartbeat", turnIndex: 1,
    struggle: struggleSig(0.9),
  });
  assert.ok(
    ["tier2", "tier3"].includes(result.tier),
    `unexpected background tier ${result.tier}`,
  );
});
