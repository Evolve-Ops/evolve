/**
 * Tests for estimateCost — the plugin-side hot-path cost estimator.
 *
 * Why this exists (2026-08-31): the PoC personal-assistant bot ran on
 * xai/grok-4 and every turn's estimated cost came out $0.00 — the pricing
 * table held only the Anthropic families and the no-match tail returned 0.
 * That zero fed the session-budget breaker, the runaway-rate cap, the
 * turn annotation's cost_estimated (→ per-app rollups), and the shared
 * turn record. The house doctrine is that an unmeasured turn must never
 * present as free: known non-Anthropic families are now priced, and a
 * known provider's unknown model falls back to the provider-level rate
 * (mirroring OFFLINE_PROVIDER_PRICING in packages/analyzer/turn_cost.py).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.estimateCost.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { estimateCost, _buildEvolveSubagentTurn } from "../dist/observer/TurnObserver.js";

test("xai grok-4 is priced, not free (the 2026-08-31 PoC-bot regression)", () => {
  // 50k input + 1k output at grok's $3/$15 per MTok = 0.15 + 0.015
  const cost = estimateCost("grok-4", 50_000, 1_000, 0, 0, "xai");
  assert.ok(Math.abs(cost - 0.165) < 1e-9, `expected ~$0.165, got ${cost}`);
  // Provider arg not required when the model family is in the table.
  assert.equal(estimateCost("grok-4", 50_000, 1_000, 0, 0), cost);
});

test("specific grok variants match before the family fallback", () => {
  // grok-3-mini: $0.30/$0.50 per MTok — must not hit the generic "grok" row.
  const mini = estimateCost("grok-3-mini", 1_000_000, 1_000_000, 0, 0);
  assert.ok(Math.abs(mini - 0.80) < 1e-9, `expected $0.80, got ${mini}`);
  const fast = estimateCost("grok-4-1-fast", 1_000_000, 0, 0, 0);
  assert.ok(Math.abs(fast - 5.0) < 1e-9, `expected $5.00, got ${fast}`);
});

test("openai and google families are priced", () => {
  // gpt-4o-mini must win over the gpt-4o row (more-specific key first).
  const mini = estimateCost("gpt-4o-mini", 1_000_000, 0, 0, 0);
  assert.ok(Math.abs(mini - 0.15) < 1e-9, `expected $0.15, got ${mini}`);
  const full = estimateCost("gpt-4o", 1_000_000, 0, 0, 0);
  assert.ok(Math.abs(full - 2.5) < 1e-9, `expected $2.50, got ${full}`);
  const flashLite = estimateCost("gemini-2.0-flash-lite", 1_000_000, 0, 0, 0);
  assert.ok(Math.abs(flashLite - 0.075) < 1e-9, `expected $0.075, got ${flashLite}`);
});

test("known provider's unknown model uses the provider fallback rate", () => {
  // A brand-new xai model id with no family match: xai provider rate $3/MTok in.
  const cost = estimateCost("shiny-new-model", 1_000_000, 0, 0, 0, "xai");
  assert.ok(Math.abs(cost - 3.0) < 1e-9, `expected $3.00, got ${cost}`);
  // Provider can also come from a qualified model id's prefix.
  const prefixed = estimateCost("mistral/some-future-model", 1_000_000, 0, 0, 0);
  assert.ok(Math.abs(prefixed - 2.0) < 1e-9, `expected $2.00, got ${prefixed}`);
});

test("unknown model on unknown provider still returns 0 (surfaced downstream)", () => {
  // The analyzer's turn_cost re-estimates zero-cost turns and counts the
  // truly unpriceable ones (B6); the TS estimator's contract is only that
  // it never invents a price for a provider it knows nothing about.
  assert.equal(estimateCost("mystery-model", 10_000, 500, 0, 0), 0);
  assert.equal(estimateCost("mystery-model", 10_000, 500, 0, 0, "mystery-cloud"), 0);
});

test("_buildEvolveSubagentTurn prices a grok subagent call via its provider", () => {
  const built = _buildEvolveSubagentTurn("summarizer", {
    model: "grok-4",
    provider: "xai",
    usage: { input: 50_000, output: 1_000 },
  });
  assert.ok(built);
  assert.ok(
    built.costEstimated > 0.16 && built.costEstimated < 0.17,
    `cost ${built.costEstimated} out of expected grok range`,
  );
});
