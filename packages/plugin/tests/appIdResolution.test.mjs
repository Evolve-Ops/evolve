/**
 * D4 fixture test — TS side of the shared app-id resolution vector.
 *
 * audit-app-framework-2026-07-02 D4: `appScriptRegistry.appIdOf` (TS) and
 * `app_integrity_coverage.resolve_app_id` (PY) hand-mirror the priority order
 * `pkg_id → id → spec_id → instance_id`. A silent drift re-opens the #3387
 * class (coverage badge never clears). This test and
 * packages/admin/tests/test_app_id_resolution_fixture.py consume the SAME
 * JSON vector (tests/fixtures/app-id-resolution.json) so the order can only
 * change on both sides at once. AL-1.4 later collapses the order to `app_id`
 * behind this same fixture.
 *
 * `expected_id: null` in the fixture means "no id resolves"; the TS resolver's
 * documented fallback for that case is the string "unknown".
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/appIdResolution.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { appIdOf } from "../dist/integrity/appScriptRegistry.js";

const fixturePath = join(
  dirname(fileURLToPath(import.meta.url)), "fixtures", "app-id-resolution.json",
);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));

const TS_NO_ID_FALLBACK = "unknown";

test("fixture is well-formed and non-trivial", () => {
  assert.ok(Array.isArray(fixture.cases));
  assert.ok(fixture.cases.length >= 10, "vector must stay substantive");
  const names = new Set(fixture.cases.map((c) => c.name));
  assert.equal(names.size, fixture.cases.length, "case names must be unique");
});

for (const c of fixture.cases) {
  test(`appIdOf resolution order: ${c.name}`, () => {
    const expected = c.expected_id === null ? TS_NO_ID_FALLBACK : c.expected_id;
    assert.equal(appIdOf(c.manifest), expected);
  });
}
