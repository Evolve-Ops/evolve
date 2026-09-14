/**
 * Tests for DangerousComboDetector — fixed 4-feature pattern.
 *
 * Per spec § 2.6: fires immediately on any turn matching ALL FOUR of
 * background + tier1 + cascade-decided + large-context. Single
 * occurrence = signal at WARNING. No baseline accumulation.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/dangerousComboDetector.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  detectDangerousCombo,
  DEFAULT_DANGEROUS_COMBO_CONFIG,
} from "../dist/observer/DangerousComboDetector.js";

// ── The canonical matched case ───────────────────────────────────────────

test("detectDangerousCombo: all 4 features → matched", () => {
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "cascade",
    contextTokens: 150_000,
  });
  assert.equal(result.matched, true);
  assert.equal(result.features_matched.background_origin, true);
  assert.equal(result.features_matched.tier1, true);
  assert.equal(result.features_matched.cascade_chosen, true);
  assert.equal(result.features_matched.large_context, true);
  assert.equal(result.context_tokens, 150_000);
});

test("detectDangerousCombo: cron_app trigger also counts as background", () => {
  const result = detectDangerousCombo({
    triggerKind: "cron_app",
    tierUsed: "tier1",
    tierChosenBy: "cascade",
    contextTokens: 200_000,
  });
  assert.equal(result.matched, true);
});

// ── Each individual feature failing prevents the match ───────────────────

test("detectDangerousCombo: user_turn trigger → not matched", () => {
  // user_turn is NOT a background origin. User is in the loop.
  const result = detectDangerousCombo({
    triggerKind: "user_turn",
    tierUsed: "tier1",
    tierChosenBy: "cascade",
    contextTokens: 150_000,
  });
  assert.equal(result.matched, false);
  assert.equal(result.features_matched.background_origin, false);
});

test("detectDangerousCombo: tier2 → not matched", () => {
  // tier2 is the WORKHORSE — running Sonnet on background isn't dangerous.
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier2",
    tierChosenBy: "cascade",
    contextTokens: 150_000,
  });
  assert.equal(result.matched, false);
});

test("detectDangerousCombo: user_request tier choice → not matched", () => {
  // The user explicitly chose tier1 via UI chip. That's consent, not danger.
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "user_request",
    contextTokens: 150_000,
  });
  assert.equal(result.matched, false);
  assert.equal(result.features_matched.cascade_chosen, false);
});

test("detectDangerousCombo: bot_initiated tier choice → not matched", () => {
  // Bot chose tier1 based on user intent. Some consent flowed in.
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "bot_initiated",
    contextTokens: 150_000,
  });
  assert.equal(result.matched, false);
});

test("detectDangerousCombo: ui_chip tier choice → not matched", () => {
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "ui_chip",
    contextTokens: 150_000,
  });
  assert.equal(result.matched, false);
});

test("detectDangerousCombo: small context → not matched", () => {
  // Under the threshold (100K default). Small-context tier1 background
  // is still spendy but not the "huge silent context" failure pattern.
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "cascade",
    contextTokens: 50_000,
  });
  assert.equal(result.matched, false);
  assert.equal(result.features_matched.large_context, false);
});

test("detectDangerousCombo: missing context → not matched", () => {
  // No contextTokens supplied → large_context feature is false.
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "cascade",
    // contextTokens absent
  });
  assert.equal(result.matched, false);
  assert.equal(result.features_matched.large_context, false);
});

// ── Boundary on context size ─────────────────────────────────────────────

test("detectDangerousCombo: 100K context tokens is NOT > 100K → not matched", () => {
  // Strict greater-than: 100,000 exact doesn't trip.
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "cascade",
    contextTokens: 100_000,
  });
  assert.equal(result.features_matched.large_context, false);
});

test("detectDangerousCombo: 100,001 tokens → large_context true", () => {
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "cascade",
    contextTokens: 100_001,
  });
  assert.equal(result.features_matched.large_context, true);
});

// ── Disable flag ────────────────────────────────────────────────────────

test("detectDangerousCombo: enabled=false → never matched (even if features fire)", () => {
  const result = detectDangerousCombo(
    {
      triggerKind: "heartbeat",
      tierUsed: "tier1",
      tierChosenBy: "cascade",
      contextTokens: 200_000,
    },
    { enabled: false },
  );
  assert.equal(result.matched, false);
  // But the features ARE still computed — the disable doesn't suppress
  // observation, only the match verdict.
  assert.equal(result.features_matched.background_origin, true);
  assert.equal(result.features_matched.tier1, true);
});

// ── Configurable thresholds ─────────────────────────────────────────────

test("detectDangerousCombo: custom minContextTokens honored", () => {
  // Tight threshold for tests
  const config = { minContextTokens: 1000 };
  const result = detectDangerousCombo(
    {
      triggerKind: "heartbeat",
      tierUsed: "tier1",
      tierChosenBy: "cascade",
      contextTokens: 5000,
    },
    config,
  );
  assert.equal(result.matched, true);
});

test("detectDangerousCombo: custom backgroundTriggerKinds honored", () => {
  // Treat subagent as background too
  const config = { backgroundTriggerKinds: ["heartbeat", "cron_app", "subagent"] };
  const result = detectDangerousCombo(
    {
      triggerKind: "subagent",
      tierUsed: "tier1",
      tierChosenBy: "cascade",
      contextTokens: 150_000,
    },
    config,
  );
  assert.equal(result.matched, true);
});

// ── Defensive: malformed input ──────────────────────────────────────────

test("detectDangerousCombo: NaN contextTokens → not matched", () => {
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "cascade",
    contextTokens: NaN,
  });
  assert.equal(result.matched, false);
});

test("detectDangerousCombo: Infinity contextTokens → not matched", () => {
  const result = detectDangerousCombo({
    triggerKind: "heartbeat",
    tierUsed: "tier1",
    tierChosenBy: "cascade",
    contextTokens: Infinity,
  });
  assert.equal(result.matched, false);
});

test("detectDangerousCombo: empty input → not matched, all features false", () => {
  const result = detectDangerousCombo({});
  assert.equal(result.matched, false);
  assert.equal(result.features_matched.background_origin, false);
  assert.equal(result.features_matched.tier1, false);
  assert.equal(result.features_matched.cascade_chosen, false);
  assert.equal(result.features_matched.large_context, false);
});

// ── Spec lock-in (default values) ──────────────────────────────────────

test("DEFAULT_DANGEROUS_COMBO_CONFIG: matches spec § 2.6", () => {
  assert.equal(DEFAULT_DANGEROUS_COMBO_CONFIG.enabled, true);
  assert.deepEqual(
    DEFAULT_DANGEROUS_COMBO_CONFIG.backgroundTriggerKinds,
    ["heartbeat", "cron_app"],
  );
  assert.equal(DEFAULT_DANGEROUS_COMBO_CONFIG.tier, "tier1");
  assert.deepEqual(DEFAULT_DANGEROUS_COMBO_CONFIG.cascadeChosenBySet, ["cascade"]);
  assert.equal(DEFAULT_DANGEROUS_COMBO_CONFIG.minContextTokens, 100_000);
});
