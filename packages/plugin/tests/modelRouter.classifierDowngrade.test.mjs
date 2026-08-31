/**
 * Tests for routing.classifierDowngrade — the opt-in gate on CONTENT-based
 * maintenance downgrades (AI Optimization "Routing rules" card rework).
 *
 * Provenance model (ModelRouter.sessionTypeSources):
 *   - setSessionType             → 'anchor'      (trigger-kind pre-classification:
 *                                                 heartbeat/cron/subagent — objective)
 *   - setSessionTypeIfMoreSpecific → 'classifier' (agent_end content guess),
 *                                   UNLESS an anchor already owns the session
 *                                   (anchor is sticky).
 *
 * Contract:
 *   - classifier-labeled `maintenance` does NOT downgrade unless
 *     routing.classifierDowngrade === true (default off — keyword misreads
 *     have routed real human work to the cheap model).
 *   - anchor-labeled `maintenance` (scaffolding) and `background`
 *     (cron/heartbeat) route to their configured roles regardless.
 *   - a suppressed classifier downgrade falls through to the SAME branch
 *     productive uses — operator default role still applies.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.classifierDowngrade.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const RUNGS = [
  { id: "haiku-class", models: ["grunt/model"], costClass: "low" },
  { id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" },
  { id: "opus-class", models: ["power/model"], costClass: "high" },
];
const ROLES = {
  fast: "haiku-class",
  standard: "sonnet-class",
  power: "opus-class",
};

function newRouter(extra = {}) {
  return new ModelRouter(
    { rungs: RUNGS, roles: ROLES, routing: { enabled: true, ...(extra.routing ?? {}) }, ...extra },
    "", "",
  );
}

// ── classifier-labeled maintenance: gated ─────────────────────────────────────

test("classifier maintenance does NOT downgrade by default", () => {
  const r = newRouter();
  r.setSessionTypeIfMoreSpecific("s1", "maintenance");
  assert.equal(r.resolveModelOverride("s1"), null,
    "content-guessed maintenance must fall through to bot default when the opt-in is off");
});

test("classifier maintenance downgrades when classifierDowngrade=true", () => {
  const r = newRouter({ routing: { classifierDowngrade: true } });
  r.setSessionTypeIfMoreSpecific("s1", "maintenance");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

test("classifierDowngrade must be a real bool — string 'true' stays off", () => {
  const r = newRouter({ routing: { classifierDowngrade: "true" } });
  r.setSessionTypeIfMoreSpecific("s1", "maintenance");
  assert.equal(r.resolveModelOverride("s1"), null);
});

test("suppressed classifier maintenance still honors the operator default role", () => {
  // The suppression must land in the SAME branch productive uses — not a
  // hard null — so the operator's Conversations default still applies.
  const r = newRouter({ userTierOverride: { defaultRole: "standard" } });
  r.setSessionTypeIfMoreSpecific("s1", "maintenance");
  assert.equal(r.resolveModelOverride("s1"), "workhorse/model");
});

// ── anchor-labeled sessions: routed regardless ───────────────────────────────

test("anchor maintenance (scaffolding subagents) downgrades with the opt-in OFF", () => {
  const r = newRouter();
  r.setSessionType("s1", "maintenance");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model",
    "trigger-anchored maintenance is objective and must keep routing");
});

test("anchor background (cron/heartbeat) downgrades with the opt-in OFF", () => {
  const r = newRouter();
  r.setSessionType("s1", "background");
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

test("anchor provenance is sticky across a lateral classifier reclassification", () => {
  // A scaffolding subagent whose content mentions `sudo` gets laterally
  // re-labeled maintenance by the classifier. The session is still a
  // subagent — it must keep routing to the maintenance role.
  const r = newRouter();
  r.setSessionType("s1", "maintenance");                 // trigger anchor
  r.setSessionTypeIfMoreSpecific("s1", "maintenance");   // classifier lateral
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

// ── isolation / cleanup ──────────────────────────────────────────────────────

test("provenance is per-session", () => {
  const r = newRouter();
  r.setSessionType("anchored", "maintenance");
  r.setSessionTypeIfMoreSpecific("guessed", "maintenance");
  assert.equal(r.resolveModelOverride("anchored"), "grunt/model");
  assert.equal(r.resolveModelOverride("guessed"), null);
});

test("clearSession drops provenance with the type", () => {
  const r = newRouter();
  r.setSessionType("s1", "maintenance");
  r.clearSession("s1");
  // Re-created by the classifier: back to gated behavior.
  r.setSessionTypeIfMoreSpecific("s1", "maintenance");
  assert.equal(r.resolveModelOverride("s1"), null);
});
