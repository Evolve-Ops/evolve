/**
 * Tests for ModelRouter's runaway-rate hard cap.
 *
 * Per spec § 2.6: per-session $/window safety net for catching runaway
 * loops. Once tripped, sticky for the rest of the session — every
 * subsequent turn forces tier3 regardless of consent source.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.runawayRate.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const CFG = {
  rungs: [
    { id: "haiku-class", models: ["grunt/model"], costClass: "low" },
    { id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" },
    { id: "opus-class", models: ["power/model"], costClass: "high" },
    { id: "judge-class", models: ["judge/model"], costClass: "medium" },
  ],
  roles: {
    fast: "haiku-class",
    standard: "sonnet-class",
    power: "opus-class",
    judge: { rung: "judge-class", provider: "not-standard" },
  },
  routing: { enabled: true },
  // Tight thresholds for testing: $5 over 1 minute (60_000ms).
  runawayRateCap: {
    enabled: true,
    dollarsPerWindow: 5.0,
    windowMinutes: 1,
    criticalTripsPer24h: 3,
  },
};

function newRouter(cfg = CFG) {
  return new ModelRouter(cfg, "", "");
}

// ── Basic tripping ──────────────────────────────────────────────────────────

test("checkRunawayRate: no cost recorded → not tripped", () => {
  const r = newRouter();
  const result = r.checkRunawayRate("s1");
  assert.equal(result.tripped, false);
  assert.equal(result.totalUsd, 0);
});

test("checkRunawayRate: single small cost under threshold → not tripped", () => {
  const r = newRouter();
  r.recordTurnCost("s1", 1.0, 1000);
  const result = r.checkRunawayRate("s1", 1000);
  assert.equal(result.tripped, false);
  assert.equal(result.totalUsd, 1.0);
});

test("checkRunawayRate: accumulates within window → trips on threshold cross", () => {
  const r = newRouter();
  r.recordTurnCost("s1", 2.0, 1000);
  r.recordTurnCost("s1", 2.0, 2000);
  // Total: $4 — under $5 threshold. Not tripped yet.
  assert.equal(r.checkRunawayRate("s1", 2000).tripped, false);

  r.recordTurnCost("s1", 2.0, 3000);  // Total now $6 → trips.
  const result = r.checkRunawayRate("s1", 3000);
  assert.equal(result.tripped, true);
  assert.equal(result.totalUsd, 6.0);
  assert.equal(result.severity, "warning");
});

test("checkRunawayRate: single big cost trips immediately", () => {
  const r = newRouter();
  r.recordTurnCost("s1", 25.0, 1000);  // way over $5 threshold
  const result = r.checkRunawayRate("s1", 1000);
  assert.equal(result.tripped, true);
  assert.equal(result.totalUsd, 25.0);
});

// ── Stickiness ──────────────────────────────────────────────────────────────

test("isRunawayTripped: sticky once tripped (even if subsequent window is empty)", () => {
  const r = newRouter();
  r.recordTurnCost("s1", 10.0, 1000);
  r.checkRunawayRate("s1", 1000);  // trips
  assert.equal(r.isRunawayTripped("s1"), true);

  // Move forward in time past the window — but tripping is sticky.
  // checkRunawayRate at a later time wouldn't re-trip, but isRunawayTripped
  // stays true for the rest of the session.
  const result = r.checkRunawayRate("s1", 1000 + 60_000 * 10);  // 10 minutes later
  // The result.tripped depends on whether NEW costs are in window, but
  // the sticky flag remains.
  assert.equal(r.isRunawayTripped("s1"), true);
});

test("clearSession: clears runaway-trip state", () => {
  const r = newRouter();
  r.recordTurnCost("s1", 10.0, 1000);
  r.checkRunawayRate("s1", 1000);  // trips
  assert.equal(r.isRunawayTripped("s1"), true);

  r.clearSession("s1");
  assert.equal(r.isRunawayTripped("s1"), false);
});

// ── Window pruning ──────────────────────────────────────────────────────────

test("checkRunawayRate: old costs outside window don't count toward trip", () => {
  const r = newRouter();
  // $4 at t=1000 — under threshold
  r.recordTurnCost("s1", 4.0, 1000);
  // $2 at t=70000 (70s later, OUTSIDE 60s window from t=1000)
  // The window from t=70000's perspective is [10000, 70000].
  // The $4 at t=1000 is outside, so only $2 counts.
  r.recordTurnCost("s1", 2.0, 70_000);
  const result = r.checkRunawayRate("s1", 70_000);
  assert.equal(result.tripped, false);
  assert.equal(result.totalUsd, 2.0);
});

test("recordTurnCost: prunes old entries to bound memory", () => {
  const r = newRouter();
  // Record many old entries — they should be pruned on subsequent record.
  for (let i = 0; i < 100; i++) {
    r.recordTurnCost("s1", 0.01, i * 100);  // all in the first 10s
  }
  // Now record one fresh entry far in the future. Old entries should
  // be pruned away.
  r.recordTurnCost("s1", 0.50, 200_000);
  const result = r.checkRunawayRate("s1", 200_000);
  // Only the fresh $0.50 counts. Old $1 ($0.01 × 100) was pruned.
  assert.equal(result.totalUsd, 0.50);
  assert.equal(result.tripped, false);
});

// ── Session isolation ──────────────────────────────────────────────────────

test("checkRunawayRate: sessions are isolated", () => {
  const r = newRouter();
  r.recordTurnCost("s1", 10.0, 1000);   // s1 tripped
  r.recordTurnCost("s2", 1.0, 1000);    // s2 fine
  r.checkRunawayRate("s1", 1000);
  r.checkRunawayRate("s2", 1000);
  assert.equal(r.isRunawayTripped("s1"), true);
  assert.equal(r.isRunawayTripped("s2"), false);
});

// ── Severity escalation ───────────────────────────────────────────────────

test("checkRunawayRate: severity escalates to critical after 3 trips in 24h", () => {
  const r = newRouter();
  const today = Date.parse("2026-05-27T12:00:00Z");

  // Trip 1
  r.recordTurnCost("s1", 10.0, today);
  let result = r.checkRunawayRate("s1", today);
  assert.equal(result.severity, "warning");

  // Trip 2 (new session)
  r.recordTurnCost("s2", 10.0, today + 1000);
  result = r.checkRunawayRate("s2", today + 1000);
  assert.equal(result.severity, "warning");

  // Trip 3 — now at threshold (3 trips), should be critical.
  r.recordTurnCost("s3", 10.0, today + 2000);
  result = r.checkRunawayRate("s3", today + 2000);
  assert.equal(result.severity, "critical");
  assert.equal(result.tripsToday, 3);

  // Trip 4 — still critical.
  r.recordTurnCost("s4", 10.0, today + 3000);
  result = r.checkRunawayRate("s4", today + 3000);
  assert.equal(result.severity, "critical");
});

test("checkRunawayRate: trips today counter resets at UTC day boundary", () => {
  const r = newRouter();
  const day1 = Date.parse("2026-05-27T23:00:00Z");
  const day2 = Date.parse("2026-05-28T01:00:00Z");

  // 3 trips on day1
  for (let i = 1; i <= 3; i++) {
    r.recordTurnCost(`d1-s${i}`, 10.0, day1 + i);
    r.checkRunawayRate(`d1-s${i}`, day1 + i);
  }
  // First trip on day2 — should be warning (counter reset).
  r.recordTurnCost("d2-s1", 10.0, day2);
  const result = r.checkRunawayRate("d2-s1", day2);
  assert.equal(result.severity, "warning");
  assert.equal(result.tripsToday, 1);
});

// ── resolveModelOverride integration ──────────────────────────────────────

test("resolveModelOverride: forces tier3 when session tripped", () => {
  const r = newRouter();
  r.setSessionType("s1", "productive");  // would normally NOT override (tier2 default)
  r.recordTurnCost("s1", 10.0, 1000);
  r.checkRunawayRate("s1", 1000);  // trips
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

test("resolveModelOverride: trip beats user-tier choice (safety)", () => {
  const r = newRouter();
  r.setUserTier("s1", "power");  // user picked Power
  r.recordTurnCost("s1", 10.0, 1000);
  r.checkRunawayRate("s1", 1000);  // trips
  // Even though user picked Power, runaway trip forces tier3.
  // Runaway = something is broken, not user-engagement.
  assert.equal(r.resolveModelOverride("s1"), "grunt/model");
});

test("resolveModelOverride: untripped session resolves normally", () => {
  const r = newRouter();
  r.setUserTier("s1", "power");
  r.recordTurnCost("s1", 1.0, 1000);  // way under threshold
  r.checkRunawayRate("s1", 1000);     // not tripped
  // User's Power pick honored.
  assert.equal(r.resolveModelOverride("s1"), "power/model");
});

// ── Config gating ──────────────────────────────────────────────────────────

test("checkRunawayRate: disabled in config → never trips", () => {
  const r = newRouter({
    ...CFG,
    runawayRateCap: { enabled: false, dollarsPerWindow: 5.0, windowMinutes: 1 },
  });
  r.recordTurnCost("s1", 1000.0, 1000);
  const result = r.checkRunawayRate("s1", 1000);
  assert.equal(result.tripped, false);
  // History shouldn't even be recorded if disabled.
  assert.equal(result.totalUsd, 0);
});

test("checkRunawayRate: missing config block → uses defaults ($20/5min)", () => {
  const r = newRouter({ ...CFG, runawayRateCap: undefined });
  // $19 should be under default $20 threshold
  r.recordTurnCost("s1", 19.0, 1000);
  assert.equal(r.checkRunawayRate("s1", 1000).tripped, false);
  // $25 trips.
  r.recordTurnCost("s2", 25.0, 1000);
  assert.equal(r.checkRunawayRate("s2", 1000).tripped, true);
});

// ── recordTurnCost edge cases ────────────────────────────────────────────

test("recordTurnCost: ignores zero/negative cost", () => {
  const r = newRouter();
  r.recordTurnCost("s1", 0, 1000);
  r.recordTurnCost("s1", -5, 1000);
  assert.equal(r.checkRunawayRate("s1", 1000).totalUsd, 0);
});

test("recordTurnCost: ignores non-number cost", () => {
  const r = newRouter();
  // TypeScript would catch this; runtime guard for paranoid callers.
  r.recordTurnCost("s1", "5" /* @ts-ignore */, 1000);
  r.recordTurnCost("s1", NaN, 1000);
  assert.equal(r.checkRunawayRate("s1", 1000).totalUsd, 0);
});

test("recordTurnCost: empty sessionKey is a no-op", () => {
  const r = newRouter();
  r.recordTurnCost("", 100, 1000);
  // No session, no trip.
  assert.equal(r.isRunawayTripped(""), false);
});

// ── isSpendCapForced — composite of runaway + daily spend cap ────────────
// Added by code review: TurnObserver's `chosenBy` precedence ladder
// needs a single helper that mirrors resolveModelOverride's check order
// (runaway → daily cap → user → classifier). Without it, the labeler's
// Signal #1 (UI-chip override) attribution sees "classifier" on every
// span and Phase 4 calibration gets no ground-truth labels.

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "mr-cap-test-"));
}

test("isSpendCapForced: false by default", () => {
  const r = newRouter();
  assert.equal(r.isSpendCapForced("s1"), false);
});

test("isSpendCapForced: true after runaway-rate trip", () => {
  const r = newRouter();
  r.recordTurnCost("s1", 6.0, 1000);
  r.checkRunawayRate("s1", 1000); // triggers trip
  assert.equal(r.isSpendCapForced("s1"), true);
});

test("isSpendCapForced: true when daily spend-cap flag is active", () => {
  // Daily cap is read from `{sharedDir}/spend-caps/{botId}-{YYYY-MM-DD}.json`
  // with `{action: "downgrade-tier"}`. Drop one in a tmp shared dir.
  const sharedDir = tmpDir();
  const botId = "team_bot_a";
  const today = new Date();
  const y = today.getFullYear();
  const m = String(today.getMonth() + 1).padStart(2, "0");
  const d = String(today.getDate()).padStart(2, "0");
  const ymd = `${y}-${m}-${d}`;
  fs.mkdirSync(path.join(sharedDir, "spend-caps"), { recursive: true });
  fs.writeFileSync(
    path.join(sharedDir, "spend-caps", `${botId}-${ymd}.json`),
    JSON.stringify({ action: "downgrade-tier", cleared: false }),
  );

  const r = new ModelRouter(CFG, sharedDir, botId);
  assert.equal(r.isSpendCapForced("s1"), true);

  // After clearing, false again.
  fs.writeFileSync(
    path.join(sharedDir, "spend-caps", `${botId}-${ymd}.json`),
    JSON.stringify({ action: "downgrade-tier", cleared: true }),
  );
  assert.equal(r.isSpendCapForced("s1"), false);

  fs.rmSync(sharedDir, { recursive: true, force: true });
});

test("isSpendCapForced: scoped per session for runaway, pod-wide for daily", () => {
  // Runaway is sticky on the session that tripped — other sessions
  // unaffected. Daily cap is pod-wide and affects every session.
  const r = newRouter();
  r.recordTurnCost("s1", 6.0, 1000);
  r.checkRunawayRate("s1", 1000);
  assert.equal(r.isSpendCapForced("s1"), true);
  assert.equal(r.isSpendCapForced("s2"), false);
});
