/**
 * Tests for _buildEvolveSubagentTurn — the llm_output-time capture of
 * Evolve's own subagent LLM calls (session summarizer, tier classifier,
 * struggle judge, preflight router).
 *
 * Why this exists (spec-evolve-overhead-budget Phase A2): OC's
 * plugin-subagent lane never fires agent_end, so the normal
 * handleTurn → writeTurnToShared path never sees these calls — the
 * 2026-07-31 A2 rollup showed Evolve-initiated kinds at $0.0000 while the
 * summarizer ran dozens of times a day. The capture writes the shared-turn
 * record at llm_output with `source` set to the canonical trigger_kind, and
 * cost_event_converter passes that source through to
 * cost_event.trigger_kind.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.evolveSubagentTurn.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { _buildEvolveSubagentTurn } from "../dist/observer/TurnObserver.js";

test("summarizer call on the bot's DEFAULT model still tags summarizer", () => {
  // Post-#3531 the model pin is denied and the run lands UNPINNED on the
  // bot's primary model — a summarizer call on Sonnet is indistinguishable
  // from a user turn by model alone. The tag must come from the session
  // key kind, never from a model heuristic.
  const built = _buildEvolveSubagentTurn("summarizer", {
    model: "claude-sonnet-4-6",
    provider: "anthropic",
    usage: { input: 12, output: 140, cacheRead: 0, cacheWrite: 8500 },
  });
  assert.ok(built);
  assert.equal(built.llm.source, "summarizer");
  assert.equal(built.llm.channel, "subagent");
  assert.equal(built.llm.model, "claude-sonnet-4-6");
  assert.equal(built.llm.provider, "anthropic");
  assert.equal(built.llm.inputTokens, 12);
  assert.equal(built.llm.outputTokens, 140);
  assert.equal(built.llm.cacheWriteTokens, 8500);
  // Sonnet pricing: 12/1M*3 + 140/1M*15 + 8500/1M*3.75 ≈ $0.034011
  assert.ok(built.costEstimated > 0.03 && built.costEstimated < 0.04,
    `cost ${built.costEstimated} out of expected Sonnet range`);
});

test("classifier kind passes through as the record source", () => {
  const built = _buildEvolveSubagentTurn("classifier", {
    model: "claude-haiku-4-5",
    provider: "anthropic",
    usage: { input: 900, output: 12 },
  });
  assert.ok(built);
  assert.equal(built.llm.source, "classifier");
  assert.equal(built.llm.cacheReadTokens, 0);
  assert.equal(built.llm.cacheWriteTokens, 0);
});

test("zero-billed attempt (errored / no usage) returns null", () => {
  assert.equal(_buildEvolveSubagentTurn("summarizer", { model: "m", usage: {} }), null);
  assert.equal(_buildEvolveSubagentTurn("summarizer", {}), null);
  assert.equal(_buildEvolveSubagentTurn("summarizer", null), null);
  assert.equal(
    _buildEvolveSubagentTurn("classifier", {
      model: "claude-haiku-4-5",
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    }),
    null,
  );
});

test("missing model/provider degrade to 'unknown', not a crash", () => {
  const built = _buildEvolveSubagentTurn("classifier", {
    usage: { input: 5, output: 5 },
  });
  assert.ok(built);
  assert.equal(built.llm.model, "unknown");
  assert.equal(built.llm.provider, "unknown");
  // Unknown model → estimateCost finds no pricing row → $0 estimate,
  // but the token counts still land in the record for the rollup.
  assert.equal(built.costEstimated, 0);
  assert.equal(built.llm.inputTokens, 5);
});

test("malformed usage values coerce to numbers instead of NaN-poisoning", () => {
  const built = _buildEvolveSubagentTurn("summarizer", {
    model: "claude-haiku-4-5",
    provider: "anthropic",
    usage: { input: "37", output: "not-a-number", cacheRead: null, cacheWrite: undefined },
  });
  assert.ok(built);
  assert.equal(built.llm.inputTokens, 37);
  assert.equal(built.llm.outputTokens, 0);
  assert.equal(built.llm.cacheReadTokens, 0);
  assert.equal(built.llm.cacheWriteTokens, 0);
  assert.ok(Number.isFinite(built.costEstimated));
});
