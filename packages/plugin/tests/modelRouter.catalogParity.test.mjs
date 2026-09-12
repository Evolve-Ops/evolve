/**
 * TS<->Python keyed-merge parity.
 *
 * Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 2.
 *
 * The default model catalog ships in code on BOTH sides (ModelRouter.ts and
 * primary_bot.py). The two must resolve a given (pod, bot) override pair to the
 * SAME five-role primaries. This test pins the TS side to the shared fixture
 * tests/fixtures/model-catalog-parity.json; the Python test
 * (test_model_catalog_parity.py) pins the Python side to the same file. Edit
 * the fixture once; both sides are held to it.
 *
 * It also doubles as the bare-pod resolution matrix: defaults-only resolves all
 * five roles, pod beats default, bot beats pod, and `max` resolves to
 * claude-fable-5 with NO fable config anywhere.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.catalogParity.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import { mergeModelCatalog, synthesizeRungsRoles } from "../dist/observer/ModelRouter.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = path.join(__dirname, "fixtures", "model-catalog-parity.json");
const ROLES = ["fast", "standard", "power", "max"];

function rungModels(merged, role) {
  const { rungs, roles } = synthesizeRungsRoles(merged);
  const entry = roles[role];
  const rungId = typeof entry === "string" ? entry : undefined;
  if (!rungId) return [];
  const rung = rungs.find((r) => r.id === rungId);
  return rung && rung.models.length ? rung.models : [];
}

function resolvePrimary(merged, role) {
  const models = rungModels(merged, role);
  if (models.length === 0) return null;
  return models[0];
}

const { scenarios } = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));

for (const scenario of scenarios) {
  test(`parity: ${scenario.name}`, () => {
    const merged = mergeModelCatalog(scenario.pod, scenario.bot);
    for (const role of ROLES) {
      const expected = scenario.expect[role];
      const actual = resolvePrimary(merged, role);
      assert.equal(
        actual,
        expected,
        `${scenario.name}: role ${role} resolved to ${actual}, fixture expects ${expected}`,
      );
    }
  });
}

test("parity: bare pod arms max from defaults", () => {
  const merged = mergeModelCatalog({}, {});
  assert.equal(resolvePrimary(merged, "max"), "anthropic/claude-fable-5");
});

test("parity: default max cap is 5, power cap is 10", () => {
  const merged = mergeModelCatalog({}, {});
  assert.equal(merged.roleCaps.max.maxPerDayPerBot, 5);
  assert.equal(merged.roleCaps.power.maxPerDayPerBot, 10);
});
