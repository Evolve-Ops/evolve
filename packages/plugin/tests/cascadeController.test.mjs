/**
 * Tests for CascadeController — per-session decision engine (Phase 2).
 *
 * Covers spec § 2.2 decision branches for user-facing and background
 * sources, including:
 *   - Default tier on turn 0
 *   - User-facing demote-on-triviality (positive evidence required)
 *   - User-facing tier3 → tier2 re-promote on struggle
 *   - User-facing tier1 ask-hint emission with cooldown
 *   - User-facing tier1 → tier2 de-escalation (consent_source dependent)
 *   - Background tier3 → tier2 escalation
 *   - Background tier2 → tier3 de-escalation with hysteresis
 *   - User-requested tier always wins
 *   - Spend-cap forces tier3
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/cascadeController.test.mjs
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

function newController(cfgOverrides) {
  // Deep clone + apply overrides shallow on user_facing / background.
  const cfg = {
    enabled: true,
    user_facing: { ...DEFAULT_CASCADE_CONFIG.user_facing, ...(cfgOverrides?.user_facing ?? {}) },
    background: { ...DEFAULT_CASCADE_CONFIG.background, ...(cfgOverrides?.background ?? {}) },
  };
  return new CascadeController(cfg, fakeLogger());
}

function struggleSig(score) {
  return { score, features: {}, raw: {}, payload_drift: null };
}

function trivialitySig(score) {
  return { score, features: {}, raw: {}, payload_drift: null };
}

// ── User-facing branch ──────────────────────────────────────────────────────

test("user_turn turn 0 → default tier2", () => {
  const c = newController();
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0,
  });
  assert.equal(result.tier, "tier2");
  assert.equal(result.escalation_event, "held");
});

test("user_turn demote tier2 → tier3 on strong triviality + low struggle", () => {
  const c = newController();
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    triviality: trivialitySig(0.85), struggle: struggleSig(0.05),
  });
  assert.equal(result.tier, "tier3");
  assert.equal(result.escalation_event, "deescalated");
});

test("user_turn does NOT demote on absence-of-struggle alone (no triviality evidence)", () => {
  const c = newController();
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    struggle: struggleSig(0.0),
    // triviality omitted → 0 → won't pass demote_threshold (0.7)
  });
  assert.equal(result.tier, "tier2");
  assert.equal(result.escalation_event, "held");
});

test("user_turn re-promotes tier3 → tier2 on struggle", () => {
  const c = newController();
  // Turn 0 → tier2. Turn 1: demote to tier3.
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    triviality: trivialitySig(0.85), struggle: struggleSig(0.05),
  });
  // Turn 2: struggle returns, should re-promote.
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 2,
    struggle: struggleSig(0.5),
  });
  assert.equal(result.tier, "tier2");
  assert.equal(result.escalation_event, "escalated");
});

// ── User-facing tier1 ask-hint flow ─────────────────────────────────────────

test("user_turn emits ask-hint after sustained tier2 struggle", () => {
  const c = newController({
    user_facing: {
      tier2_struggle_persistence: 2,
      tier2_struggle_threshold: 0.5,
    },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  // Turn 1: struggle high. persistence_turns = 1.
  c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    struggle: struggleSig(0.8),
  });
  // Turn 2: struggle high. persistence_turns = 2 → ask-hint fires.
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 2,
    struggle: struggleSig(0.8),
  });
  assert.equal(result.tier, "tier2");  // tier doesn't change — ask-hint only
  assert.ok(result.askHint, "expected ask-hint");
  assert.equal(result.askHint.kind, "consider_tier1_escalation");
  assert.equal(result.askHint.turns_struggling, 2);
});

test("user_turn ask-hint suppressed when tier1_ask_enabled=false", () => {
  const c = newController({
    user_facing: {
      tier1_ask_enabled: false,
      tier2_struggle_persistence: 1,
      tier2_struggle_threshold: 0.5,
    },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    struggle: struggleSig(0.9),
  });
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 2,
    struggle: struggleSig(0.9),
  });
  assert.equal(result.askHint, undefined);
});

test("user_turn ask-hint cooldown after first emission", () => {
  const c = newController({
    user_facing: {
      tier2_struggle_persistence: 1,
      tier2_struggle_threshold: 0.5,
      tier1_ask_cooldown_turns: 10,
    },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  // Turn 1 emits ask-hint.
  const first = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    struggle: struggleSig(0.9),
  });
  assert.ok(first.askHint);
  // Turn 2-9: cooldown active, no ask-hint even with continued struggle.
  for (let t = 2; t < 11; t++) {
    const r = c.decide({
      sessionKey: "s1", triggerKind: "user_turn", turnIndex: t,
      struggle: struggleSig(0.9),
    });
    if (t < 11) {
      assert.equal(r.askHint, undefined, `turn ${t} should be in cooldown`);
    }
  }
  // Turn 11+: cooldown expired, can fire again.
  const after = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 12,
    struggle: struggleSig(0.9),
  });
  assert.ok(after.askHint);
});

// ── User-facing tier1 de-escalation ────────────────────────────────────────

test("user_turn tier1 → tier2 de-escalation requires consent_source=ask_hint_agreed", () => {
  const c = newController({
    user_facing: {
      tier1_destabilize_threshold: 0.3,
      tier1_destabilize_turns: 3,
    },
  });
  // Simulate user-chip Power → ui_chip
  for (let t = 0; t < 5; t++) {
    c.decide({
      sessionKey: "s1", triggerKind: "user_turn", turnIndex: t,
      userRequestedTier: "tier1", consentSource: "ui_chip",
      struggle: struggleSig(0.1),
    });
  }
  // ui_chip is sticky — even with low struggle, no de-escalation.
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 5,
    consentSource: "ui_chip",  // would-be sticky, but no userRequestedTier this turn
    struggle: struggleSig(0.1),
  });
  // No userRequestedTier on this turn → controller looks at sessionState.
  // currentTier was set to tier1 via prior userRequestedTier calls; only
  // de-escalates when consent_source === "ask_hint_agreed".
  assert.notEqual(result.tier, "tier2", "ui_chip consent should NOT auto-deescalate");
});

test("user_turn tier1 → tier2 de-escalation fires for ask_hint_agreed after stable turns", () => {
  const c = newController({
    user_facing: {
      tier1_destabilize_threshold: 0.3,
      tier1_destabilize_turns: 3,
    },
  });
  // Establish tier1 via ask-hint-agreed consent.
  for (let t = 0; t < 5; t++) {
    c.decide({
      sessionKey: "s1", triggerKind: "user_turn", turnIndex: t,
      userRequestedTier: "tier1", consentSource: "ask_hint_agreed",
      struggle: struggleSig(0.1),
    });
  }
  // Now NO userRequestedTier on the next turn — controller should
  // check de-escalation. State has 5 stable low-struggle turns at tier1.
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 5,
    consentSource: "ask_hint_agreed",
    struggle: struggleSig(0.1),
  });
  assert.equal(result.tier, "tier2");
  assert.equal(result.escalation_event, "deescalated");
});

// ── Background branch ───────────────────────────────────────────────────────

test("heartbeat turn 0 → default tier3", () => {
  const c = newController();
  const result = c.decide({
    sessionKey: "s1", triggerKind: "heartbeat", turnIndex: 0,
  });
  assert.equal(result.tier, "tier3");
});

test("cron_app turn 0 → tier3", () => {
  const c = newController();
  const result = c.decide({
    sessionKey: "s1", triggerKind: "cron_app", turnIndex: 0,
  });
  assert.equal(result.tier, "tier3");
});

test("background escalates tier3 → tier2 on struggle", () => {
  const c = newController({
    background: { tier3_escalate_threshold: 0.5 },
  });
  c.decide({ sessionKey: "s1", triggerKind: "heartbeat", turnIndex: 0 });
  const result = c.decide({
    sessionKey: "s1", triggerKind: "heartbeat", turnIndex: 1,
    struggle: struggleSig(0.7),
  });
  assert.equal(result.tier, "tier2");
  assert.equal(result.escalation_event, "escalated");
});

test("background does NOT escalate to tier1 without tier1_enabled opt-in", () => {
  const c = newController({
    background: {
      tier3_escalate_threshold: 0.4,
      tier2_escalate_threshold: 0.5,
      tier1_enabled: false,  // default
    },
  });
  // Drive struggle high enough to want tier1 but opt-in is off.
  c.decide({ sessionKey: "s1", triggerKind: "heartbeat", turnIndex: 0 });
  c.decide({
    sessionKey: "s1", triggerKind: "heartbeat", turnIndex: 1,
    struggle: struggleSig(0.9),
  });
  // Now at tier2. Even with sustained high struggle, tier1 NOT reached.
  for (let t = 2; t < 6; t++) {
    const r = c.decide({
      sessionKey: "s1", triggerKind: "heartbeat", turnIndex: t,
      struggle: struggleSig(0.9),
    });
    assert.notEqual(r.tier, "tier1");
  }
});

test("background tier2 → tier3 de-escalation with hysteresis", () => {
  const c = newController({
    background: {
      tier3_escalate_threshold: 0.5,
      tier2_destabilize_threshold: 0.2,
      tier2_destabilize_turns: 3,
    },
  });
  c.decide({ sessionKey: "s1", triggerKind: "heartbeat", turnIndex: 0 });
  // Turn 1: escalate to tier2 due to high struggle.
  const r1 = c.decide({
    sessionKey: "s1", triggerKind: "heartbeat", turnIndex: 1,
    struggle: struggleSig(0.7),
  });
  assert.equal(r1.tier, "tier2");
  // Turns 2-N: low struggle, accumulate turnsAtCurrentTier. De-escalation
  // fires the FIRST turn that meets all conditions: (a) ≥ destabilize_turns
  // at tier2, (b) all last destabilize_turns scores < threshold, AND
  // (c) recentAvg < threshold. Find that turn and assert.
  let deescalatedAt = -1;
  for (let t = 2; t <= 8; t++) {
    const r = c.decide({
      sessionKey: "s1", triggerKind: "heartbeat", turnIndex: t,
      struggle: struggleSig(0.1),
    });
    if (r.escalation_event === "deescalated") {
      deescalatedAt = t;
      assert.equal(r.tier, "tier3");
      break;
    }
  }
  assert.ok(deescalatedAt > 0, `expected de-escalation within 7 stable turns, got never`);
});

test("background force_default_tier overrides tier3 default", () => {
  // Morning-briefing pattern: cron output is user-visible, operator
  // opts the bot's backgrounds into starting at tier2.
  const c = newController({
    background: { force_default_tier: "tier2" },
  });
  const result = c.decide({
    sessionKey: "s1", triggerKind: "cron_app", turnIndex: 0,
  });
  assert.equal(result.tier, "tier2");
});

// ── User-requested override always wins ─────────────────────────────────────

test("userRequestedTier=tier1 always returned", () => {
  const c = newController();
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 5,
    userRequestedTier: "tier1", consentSource: "ui_chip",
  });
  assert.equal(result.tier, "tier1");
});

test("spendCapForced overrides everything else", () => {
  const c = newController();
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 5,
    userRequestedTier: "tier1", consentSource: "ui_chip",
    spendCapForced: true,
    struggle: struggleSig(0.9),
  });
  assert.equal(result.tier, "tier3");
});

// ── Subagent inheritance ────────────────────────────────────────────────────

test("subagent defaults to user-facing branch (safer fallback per open Q#7)", () => {
  const c = newController();
  const result = c.decide({
    sessionKey: "s1", triggerKind: "subagent", turnIndex: 0,
  });
  // Should match user-facing default (tier2), not background default (tier3).
  assert.equal(result.tier, "tier2");
});

// ── State cleanup ──────────────────────────────────────────────────────────

test("clearSession removes per-session state", () => {
  const c = newController();
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 5 });
  c.clearSession("s1");
  // After clear, next turn 0 should produce the default tier (not stay at whatever was set).
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0,
  });
  assert.equal(result.tier, "tier2");
});

// ── Code-review regressions ────────────────────────────────────────────────
//
// Each block below corresponds to one finding from the post-merge code
// review of Phase 2 cascade work — see PR title for the full list.

// MEDIUM #11 — unknown trigger should default to user-facing.
test("triggerKind='unknown' defaults to user-facing branch (turn 0 → tier2)", () => {
  const c = newController();
  const result = c.decide({
    sessionKey: "s1", triggerKind: "unknown", turnIndex: 0,
  });
  assert.equal(result.tier, "tier2");
});

// MEDIUM #12 — payload-drift fail-safe (spec § 2.5).
test("demote blocked when struggle.score is null (drift fail-safe)", () => {
  const c = newController();
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  // High triviality but struggle could not be measured. Coercing
  // null → 0 used to let demote proceed; now it must hold.
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    triviality: { score: 0.9, features: {}, raw: {}, payload_drift: null },
    struggle: { score: null, features: {}, raw: {}, payload_drift: "missing" },
  });
  assert.equal(result.tier, "tier2");
  assert.equal(result.escalation_event, "held");
});

test("demote blocked when triviality.score is null (drift fail-safe)", () => {
  const c = newController();
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  const result = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    triviality: { score: null, features: {}, raw: {}, payload_drift: "missing" },
    struggle: struggleSig(0.05),
  });
  assert.equal(result.tier, "tier2");
});

// MEDIUM #13 — trigger_kind sticky across session.
test("triggerKind sticky: user_turn start, later heartbeat keeps user-facing branch", () => {
  const c = newController();
  // Session opens as user_turn → tier2 (user-facing branch).
  const r0 = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0,
  });
  assert.equal(r0.tier, "tier2");
  // A later turn arrives labeled heartbeat (e.g. OC version bump
  // re-labels mid-session). Background branch would force tier3 on this
  // input; user-facing branch holds tier2 without struggle. Sticky kind
  // wins.
  const r1 = c.decide({
    sessionKey: "s1", triggerKind: "heartbeat", turnIndex: 1,
    struggle: struggleSig(0.1),
  });
  assert.equal(r1.tier, "tier2");
});

test("triggerKind sticky: heartbeat start, later user_turn stays in background branch", () => {
  const c = newController();
  // Session opens as heartbeat → tier3.
  const r0 = c.decide({
    sessionKey: "s1", triggerKind: "heartbeat", turnIndex: 0,
  });
  assert.equal(r0.tier, "tier3");
  // Now arrives a turn labeled user_turn with low struggle. User-facing
  // branch would default to tier2 (initial tier) and ask-hint paths;
  // background branch holds tier3.
  const r1 = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    struggle: struggleSig(0.1),
  });
  assert.equal(r1.tier, "tier3");
});

// HIGH #6 — persistentStruggleTurns must reset on tier change.
test("persistentStruggleTurns resets on tier change (no immediate ask-hint after promote)", () => {
  // Walk: tier3 → tier2 (promote) on high struggle. Then sustained
  // high struggle. Should require tier2_struggle_persistence turns AT
  // tier2 before ask-hint fires — counter must reset on the promote.
  const c = newController({
    user_facing: {
      tier2_struggle_persistence: 2,
      tier2_struggle_threshold: 0.5,
      tier3_repromote_threshold: 0.4,
      demote_threshold: 0.7,
      tier1_ask_cooldown_turns: 10,
      tier1_ask_no_response_turns: 3,
    },
  });
  // Turn 0: tier2.
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  // Turn 1: high triviality + zero struggle → demote to tier3.
  const r1 = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    triviality: trivialitySig(0.85), struggle: struggleSig(0.0),
  });
  assert.equal(r1.tier, "tier3");
  // Turn 2: struggle returns hot → re-promote to tier2. This counts
  // as a tier change; persistentStruggleTurns must reset to 0.
  const r2 = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 2,
    struggle: struggleSig(0.9),
  });
  assert.equal(r2.tier, "tier2");
  assert.equal(r2.escalation_event, "escalated");
  // Turn 3: still high struggle. Without the reset, counter would
  // already be at 1 (from the promote turn's struggleScore > threshold);
  // +1 here = 2 → ask-hint fires prematurely. With the reset, counter
  // starts at 0, this turn brings it to 1, no ask-hint yet.
  const r3 = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 3,
    struggle: struggleSig(0.9),
  });
  assert.equal(r3.askHint, undefined, "ask-hint fired too early after tier change");
  // Turn 4: counter hits 2 → ask-hint fires legitimately.
  const r4 = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 4,
    struggle: struggleSig(0.9),
  });
  assert.ok(r4.askHint, "expected ask-hint on second consecutive tier2 struggle turn");
});

// HIGH #7 — ask-hint cooldown distinguishes pending vs declined.
test("ask-hint pending window: suppressed for tier1_ask_no_response_turns turns after ask", () => {
  // Spec § 2.2: after asking, wait up to no_response_turns (3) for the
  // user to respond. During that window, even if the cooldown clock
  // were satisfied somehow, suppress — user hasn't had a chance yet.
  // Then full cooldown applies from the ask.
  const c = newController({
    user_facing: {
      tier2_struggle_persistence: 1,
      tier2_struggle_threshold: 0.5,
      tier1_ask_cooldown_turns: 10,
      tier1_ask_no_response_turns: 3,
    },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  // Turn 1 emits ask-hint.
  const first = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 1,
    struggle: struggleSig(0.9),
  });
  assert.ok(first.askHint, "expected first ask-hint");
  // Turns 2..3: still within pending window — suppressed.
  for (let t = 2; t <= 3; t++) {
    const r = c.decide({
      sessionKey: "s1", triggerKind: "user_turn", turnIndex: t,
      struggle: struggleSig(0.9),
    });
    assert.equal(r.askHint, undefined, `turn ${t} should be in pending window`);
  }
  // Turn 4 — now past no_response window (askHintWaitingTurns=3),
  // user has declined. Apply 10-turn cooldown from the ask (turn 1).
  // So suppressed until turn 11.
  for (let t = 4; t <= 10; t++) {
    const r = c.decide({
      sessionKey: "s1", triggerKind: "user_turn", turnIndex: t,
      struggle: struggleSig(0.9),
    });
    assert.equal(r.askHint, undefined, `turn ${t} should be in declined cooldown`);
  }
  // Turn 11 = 10 turns after the ask → cooldown clear.
  const after = c.decide({
    sessionKey: "s1", triggerKind: "user_turn", turnIndex: 11,
    struggle: struggleSig(0.9),
  });
  assert.ok(after.askHint, "expected ask-hint after full cooldown");
});

// ── Spec lock-in ───────────────────────────────────────────────────────────

test("DEFAULT_CASCADE_CONFIG matches spec § 4.3", () => {
  assert.equal(DEFAULT_CASCADE_CONFIG.user_facing.default_tier, "tier2");
  assert.equal(DEFAULT_CASCADE_CONFIG.background.default_tier, "tier3");
  assert.equal(DEFAULT_CASCADE_CONFIG.user_facing.tier1_ask_enabled, true);
  assert.equal(DEFAULT_CASCADE_CONFIG.background.tier1_enabled, false);
  assert.equal(DEFAULT_CASCADE_CONFIG.user_facing.demote_threshold, 0.7);
  assert.equal(DEFAULT_CASCADE_CONFIG.user_facing.tier1_destabilize_turns, 5);
});

// ── Session-aggregate + judge integration (2026-06-07) ─────────────────────

test("user_turn: sessionAggregate elevation lifts persistence and fires ask-hint", () => {
  // The aggregator's elevation thresholds (≥3 shell pastes OR ≥2 bot
  // self-corrections OR sustained high velocity) should short-circuit
  // the per-turn persistence requirement — first qualifying turn fires
  // ask-hint without two prior consecutive turns of struggle.
  const c = newController({
    user_facing: {
      tier2_struggle_persistence: 2,
      tier2_struggle_threshold: 0.5,
      tier1_ask_enabled: true,
    },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  const result = c.decide({
    sessionKey: "s1",
    triggerKind: "user_turn",
    turnIndex: 1,
    // Per-turn struggle is low (0.0) — without aggregate, this turn
    // wouldn't escalate. The aggregate trips elevation.
    struggle: struggleSig(0.0),
    sessionAggregate: {
      shell_error_paste_count: 3,
      bot_self_correction_count: 0,
      turn_velocity_per_min: 0.5,
      turn_count: 4,
    },
  });
  assert.ok(result.askHint, "expected ask-hint when aggregate elevated");
});

test("user_turn: sessionAggregate BELOW elevation thresholds → no early escalation", () => {
  // Pre-thresholds (≥1) are looser than elevation thresholds (≥3, ≥2).
  // Aggregate at the pre-threshold level should NOT trigger cascade
  // escalation directly — only the judge running on top of those
  // signals can. The controller respects the asymmetry.
  const c = newController({
    user_facing: {
      tier2_struggle_persistence: 2,
      tier2_struggle_threshold: 0.5,
    },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  const result = c.decide({
    sessionKey: "s1",
    triggerKind: "user_turn",
    turnIndex: 1,
    struggle: struggleSig(0.0),
    sessionAggregate: {
      shell_error_paste_count: 1,  // below elevation threshold of 3
      bot_self_correction_count: 1,  // below threshold of 2
      turn_velocity_per_min: 0.5,
      turn_count: 4,
    },
  });
  assert.equal(result.askHint, undefined,
    "no ask-hint expected when aggregate below elevation thresholds");
});

test("user_turn: sessionJudgeVerdict=STRUGGLING lifts persistence (sharpening layer)", () => {
  // The LLM judge running on top of the aggregator's pre-thresholds
  // produces a STRUGGLING verdict. Cascade controller treats this
  // equivalently to aggregate elevation — fires ask-hint without
  // counting per-turn persistence.
  const c = newController({
    user_facing: {
      tier2_struggle_persistence: 2,
      tier2_struggle_threshold: 0.5,
      tier1_ask_enabled: true,
    },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  const result = c.decide({
    sessionKey: "s1",
    triggerKind: "user_turn",
    turnIndex: 1,
    struggle: struggleSig(0.0),
    sessionJudgeVerdict: "STRUGGLING",  // LLM looked at conversation, said yes
  });
  assert.ok(result.askHint, "expected ask-hint when LLM judge says STRUGGLING");
});

test("user_turn: sessionJudgeVerdict=OK is no-signal", () => {
  // OK means LLM looked and said this is fine — controller shouldn't
  // escalate. Same for AMBIGUOUS.
  const c = newController({
    user_facing: {
      tier2_struggle_persistence: 2,
      tier2_struggle_threshold: 0.5,
    },
  });
  c.decide({ sessionKey: "s1", triggerKind: "user_turn", turnIndex: 0 });
  for (const verdict of ["OK", "AMBIGUOUS"]) {
    const result = c.decide({
      sessionKey: "s1",
      triggerKind: "user_turn",
      turnIndex: 1,
      struggle: struggleSig(0.0),
      sessionJudgeVerdict: verdict,
    });
    assert.equal(result.askHint, undefined,
      `judge=${verdict} must not trigger escalation`);
  }
});
