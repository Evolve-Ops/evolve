/**
 * Tests for splitProviderModelRef — the coherent providerOverride +
 * modelOverride pair the before_model_resolve hook emits (2026-07-31
 * incident, defect 3).
 *
 * OC applies `modelOverride` to the modelId slot only, keeping the
 * current lane's provider unless `providerOverride` is also set.
 * Emitting the full "provider/model" ref as modelOverride minted
 * "google/anthropic/claude-haiku-4-5" during a google failover lane
 * and killed the walk with FailoverError. The split keeps the pair
 * coherent in every lane.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.splitProviderModelRef.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { splitProviderModelRef } from "../dist/observer/ModelRouter.js";

test("full ref splits into provider + model", () => {
  assert.deepEqual(splitProviderModelRef("anthropic/claude-haiku-4-5"), {
    providerOverride: "anthropic",
    modelOverride: "claude-haiku-4-5",
  });
});

test("split is on the FIRST slash — multi-segment ids keep their tail", () => {
  assert.deepEqual(splitProviderModelRef("openrouter/deepseek/deepseek-chat"), {
    providerOverride: "openrouter",
    modelOverride: "deepseek/deepseek-chat",
  });
});

test("bare model id passes through as modelOverride alone", () => {
  assert.deepEqual(splitProviderModelRef("claude-haiku-4-5"), {
    modelOverride: "claude-haiku-4-5",
  });
});

test("degenerate slashes pass through unsplit", () => {
  // Leading slash: no provider segment.
  assert.deepEqual(splitProviderModelRef("/claude"), { modelOverride: "/claude" });
  // Trailing slash: no model segment.
  assert.deepEqual(splitProviderModelRef("anthropic/"), { modelOverride: "anthropic/" });
});

test("safety-net refuse sentinel still splits into an unresolvable pair", () => {
  // The breaker's refuse sentinel must stay unresolvable (that is its
  // job) — after the split it presents provider "evolve", which is never
  // registered, so OC's lookup still fails loudly and coherently.
  assert.deepEqual(
    splitProviderModelRef("evolve/safety-net-blocked-fast-unconfigured"),
    {
      providerOverride: "evolve",
      modelOverride: "safety-net-blocked-fast-unconfigured",
    },
  );
});
