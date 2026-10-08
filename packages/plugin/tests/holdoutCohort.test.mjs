/**
 * Tests for HoldoutCohort — deterministic 2% session assignment.
 *
 * Per spec § 2.3 Component 5 + round-3 review findings #1, #5, #6.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/holdoutCohort.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  shouldBeInHoldout,
  inheritFromParent,
  checkOversized,
  DEFAULT_HOLDOUT_CONFIG,
} from "../dist/observer/HoldoutCohort.js";

// ── Determinism ──────────────────────────────────────────────────────────────

test("same (bot_id, session_id) always produces same assignment", () => {
  // Call many times; result must be consistent.
  const first = shouldBeInHoldout("team_bot_a", "sess-12345");
  for (let i = 0; i < 100; i++) {
    assert.equal(shouldBeInHoldout("team_bot_a", "sess-12345"), first);
  }
});

test("different session_id → different assignments are possible", () => {
  // Across many distinct session IDs, both true and false should appear.
  // (We don't know the exact split, but with 2% rate over 200 sessions
  // we'd expect ~4 in cohort — but the count varies; we just verify
  // BOTH outcomes occur.)
  const seen = { true: 0, false: 0 };
  for (let i = 0; i < 1000; i++) {
    const r = shouldBeInHoldout("team_bot_a", `sess-${i}`);
    seen[String(r)]++;
  }
  assert.ok(seen.true > 0, "at least one session should be in holdout");
  assert.ok(seen.false > 0, "at least one session should NOT be in holdout");
});

test("different bot_id with same session_id → independent assignment", () => {
  // Different bots have different cohort assignments for the same
  // session_id. Verifies bot_id is part of the hash.
  let differed = false;
  for (let i = 0; i < 200; i++) {
    const sid = `sess-${i}`;
    if (shouldBeInHoldout("team_bot_a", sid) !== shouldBeInHoldout("admin_bot", sid)) {
      differed = true;
      break;
    }
  }
  assert.ok(differed, "expected at least one session_id to differ between bots");
});

// ── Distribution ─────────────────────────────────────────────────────────────

test("default 2% target rate produces ~2% cohort over many sessions", () => {
  // Over 10,000 sessions, the cohort rate should be near 2%.
  // Tolerance: ±0.5pp (between 1.5% and 2.5%).
  let inCohort = 0;
  const n = 10_000;
  for (let i = 0; i < n; i++) {
    if (shouldBeInHoldout("team_bot_a", `sess-${i}`)) inCohort++;
  }
  const rate = inCohort / n;
  assert.ok(
    rate >= 0.015 && rate <= 0.025,
    `expected ~2% rate, got ${(rate * 100).toFixed(2)}%`,
  );
});

test("custom target_rate of 10% produces ~10% cohort", () => {
  let inCohort = 0;
  const n = 10_000;
  const config = { target_rate: 0.1 };
  for (let i = 0; i < n; i++) {
    if (shouldBeInHoldout("team_bot_a", `sess-${i}`, config)) inCohort++;
  }
  const rate = inCohort / n;
  assert.ok(
    rate >= 0.09 && rate <= 0.11,
    `expected ~10% rate, got ${(rate * 100).toFixed(2)}%`,
  );
});

test("target_rate = 0 → never in cohort", () => {
  for (let i = 0; i < 100; i++) {
    assert.equal(shouldBeInHoldout("team_bot_a", `sess-${i}`, { target_rate: 0 }), false);
  }
});

test("target_rate = 1 → always in cohort", () => {
  for (let i = 0; i < 100; i++) {
    assert.equal(shouldBeInHoldout("team_bot_a", `sess-${i}`, { target_rate: 1 }), true);
  }
});

// ── Disable flag ─────────────────────────────────────────────────────────────

test("enabled=false → never in cohort, regardless of target_rate", () => {
  for (let i = 0; i < 100; i++) {
    assert.equal(
      shouldBeInHoldout("team_bot_a", `sess-${i}`, { enabled: false, target_rate: 0.5 }),
      false,
    );
  }
});

// ── Defensive: missing inputs ────────────────────────────────────────────────

test("empty bot_id → never in cohort", () => {
  assert.equal(shouldBeInHoldout("", "sess-1"), false);
});

test("empty session_id → never in cohort", () => {
  assert.equal(shouldBeInHoldout("team_bot_a", ""), false);
});

// ── Subagent inheritance ─────────────────────────────────────────────────────

test("inheritFromParent passes through the parent's assignment", () => {
  assert.equal(inheritFromParent(true), true);
  assert.equal(inheritFromParent(false), false);
});

// ── Oversized cohort detection ──────────────────────────────────────────────

test("checkOversized: under default threshold (5%) → not oversized", () => {
  const r = checkOversized(0.02);
  assert.equal(r.oversized, false);
  assert.equal(r.observed_rate, 0.02);
  assert.equal(r.threshold, 0.05);
});

test("checkOversized: at exactly default threshold → not oversized (strict >)", () => {
  const r = checkOversized(0.05);
  assert.equal(r.oversized, false);
});

test("checkOversized: above default threshold → oversized", () => {
  const r = checkOversized(0.08);
  assert.equal(r.oversized, true);
});

test("checkOversized: custom threshold honored", () => {
  const r = checkOversized(0.03, { oversized_threshold: 0.025 });
  assert.equal(r.oversized, true);
  assert.equal(r.threshold, 0.025);
});

// ── Spec lock-in ─────────────────────────────────────────────────────────────

test("DEFAULT_HOLDOUT_CONFIG matches spec § 2.3 Component 5", () => {
  assert.equal(DEFAULT_HOLDOUT_CONFIG.enabled, true);
  assert.equal(DEFAULT_HOLDOUT_CONFIG.target_rate, 0.02);
  assert.equal(DEFAULT_HOLDOUT_CONFIG.oversized_threshold, 0.05);
});
