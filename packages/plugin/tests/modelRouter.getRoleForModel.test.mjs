/**
 * Tests for ModelRouter.getRoleForModel — the reverse-lookup from a model
 * string back to its role ID (fast | standard | power | max | judge).
 *
 * This is load-bearing for the cascade calibration loop: cascade telemetry
 * records what role was actually used from this lookup against the model OC
 * actually ran, NOT from the cascade controller's intent. Per
 * spec-tier-cascade § 6.3 and failure-mode review F8 — without this, the
 * calibration loop reads intent (a lie) instead of truth (what billed).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.getRoleForModel.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

// Rungs/roles config (spec shape). judge rides the sonnet-class rung with a
// provider-diversity constraint and a cross-provider fallback present.
const CFG = {
  rungs: [
    { id: "haiku-class", models: ["anthropic/claude-haiku-4-5"], costClass: "low" },
    { id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6", "openai/gpt-4o"], costClass: "medium" },
    { id: "opus-class", models: ["anthropic/claude-opus-4-8"], costClass: "high" },
    { id: "fable-class", models: ["anthropic/claude-fable-5"], costClass: "premium" },
  ],
  roles: {
    fast: "haiku-class",
    standard: "sonnet-class",
    power: "opus-class",
    max: "fable-class",
    judge: { rung: "sonnet-class", provider: "not-standard" },
  },
  routing: { enabled: true },
};

function newRouter() {
  return new ModelRouter(CFG, "", "");
}

test("getRoleForModel: exact match with provider prefix → returns role", () => {
  const r = newRouter();
  assert.equal(r.getRoleForModel("anthropic/claude-sonnet-4-6"), "standard");
  assert.equal(r.getRoleForModel("anthropic/claude-haiku-4-5"), "fast");
  assert.equal(r.getRoleForModel("anthropic/claude-opus-4-8"), "power");
  assert.equal(r.getRoleForModel("anthropic/claude-fable-5"), "max");
});

test("getRoleForModel: bare model name (no provider prefix) still matches", () => {
  const r = newRouter();
  assert.equal(r.getRoleForModel("claude-sonnet-4-6"), "standard");
  assert.equal(r.getRoleForModel("claude-haiku-4-5"), "fast");
});

test("getRoleForModel: case-insensitive", () => {
  const r = newRouter();
  assert.equal(r.getRoleForModel("ANTHROPIC/CLAUDE-SONNET-4-6"), "standard");
  assert.equal(r.getRoleForModel("Claude-Fable-5"), "max");
});

test("getRoleForModel: null/undefined/empty → null", () => {
  const r = newRouter();
  assert.equal(r.getRoleForModel(null), null);
  assert.equal(r.getRoleForModel(undefined), null);
  assert.equal(r.getRoleForModel(""), null);
});

test("getRoleForModel: unknown model → null (not a guess)", () => {
  const r = newRouter();
  assert.equal(r.getRoleForModel("anthropic/claude-mystery-7-0"), null);
  assert.equal(r.getRoleForModel("totally-made-up-model"), null);
});

test("getRoleForModel: a model only in the judge fallback resolves to standard", () => {
  // openai/gpt-4o lives in the sonnet-class rung as the cross-provider
  // judge option. The standard role and the judge role both point at that
  // rung; by operational preference (standard < judge) the reverse lookup
  // attributes it to standard. judge is a selection mechanism, not a
  // distinct rung — so this is the intended behavior.
  const r = newRouter();
  assert.equal(r.getRoleForModel("openai/gpt-4o"), "standard");
});

test("getRoleForModel: same model in two rungs → operational preference wins", () => {
  // standard (0) beats power (1): a model listed in both the standard and
  // power rungs attributes to standard, the most operationally-likely role.
  const router = new ModelRouter(
    {
      rungs: [
        { id: "sonnet-class", models: ["claude-x"] },
        { id: "opus-class", models: ["claude-x"] },
      ],
      roles: { standard: "sonnet-class", power: "opus-class" },
      routing: { enabled: true },
    },
    "", "",
  );
  assert.equal(router.getRoleForModel("claude-x"), "standard");
});

test("getRoleForModel: longest-candidate match wins on tie", () => {
  const router = new ModelRouter(
    {
      rungs: [
        { id: "opus-class", models: ["claude-sonnet-4-6"] },           // shorter alias
        { id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6"] }, // longer canonical
      ],
      roles: { power: "opus-class", standard: "sonnet-class" },
      routing: { enabled: true },
    },
    "", "",
  );
  assert.equal(router.getRoleForModel("anthropic/claude-sonnet-4-6"), "standard");
});

test("getRoleForModel: exact match beats longer non-exact match", () => {
  const router = new ModelRouter(
    {
      rungs: [
        { id: "opus-class", models: ["claude-sonnet-4-6"] },
        { id: "sonnet-class", models: ["super-long-prefix/anthropic/claude-sonnet-4-6-extra"] },
      ],
      roles: { power: "opus-class", standard: "sonnet-class" },
      routing: { enabled: true },
    },
    "", "",
  );
  assert.equal(router.getRoleForModel("claude-sonnet-4-6"), "power");
});

test("getRoleForModel: ignores excessively short candidates", () => {
  const router = new ModelRouter(
    {
      rungs: [
        { id: "haiku-class", models: ["c"] },  // 1 char — must not match via substring
        { id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6"] },
      ],
      roles: { fast: "haiku-class", standard: "sonnet-class" },
      routing: { enabled: true },
    },
    "", "",
  );
  assert.equal(router.getRoleForModel("anthropic/claude-sonnet-4-6"), "standard");
});

// ── Legacy getTierForModel alias (kept for un-migrated readers) ───────────

test("getTierForModel: legacy alias maps role back to tier key", () => {
  const r = newRouter();
  assert.equal(r.getTierForModel("anthropic/claude-sonnet-4-6"), "tier2");
  assert.equal(r.getTierForModel("anthropic/claude-haiku-4-5"), "tier3");
  assert.equal(r.getTierForModel("anthropic/claude-opus-4-8"), "tier1");
  // `max` has no legacy tier key (Fable post-dates the tier scheme) → null.
  assert.equal(r.getTierForModel("anthropic/claude-fable-5"), null);
});

test("getRoleForModel: accepts a legacy {tiers} config via synthesis", () => {
  // A router constructed from the legacy tier shape synthesizes rungs/roles
  // in the constructor; the reverse-lookup then speaks roles.
  const router = new ModelRouter(
    {
      tiers: {
        tier0: { models: ["openai/gpt-4o"] },
        tier1: { models: ["anthropic/claude-opus-4-6"] },
        tier2: { models: ["anthropic/claude-sonnet-4-6"] },
        tier3: { models: ["anthropic/claude-haiku-4-5"] },
      },
      routing: { enabled: true },
    },
    "", "",
  );
  assert.equal(router.getRoleForModel("anthropic/claude-opus-4-6"), "power");
  assert.equal(router.getRoleForModel("anthropic/claude-haiku-4-5"), "fast");
  assert.equal(router.getRoleForModel("anthropic/claude-sonnet-4-6"), "standard");
});
