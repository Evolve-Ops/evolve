/**
 * Tests for the routing rule (D-OH2 —
 * internal/decision-evolve-overhead-2026-09-07.md).
 *
 * The contract under test:
 *   - trigger kind decides: heartbeat / cron / in-session scaffolding go
 *     to the operator's configured cheap rung, with NO model call;
 *   - a user turn is not decided by the rule unless the bot has a
 *     confident learned prior for that surface;
 *   - the prior is refused below the confidence bar, and may never name
 *     `max` (pull-only);
 *   - the operator's configured maintenance/background roles are
 *     honoured, so a pod that moved them sees the rule follow.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/routingRule.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  decideRoutingRule,
  triggerKindToSessionClass,
  priorRoleForSurface,
  DEFAULT_PRIOR_MIN_CONFIDENCE,
} from "../dist/observer/routingRule.js";

// ── The rule table, by trigger ──────────────────────────────────────────────

const AUTO_TRIGGERS = [
  ["heartbeat", "background"],
  ["cron_app", "background"],
  ["subagent", "maintenance"],
  ["summarizer", "maintenance"],
  ["classifier", "maintenance"],
  ["task_extractor", "maintenance"],
  ["fallback", "maintenance"],
];

for (const [triggerKind, expectedClass] of AUTO_TRIGGERS) {
  test(`rule: ${triggerKind} → ${expectedClass}, cheap rung, driver=trigger`, () => {
    const d = decideRoutingRule({ triggerKind });
    assert.equal(d.sessionClass, expectedClass);
    assert.equal(d.role, "fast");
    assert.equal(d.tier, "tier3");
    assert.equal(d.driver, "trigger");
    assert.equal(d.reason, `trigger:${triggerKind}`);
  });
}

test("rule: a user turn is not decided by the rule — it falls to the primary", () => {
  const d = decideRoutingRule({ triggerKind: "user_turn", surface: "slack" });
  assert.equal(d.sessionClass, null, "a person's turn is never anchored");
  assert.equal(d.role, null, "null means 'we did not decide' — the ladder does");
  assert.equal(d.tier, null);
  assert.equal(d.driver, "primary");
});

test("rule: an unknown trigger falls to the primary, not to the cheap rung", () => {
  // Guessing 'cheap' on an unrecognised trigger would silently downgrade
  // a real conversation — the exact class of change the standing rule
  // forbids. Unknown means unknown.
  const d = decideRoutingRule({ triggerKind: "something-new-in-oc" });
  assert.equal(d.role, null);
  assert.equal(d.driver, "primary");
});

test("rule: the operator's configured background/maintenance roles are honoured", () => {
  const bg = decideRoutingRule({ triggerKind: "heartbeat", backgroundRole: "standard" });
  assert.equal(bg.role, "standard", "a pod that moved backgroundRole is followed");
  assert.equal(bg.tier, "tier2");

  const mt = decideRoutingRule({ triggerKind: "subagent", maintenanceRole: "power" });
  assert.equal(mt.role, "power");
  assert.equal(mt.tier, "tier1");
});

test("rule: a configured `max` background role clamps to fast (pull-only)", () => {
  // Mirrors _resolveModelAndTier's own clamp: `max` is reachable only by
  // an explicit pull, never by a rule.
  const d = decideRoutingRule({ triggerKind: "heartbeat", backgroundRole: "max" });
  assert.equal(d.role, "fast");
});

test("rule: junk in the configured role clamps to fast", () => {
  const d = decideRoutingRule({ triggerKind: "cron_app", backgroundRole: "turbo" });
  assert.equal(d.role, "fast");
});

// ── The learned prior ───────────────────────────────────────────────────────

test("prior: a confident bot-wide prior decides a user turn", () => {
  const d = decideRoutingRule({
    triggerKind: "user_turn",
    surface: "slack",
    prior: { role: "fast", confidence: 0.95 },
  });
  assert.equal(d.role, "fast");
  assert.equal(d.driver, "bot_prior");
  assert.equal(d.reason, "bot_prior:bot");
});

test("prior: a per-surface entry beats the bot-wide value on that surface", () => {
  const prior = { role: "fast", confidence: 0.95, surfaces: { slack: "standard" } };
  assert.equal(
    decideRoutingRule({ triggerKind: "user_turn", surface: "slack", prior }).role,
    "standard",
  );
  assert.equal(
    decideRoutingRule({ triggerKind: "user_turn", surface: "telegram", prior }).role,
    "fast",
    "a surface with no entry falls back to the bot-wide value",
  );
});

test("prior: surface match is case-insensitive but exact — no near-misses", () => {
  const prior = { role: "fast", confidence: 0.95, surfaces: { slack: "standard" } };
  assert.equal(
    decideRoutingRule({ triggerKind: "user_turn", surface: "SLACK", prior }).role,
    "standard",
  );
  assert.equal(
    decideRoutingRule({ triggerKind: "user_turn", surface: "slack-connect", prior }).role,
    "fast",
    "a different surface must not inherit slack's entry",
  );
});

test("prior: refused below the confidence bar — the bot stays on its primary", () => {
  const d = decideRoutingRule({
    triggerKind: "user_turn",
    surface: "slack",
    prior: { role: "fast", confidence: DEFAULT_PRIOR_MIN_CONFIDENCE - 0.01 },
  });
  assert.equal(d.role, null, "an unconfident prior must not move a turn");
  assert.equal(d.driver, "primary");
});

test("prior: exactly at the bar is accepted", () => {
  const d = decideRoutingRule({
    triggerKind: "user_turn",
    prior: { role: "fast", confidence: DEFAULT_PRIOR_MIN_CONFIDENCE },
  });
  assert.equal(d.driver, "bot_prior");
});

test("prior: a prior naming `max` is refused (pull-only)", () => {
  const d = decideRoutingRule({
    triggerKind: "user_turn",
    prior: { role: "max", confidence: 1.0 },
  });
  assert.equal(d.role, null);
  assert.equal(d.driver, "primary");
});

test("prior: a surface entry naming `max` falls back to the bot-wide value", () => {
  const d = decideRoutingRule({
    triggerKind: "user_turn",
    surface: "slack",
    prior: { role: "fast", confidence: 0.9, surfaces: { slack: "max" } },
  });
  assert.equal(d.role, "fast");
  assert.equal(d.reason, "bot_prior:bot");
});

test("prior: the trigger beats the prior — a heartbeat is never a user turn", () => {
  const d = decideRoutingRule({
    triggerKind: "heartbeat",
    surface: "heartbeat",
    prior: { role: "power", confidence: 1.0 },
  });
  assert.equal(d.role, "fast");
  assert.equal(d.driver, "trigger");
});

test("prior: a missing confidence field is treated as no confidence", () => {
  const d = decideRoutingRule({ triggerKind: "user_turn", prior: { role: "fast" } });
  assert.equal(d.driver, "primary");
});

test("priorRoleForSurface: null prior, null surface, junk — all yield null", () => {
  assert.equal(priorRoleForSurface(null, "slack"), null);
  assert.equal(priorRoleForSurface(undefined, null), null);
  assert.equal(priorRoleForSurface({ role: "nope", confidence: 1 }, null), null);
});

// ── The one mapping ─────────────────────────────────────────────────────────

test("triggerKindToSessionClass: user_turn and unknown anchor nothing", () => {
  assert.equal(triggerKindToSessionClass("user_turn"), null);
  assert.equal(triggerKindToSessionClass("unknown"), null);
  assert.equal(triggerKindToSessionClass(""), null);
});
