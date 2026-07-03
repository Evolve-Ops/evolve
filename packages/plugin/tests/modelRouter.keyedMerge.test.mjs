/**
 * Tests for the keyed pod-base ⊕ per-bot catalog merge.
 *
 * Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum (A.4).
 *
 * Block-precedence (the pre-Addendum behavior) made a pod-wide adoption
 * invisible because every bot carries per-bot rungs. The keyed merge fixes
 * that: rungs merge by id, roles/roleCaps by key. This test pins the merge
 * semantics directly AND the end-to-end effect through reloadConfig: a
 * `max` role pointing at a pod-only `fable-class` rung resolves even though
 * the per-bot file omits that rung.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.keyedMerge.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  ModelRouter,
  mergeModelCatalog,
  tierOverrideIsBroken,
} from "../dist/observer/ModelRouter.js";

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "mr-merge-test-"));
}

test("mergeModelCatalog: per-bot rung overrides base by id", () => {
  // Pure two-layer kernel (includeDefaults: false) — exercises the by-id
  // override rule in isolation, without the code-default base layer folded in.
  const base = { rungs: [{ id: "sonnet-class", models: ["old"], costClass: "medium" }] };
  const over = { rungs: [{ id: "sonnet-class", models: ["new"], costClass: "medium" }] };
  const m = mergeModelCatalog(base, over, { includeDefaults: false });
  assert.equal(m.rungs.length, 1);
  assert.deepEqual(m.rungs[0].models, ["new"]);
});

test("mergeModelCatalog: pod-only rung is visible (appended in pod order)", () => {
  const base = {
    rungs: [
      { id: "haiku-class", models: ["anthropic/claude-haiku-4-5"], costClass: "low" },
      { id: "fable-class", models: ["anthropic/claude-fable-5"], costClass: "premium" },
    ],
    roles: { max: "fable-class" },
  };
  const over = {
    rungs: [{ id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6"], costClass: "medium" }],
    roles: { standard: "sonnet-class" },
  };
  const m = mergeModelCatalog(base, over);
  const ids = m.rungs.map((r) => r.id);
  assert.ok(ids.includes("fable-class"), "pod-only fable-class survives the merge");
  assert.ok(ids.includes("sonnet-class"), "per-bot sonnet-class survives");
  // roles merged by key.
  assert.equal(m.roles.max, "fable-class");
  assert.equal(m.roles.standard, "sonnet-class");
});

test("mergeModelCatalog: roleCaps merge by key (per-bot wins)", () => {
  const base = { rungs: [{ id: "fable-class", models: ["x"] }], roleCaps: { max: { maxPerDayPerBot: 5 }, power: { maxPerDayPerBot: 9 } } };
  const over = { rungs: [{ id: "s", models: ["y"] }], roleCaps: { max: { maxPerDayPerBot: 1 } } };
  const m = mergeModelCatalog(base, over);
  assert.equal(m.roleCaps.max.maxPerDayPerBot, 1, "per-bot max cap wins");
  assert.equal(m.roleCaps.power.maxPerDayPerBot, 9, "base power cap survives");
});

test("mergeModelCatalog: no rungs anywhere → block precedence (override wins)", () => {
  const base = { routing: { enabled: false } };
  const over = { routing: { enabled: true } };
  const m = mergeModelCatalog(base, over);
  assert.equal(m.routing.enabled, true);
});

test("reloadConfig: max resolves to a pod-only fable rung via keyed merge", () => {
  const dir = tmpDir();
  const networkPath = path.join(dir, "network.json");
  const tiersPath = path.join(dir, "evolve-tiers.json");

  // Pod base carries fable-class + the max role; per-bot file carries only
  // sonnet/haiku rungs (the realistic shape — every bot has per-bot rungs).
  fs.writeFileSync(
    networkPath,
    JSON.stringify({
      models: {
        rungs: [
          { id: "haiku-class", models: ["anthropic/claude-haiku-4-5"], costClass: "low" },
          { id: "fable-class", models: ["anthropic/claude-fable-5"], costClass: "premium" },
        ],
        roles: { max: "fable-class" },
      },
    }),
  );
  fs.writeFileSync(
    tiersPath,
    JSON.stringify({
      rungs: [
        { id: "haiku-class", models: ["anthropic/claude-haiku-4-5"], costClass: "low" },
        { id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6"], costClass: "medium" },
      ],
      roles: { fast: "haiku-class", standard: "sonnet-class" },
    }),
  );

  const router = new ModelRouter({ rungs: [], roles: {} }, dir, "bot");
  router.reloadConfig(networkPath, tiersPath, dir, "bot");

  // The `max` role lives only in network.models, pointing at a pod-only
  // rung. Block-precedence would have dropped it (tiersFile had rungs);
  // keyed merge surfaces it.
  assert.equal(router.resolveRoleToModel("max"), "anthropic/claude-fable-5");

  fs.rmSync(dir, { recursive: true, force: true });
});

// ── empty-rung override is a no-op (does NOT brick), and tierOverrideIsBroken ──
// Mirrors the Python merge no-op + tier_override_is_broken detector helper
// (#2561 onboarding semantics). Keep in lockstep with primary_bot.py.

test("mergeModelCatalog: empty-models override rung does not shadow base", () => {
  const base = { rungs: [{ id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6"], costClass: "medium" }] };
  const over = { rungs: [{ id: "sonnet-class", models: [], costClass: "premium" }] };
  const m = mergeModelCatalog(base, over, { includeDefaults: false });
  const rung = m.rungs.find((r) => r.id === "sonnet-class");
  // Models survive from base (empty override doesn't speak); other fields win.
  assert.deepEqual(rung.models, ["anthropic/claude-sonnet-4-6"]);
  assert.equal(rung.costClass, "premium");
});

test("tierOverrideIsBroken: explicit-empty flags, absent does not, populated does not", () => {
  // Legacy shape.
  assert.equal(tierOverrideIsBroken({ tiers: { tier2: { models: [] } } }, "tier2"), true);
  assert.equal(tierOverrideIsBroken({ tiers: { tier2: { models: ["  "] } } }, "tier2"), true);
  assert.equal(tierOverrideIsBroken({ tiers: { tier0: { models: ["openai/gpt-4o"] } } }, "tier2"), false); // tier2 omitted
  assert.equal(tierOverrideIsBroken({ tiers: { tier2: { models: ["anthropic/claude-sonnet-4-6"] } } }, "tier2"), false);
  // New shape (rung id for tier2 is sonnet-class).
  assert.equal(tierOverrideIsBroken({ rungs: [{ id: "sonnet-class", models: [] }] }, "tier2"), true);
  assert.equal(tierOverrideIsBroken({ rungs: [{ id: "opus-class", models: [] }] }, "tier2"), false); // different rung
  assert.equal(tierOverrideIsBroken({}, "tier2"), false);
});
