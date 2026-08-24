/**
 * TS<->Python availability-aware resolution parity.
 *
 * Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 3.A.
 *
 * resolveRoleAvailability (TS) and resolve_role_with_availability (Python)
 * must resolve each (pod, bot, credentialed) triple to the SAME
 * {model, resolvedRole, degraded, reason} per role. This pins the TS side to
 * the shared fixture tests/fixtures/model-availability-parity.json; the Python
 * test (test_model_availability_parity.py) pins the Python side to the same
 * file. Edit the fixture once; both sides are held to it.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.availabilityParity.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import { ModelRouter, mergeModelCatalog } from "../dist/observer/ModelRouter.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = path.join(__dirname, "fixtures", "model-availability-parity.json");
const scenarios = JSON.parse(fs.readFileSync(FIXTURE, "utf8")).scenarios;

function routerFor(scenario) {
  const merged = mergeModelCatalog(scenario.pod, scenario.bot);
  const r = new ModelRouter(merged, "", "");
  r._setCredentialedProvidersForTest(scenario.credentialed);
  return r;
}

for (const scenario of scenarios) {
  test(`availability: ${scenario.name}`, () => {
    const r = routerFor(scenario);
    for (const [role, exp] of Object.entries(scenario.expect)) {
      const got = r.resolveRoleAvailability(role);
      assert.equal(got.model, exp.model, `${role} model`);
      assert.equal(got.resolvedRole, exp.resolvedRole, `${role} resolvedRole`);
      assert.equal(got.degraded, exp.degraded, `${role} degraded`);
      assert.equal(got.reason, exp.reason, `${role} reason`);
    }
  });
}

test("non-LLM credential is intersected out without a provider literal", () => {
  const merged = mergeModelCatalog({}, {});
  const r = new ModelRouter(merged, "", "");
  r._setCredentialedProvidersForTest(["brave", "anthropic"]);
  // brave is not in any rung cluster, so availability is {anthropic}.
  assert.equal(r.resolveRoleAvailability("max").model, "anthropic/claude-fable-5");
  // power resolves to anthropic too (opus-class leads with anthropic).
  assert.equal(r.resolveRoleAvailability("power").model, "anthropic/claude-opus-4-8");
});

test("forced pull degrades honestly when the rung is uncredentialed", () => {
  const merged = mergeModelCatalog({}, {});
  const r = new ModelRouter(merged, "", "");
  // Only openai credentialed: a forced max pull must degrade down the ladder
  // to the first credentialed rung, not die at the anthropic-only fable
  // provider. The enriched opus-class carries openai/gpt-4.1, so max degrades
  // exactly one rung (max -> power) and resolves there.
  r._setCredentialedProvidersForTest(["openai"]);
  r.setUserTier("s1", "max", "ui_chip");
  const model = r.resolveModelOverride("s1");
  // Degraded max -> power (opus-class holds openai/gpt-4.1).
  assert.equal(model, "openai/gpt-4.1");
  // The decision driver still reflects the user's explicit pull.
  assert.equal(r.getLastDecisionDriver("s1"), "user_request");
});
