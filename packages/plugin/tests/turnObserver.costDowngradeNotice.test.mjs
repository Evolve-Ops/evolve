/**
 * Tests for _buildCostDowngradeNotice — the bot-visible attribution note
 * injected (via before_prompt_build → appendSystemContext) on turns whose
 * model was forced down by a cost safety net (spend_cap / runaway).
 *
 * Incident 2026-07-31 (reference pod): a bot's daily cost breaker tripped,
 * ModelRouter overrode the next user turn from the sonnet primary to haiku,
 * and OC rendered "Model Fallback: … (selected …; selected model
 * unavailable)". The reason is false — OC builds fallback reasons only from
 * provider-failure attempts, and a hook override has none, so the default
 * "selected model unavailable" text claims a provider outage that never
 * happened. The installed gateway (dist 2026.7.1-2) offers no reason/label
 * field on the before_model_resolve result (mergeBeforeModelResolve keeps
 * only modelOverride/providerOverride), so the banner cannot be corrected;
 * this note briefs the BOT so it attributes the downgrade to cost instead of
 * confabulating an outage.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.costDowngradeNotice.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { _buildCostDowngradeNotice } from "../dist/observer/TurnObserver.js";
import { buildPrefixHashRecord } from "../dist/observer/PrefixHashLedger.js";

test("spend_cap notice names the model and the daily cap", () => {
  const note = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  assert.match(note, /^\[EVOLVE COST DOWNGRADE\]/);
  assert.match(note, /anthropic\/claude-haiku-4-5/);
  assert.match(note, /daily spending cap/);
  assert.match(note, /NOT a provider outage/);
});

test("runaway notice names the runaway-rate cap, not the daily cap", () => {
  const note = _buildCostDowngradeNotice("runaway", "anthropic/claude-haiku-4-5");
  assert.match(note, /runaway-rate cost cap/);
  assert.doesNotMatch(note, /daily spending cap/);
});

test("notice pre-empts the false OC banner reason", () => {
  // The load-bearing sentence: the bot must be told the banner's
  // "selected model unavailable" reason is wrong so it doesn't relay a
  // provider-outage misdiagnosis to the user.
  const note = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  assert.match(note, /selected model unavailable/);
  assert.match(note, /wrong/);
});

test("notice is deterministic for identical inputs (prompt-cache stability)", () => {
  // before_prompt_build may fire more than once for a run (rebuilds);
  // the entry is kept in _costDowngradeRuns so re-injection must be
  // byte-identical.
  const a = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  const b = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  assert.equal(a, b);
});

// ── Prefix-hash ledger carries the new block ────────────────────────────────

test("prefix-hash record attributes the costDowngrade block", () => {
  const note = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  const rec = buildPrefixHashRecord({
    botId: "test-bot",
    sessionId: "s1",
    turnId: "t1",
    path: "blocks",
    combined: note,
    blocks: { costDowngrade: note },
  });
  assert.equal(typeof rec.appended_block_shas.cost_downgrade, "string");
  assert.equal(rec.appended_block_shas.capabilities, null);
});

test("prefix-hash record without a downgrade records null (absent, not empty)", () => {
  const rec = buildPrefixHashRecord({
    botId: "test-bot",
    sessionId: "s1",
    turnId: "t1",
    path: "blocks",
    combined: "cap-block",
    blocks: { capabilities: "cap-block", costDowngrade: "" },
  });
  assert.equal(rec.appended_block_shas.cost_downgrade, null);
});
