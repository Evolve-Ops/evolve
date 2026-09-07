/**
 * Tests for _triggerKindToSessionClass — the trigger_kind → session_class
 * mapping used by the pre-classification anchor in TurnObserver.
 *
 * The anchor runs in before_model_resolve BEFORE the classifier (which
 * lives in agent_end), so for single-turn auto-driven sessions
 * (heartbeats, scheduled crons, in-session subagents) the trigger
 * decides which tier model selection picks. Mapping mistakes here
 * route auto turns to the wrong tier — a heartbeat that maps to
 * "productive" would silently fall through to primary=Sonnet, which
 * is exactly the bug this fix addresses.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/triggerKindToSessionClass.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { _triggerKindToSessionClass } from "../dist/observer/TurnObserver.js";


test("heartbeat → background (clock-fired tier3 routing)", () => {
  assert.equal(_triggerKindToSessionClass("heartbeat"), "background");
});

test("cron_app → background (scheduled tier3 routing)", () => {
  assert.equal(_triggerKindToSessionClass("cron_app"), "background");
});

test("subagent → maintenance (in-session scaffolding stays tier3)", () => {
  assert.equal(_triggerKindToSessionClass("subagent"), "maintenance");
});

test("summarizer → maintenance", () => {
  assert.equal(_triggerKindToSessionClass("summarizer"), "maintenance");
});

test("classifier → maintenance", () => {
  assert.equal(_triggerKindToSessionClass("classifier"), "maintenance");
});

test("task_extractor → maintenance", () => {
  assert.equal(_triggerKindToSessionClass("task_extractor"), "maintenance");
});

test("fallback → maintenance", () => {
  assert.equal(_triggerKindToSessionClass("fallback"), "maintenance");
});

test("user_turn → null (don't anchor; classifier sees message content)", () => {
  assert.equal(_triggerKindToSessionClass("user_turn"), null);
});

test("unknown → null (don't anchor on indeterminate trigger)", () => {
  assert.equal(_triggerKindToSessionClass("unknown"), null);
});

test("empty / undefined trigger → null", () => {
  assert.equal(_triggerKindToSessionClass(""), null);
  assert.equal(_triggerKindToSessionClass(undefined), null);
});

test("REGRESSION: every auto-driven trigger anchors away from null", () => {
  // The whole point of the fix: if a trigger represents auto-driven
  // work, the mapping MUST produce a non-null session_class so the
  // anchor fires and tier3 routing applies. Add a new auto trigger
  // upstream → the mapping must add it here or auto turns silently
  // fall through to primary.
  const autoTriggers = [
    "heartbeat", "cron_app",
    "subagent", "summarizer", "classifier",
    "task_extractor", "fallback",
  ];
  for (const trigger of autoTriggers) {
    assert.notEqual(
      _triggerKindToSessionClass(trigger), null,
      `${trigger} must produce a session_class to anchor tier3 routing — ` +
      `null would silently route through primary=Sonnet`
    );
  }
});
