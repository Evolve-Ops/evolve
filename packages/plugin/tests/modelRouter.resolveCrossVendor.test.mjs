/**
 * Tests for ModelRouter.resolveCrossVendor — the J2 cross-vendor judge
 * derivation (internal/design-judge-role-collapse-2026-08-21.md §5.2).
 *
 * The TS mirror of primary_bot.resolve_cross_vendor: the first credentialed
 * model in the against-role's resolved rung whose provider differs from the
 * resolved model's provider — or null when no such model exists (null is
 * meaningful: a single-provider pod gets NO cross-vendor judge). The rung's
 * models[] order stands in for the operator's provider_order rank (easy-setup
 * sorts the chain at write time), so the head-to-tail walk IS the
 * provider-preference walk. These cases mirror
 * packages/analyzer/tests/test_resolve_cross_vendor.py (minus the
 * Python-only None-credentialed fail-open case — the TS side always has a
 * concrete credentialed set); the shared-fixture parity pin lives in
 * modelRouter.availabilityParity.test.mjs.
 *
 * Provider names here are fixture DATA (fake providers pa/pb/pc), not
 * literals in logic.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.resolveCrossVendor.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

// Isolate HOME so nothing in ModelRouter can read the laptop's real
// ~/.openclaw (auth-profiles / evolve-tiers) — the credentialed set is
// injected via the test seam, but the isolation keeps that a guarantee
// rather than an implementation detail.
const FAKE_HOME = fs.mkdtempSync(path.join(os.tmpdir(), "mr-xv-home-"));
process.env.HOME = FAKE_HOME;

/**
 * A three-provider catalog with distinct chains per rung. models[] order
 * stands in for the operator's provider_order rank (pa > pb > pc).
 * Same shape as the Python test's _catalog().
 */
function catalog() {
  return {
    rungs: [
      { id: "low-rung", models: ["pb/small-b", "pa/small-a", "pc/small-c"] },
      { id: "mid-rung", models: ["pa/mid-a", "pb/mid-b", "pc/mid-c"] },
      { id: "top-rung", models: ["pa/top-a"] },
    ],
    roles: {
      fast: "low-rung",
      standard: "mid-rung",
      power: "top-rung",
      max: "top-rung",
    },
  };
}

function routerWith(credentialed) {
  const r = new ModelRouter(catalog(), "", "");
  r._setCredentialedProvidersForTest(credentialed);
  return r;
}

test("multi-provider: picks first credentialed different-provider in rank order", () => {
  // standard resolves pa/mid-a; the walk skips pa, lands on pb (rank order).
  assert.equal(routerWith(["pa", "pb", "pc"]).resolveCrossVendor(), "pb/mid-b");
});

test("uncredentialed provider is skipped in rank order", () => {
  // pb holds no key: the walk must pass over pb/mid-b and land on pc.
  assert.equal(routerWith(["pa", "pc"]).resolveCrossVendor(), "pc/mid-c");
});

test("single-provider pod returns null (meaningful, not a failure)", () => {
  assert.equal(routerWith(["pa"]).resolveCrossVendor(), null);
});

test("againstRole parameterization: fast diffs against fast's resolved provider", () => {
  // fast resolves pb/small-b (first in ITS chain) → cross-vendor is the pa
  // model — a different answer than against standard, same catalog.
  assert.equal(routerWith(["pa", "pb", "pc"]).resolveCrossVendor("fast"), "pa/small-a");
});

test("walk covers the rung resolution landed in (degradation-aware)", () => {
  // max's rung is pa-only; with pa uncredentialed, max degrades down the
  // ladder and resolves pb/mid-b in mid-rung. The walk must cover THAT
  // rung — the chain that actually produces the judged work.
  assert.equal(routerWith(["pb", "pc"]).resolveCrossVendor("max"), "pc/mid-c");
});

test("unresolvable againstRole returns null (nothing to diff against)", () => {
  assert.equal(routerWith([]).resolveCrossVendor(), null);
});

test("empty catalog returns null", () => {
  const r = new ModelRouter({}, "", "");
  r._setCredentialedProvidersForTest(["pa"]);
  assert.equal(r.resolveCrossVendor(), null);
});
