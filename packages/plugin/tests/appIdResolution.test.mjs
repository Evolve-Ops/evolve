/**
 * D4 fixture test — TS side of the shared app-id resolution vector.
 *
 * audit-app-framework-2026-07-02 D4: the TS and PY app-id resolvers used to
 * hand-mirror the priority order `pkg_id → id → spec_id → instance_id`, and a
 * silent drift between the copies re-opens the #3387 class (coverage badge
 * never clears). AL-1.4a collapsed each side onto ONE implementation —
 * `apps/appIdentity.appIdOf` here (re-exported as `appScriptRegistry.appIdOf`,
 * the middleware's import site), `applications/app_identity.resolve_app_id`
 * there — and put the canonical `app_id` at the head of the order. This test
 * and packages/admin/tests/test_app_id_resolution_fixture.py consume the SAME
 * JSON vector (tests/fixtures/app-id-resolution.json) so the order can still
 * only change on both sides at once.
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
import { appIdOf as appIdOfDirect, draftIdOf } from "../dist/apps/appIdentity.js";

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

test("appScriptRegistry re-exports the one resolver, not a copy", () => {
  // The middleware imports appIdOf from appScriptRegistry; AL-1.4a made that
  // a re-export of apps/appIdentity. If someone re-inlines a second copy
  // there, the D4 drift class is back even though the cases above still pass.
  assert.equal(appIdOf, appIdOfDirect);
});

test("draft_id never resolves as an app id", () => {
  // Design §3: a discovered draft may be merged, renamed or dropped freely,
  // so its id is explicitly unstable and must never appear in attribution,
  // access or sharing. draftIdOf reads it; appIdOf must not.
  const draft = { draft_id: "draft-9f2c1a" };
  assert.equal(draftIdOf(draft), "draft-9f2c1a");
  assert.equal(appIdOf(draft), TS_NO_ID_FALLBACK);
  assert.equal(draftIdOf({ pkg_id: "app-a" }), "");
});
