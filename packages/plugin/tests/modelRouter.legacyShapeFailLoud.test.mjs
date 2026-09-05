/**
 * Legacy tier0-tier3 config shape → FAIL LOUD, never translate, never
 * silently misroute.
 *
 * The runtime legacy-shape fallback (_LEGACY_TIER_TO_ROLE /
 * _LEGACY_TIER_TO_RUNG translation at routing time) was removed 2026-08-15
 * after both production pods verified fully migrated. The tier→role mapping
 * now lives only in migrate_model_roles.py (writer) and primary_bot.py
 * (admin read side). Contract pinned here:
 *
 *   - Pure shape functions (synthesizeRungsRoles / mergeModelCatalog /
 *     normalizeRouting) THROW LegacyTierShapeError naming the remediation
 *     (`sudo evolve-admin migrate-model-roles --apply`).
 *   - Production seams (constructor, reloadConfig) do NOT throw — a throw
 *     at plugin init would take down the whole plugin, security hooks
 *     included. They poison the router instead: every turn resolves to
 *     LEGACY_CONFIG_REFUSE_SENTINEL (unresolvable → the turn errors loudly)
 *     and the remediation is console.error'd once. Doctrine: a breaker
 *     must degrade or refuse, never escalate — routing on code defaults /
 *     bot default could be a MORE expensive model than the operator chose.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.legacyShapeFailLoud.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  ModelRouter,
  LegacyTierShapeError,
  LEGACY_CONFIG_REFUSE_SENTINEL,
  legacyTiersRefuseConfig,
  synthesizeRungsRoles,
  mergeModelCatalog,
  normalizeRouting,
} from "../dist/observer/ModelRouter.js";

// Isolate HOME: constructor/reload paths consult ~/.openclaw/evolve-tiers.json;
// a stray file in the developer's real home must not leak into these tests.
process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), "mr-legacy-test-"));

const LEGACY_MODELS = {
  tiers: {
    tier2: { models: ["workhorse/model"] },
    tier3: { models: ["grunt/model"] },
  },
};

const REMEDIATION = "migrate-model-roles --apply";

// ── Pure functions throw the explicit error ─────────────────────────────────

test("synthesizeRungsRoles: legacy tiers shape throws with remediation", () => {
  assert.throws(
    () => synthesizeRungsRoles(LEGACY_MODELS),
    (e) => e instanceof LegacyTierShapeError &&
      e.message.includes(REMEDIATION) &&
      e.message.includes("tier2"),
  );
});

test("synthesizeRungsRoles: mixed shape (rungs + stale tiers.tierN) also throws", () => {
  // The old freshness-advisory pollution class: rungs present would have
  // silently won on the discriminator, hiding the stale legacy key.
  const mixed = {
    rungs: [{ id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" }],
    roles: { standard: "sonnet-class" },
    tiers: { tier1: { models: ["power/model"] } },
  };
  assert.throws(() => synthesizeRungsRoles(mixed), LegacyTierShapeError);
});

test("synthesizeRungsRoles: vestigial EMPTY tiers:{} is tolerated (no tierN keys, no intent)", () => {
  const out = synthesizeRungsRoles({ tiers: {}, rungs: [], roles: {} });
  assert.deepEqual(out, { rungs: [], roles: {} });
});

test("mergeModelCatalog: legacy bot layer throws (not silently shadowed by defaults)", () => {
  assert.throws(() => mergeModelCatalog({}, LEGACY_MODELS), LegacyTierShapeError);
});

test("mergeModelCatalog: legacy pod layer throws too", () => {
  assert.throws(() => mergeModelCatalog(LEGACY_MODELS, {}), LegacyTierShapeError);
});

test("normalizeRouting: legacy *Tier keys throw", () => {
  assert.throws(
    () => normalizeRouting({ enabled: true, maintenanceTier: "tier3" }),
    (e) => e instanceof LegacyTierShapeError && e.message.includes("maintenanceTier"),
  );
});

test("normalizeRouting: legacy tierN VALUE in a *Role key throws", () => {
  assert.throws(
    () => normalizeRouting({ enabled: true, backgroundRole: "tier3" }),
    (e) => e instanceof LegacyTierShapeError && e.message.includes("backgroundRole"),
  );
});

test("normalizeRouting: role-shaped block still normalizes", () => {
  const r = normalizeRouting({ enabled: true, maintenanceRole: "fast", ambiguousRole: null });
  assert.equal(r.maintenanceRole, "fast");
  assert.equal(r.ambiguousRole, null);
});

// ── Production seams poison instead of throwing ─────────────────────────────

test("constructor: legacy config does NOT throw; every turn refuses with the sentinel", () => {
  // Capture the one-time console.error (this file's process logs it here,
  // the first poisoning in the process).
  const errors = [];
  const origError = console.error;
  console.error = (...args) => errors.push(args.join(" "));
  let r;
  try {
    r = new ModelRouter(
      { ...LEGACY_MODELS, routing: { enabled: true } },
      "", "",
    );
  } finally {
    console.error = origError;
  }

  assert.equal(errors.length, 1, "remediation must be console.error'd exactly once");
  assert.ok(errors[0].includes(REMEDIATION));

  // Every session class refuses — routed AND unrouted alike: none of the
  // shape-derived config is trustworthy, and the sentinel is unresolvable
  // so the turn errors loudly instead of running on a guessed model.
  for (const cls of ["productive", "maintenance", "background"]) {
    const key = `s-${cls}`;
    r.setSessionType(key, cls);
    assert.equal(
      r.resolveModelOverride(key),
      LEGACY_CONFIG_REFUSE_SENTINEL,
      `session class ${cls} must refuse`,
    );
    assert.equal(r.getLastDecisionDriver(key), "legacy_config");
  }

  // A user pull cannot bypass the poison either.
  r.setUserTier("s-pull", "max");
  assert.equal(r.resolveModelOverride("s-pull"), LEGACY_CONFIG_REFUSE_SENTINEL);
});

test("reloadConfig: legacy tiers.json poisons the router (no silent keep-old)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mr-legacy-reload-"));
  try {
    const networkPath = path.join(dir, "network.json");
    fs.writeFileSync(networkPath, "{}");
    const tiersPath = path.join(dir, "tiers.json");
    fs.writeFileSync(tiersPath, JSON.stringify({
      tiers: { tier3: { models: ["grunt/model"] } },
      routing: { enabled: true },
    }));

    // Healthy rungs/roles constructor config — the reload must NOT keep it.
    const r = new ModelRouter({
      rungs: [{ id: "haiku-class", models: ["grunt/model"], costClass: "low" }],
      roles: { fast: "haiku-class" },
      routing: { enabled: true },
    }, dir, "team_bot_a");
    r.setSessionType("s1", "background");
    assert.equal(r.resolveModelOverride("s1"), "grunt/model", "pre-reload sanity");

    r.reloadConfig(networkPath, tiersPath, dir, "team_bot_a");
    assert.equal(
      r.resolveModelOverride("s1"),
      LEGACY_CONFIG_REFUSE_SENTINEL,
      "a legacy-shaped reload must refuse, not silently keep the old catalog",
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ── Sentinel + refuse-config shape contracts ────────────────────────────────

test("sentinel parses as a provider/model pair on a never-registered provider", () => {
  assert.ok(LEGACY_CONFIG_REFUSE_SENTINEL.startsWith("evolve/"));
  assert.ok(!LEGACY_CONFIG_REFUSE_SENTINEL.includes(":"));
});

test("legacyTiersRefuseConfig carries the message and routes nothing", () => {
  const cfg = legacyTiersRefuseConfig("boom");
  assert.equal(cfg.legacyConfigError, "boom");
  assert.deepEqual(cfg.rungs, []);
  assert.deepEqual(cfg.roles, {});
  assert.equal(cfg.routing.enabled, true);
});
