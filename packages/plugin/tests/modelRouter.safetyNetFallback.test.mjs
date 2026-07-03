/**
 * Tests for the safety-net downgrade behavior when tier3 is empty.
 *
 * History:
 *   Pre-#1767: when tier3 was unconfigured or empty, the safety-net
 *   branches in _resolveModelAndTier returned null model → OC treated
 *   that as "no override, use bot default" → cost continued, telemetry
 *   lied about the breaker firing. The 2026-05-20 blackout pattern.
 *
 *   #1767: substituted a hardcoded "anthropic/claude-haiku-4-5"
 *   sentinel. Cost capped, but violated the "no hardcoded model names
 *   in code" principle.
 *
 *   This file (post-#1774-followup): returns an unresolvable sentinel
 *   "evolve/safety-net-blocked-fast-unconfigured". OC fails to
 *   resolve it → turn refused entirely → bot stops spending → loud
 *   gateway.log signal. The breaker honors its intent (stop cost)
 *   without lying about which model ran.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.safetyNetFallback.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const REFUSE_SENTINEL = "evolve/safety-net-blocked-fast-unconfigured";

function mkTmpDir(label) {
  const d = path.join(os.tmpdir(), `evolve-test-${label}-${Date.now()}-${Math.random()}`);
  fs.mkdirSync(d, { recursive: true });
  return d;
}

function configWithFullTier3() {
  return {
    tiers: {
      tier1: { models: ["anthropic/claude-opus-4-7"] },
      tier2: { models: ["anthropic/claude-sonnet-4-6"] },
      tier3: { models: ["anthropic/claude-haiku-4-5"] },
    },
    routing: { enabled: true },
    runawayRateCap: { enabled: true, dollarsPerWindow: 20, windowMinutes: 5 },
  };
}

function configWithEmptyTier3() {
  return {
    tiers: {
      tier1: { models: ["anthropic/claude-opus-4-7"] },
      tier2: { models: ["anthropic/claude-sonnet-4-6"] },
      // tier3 deliberately missing — the bug scenario.
    },
    routing: { enabled: true },
    runawayRateCap: { enabled: true, dollarsPerWindow: 20, windowMinutes: 5 },
  };
}

// Trigger a runaway-rate trip by feeding cost history above the cap.
function tripRunaway(router, sessionKey, dollars = 25) {
  // The router exposes recordTurnCost (private but importable in the
  // dist JS). We use the public hook surface: simulate via the spend-cap
  // flag file mechanism since that's externally observable. For runaway
  // specifically we'd need cost history — instead test spend_cap which
  // is the cleaner external trigger.
  // (Runaway internals tested separately in modelRouter.runawayRate.test.mjs;
  // here we focus on the fallback behavior for both breaker paths.)
}

function writeSpendCapFlag(sharedDir, botId) {
  const d = new Date();
  const ymd = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const dir = path.join(sharedDir, "spend-caps");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, `${botId}-${ymd}.json`),
    JSON.stringify({ action: "downgrade-tier", cleared: false }),
  );
}

// ── Spend-cap → returns configured tier3 model when present ────────────────

test("spend-cap downgrade uses configured tier3 model when present", () => {
  const sharedDir = mkTmpDir("spendcap-tier3-present");
  const botId = "team_bot_a";
  writeSpendCapFlag(sharedDir, botId);

  const router = new ModelRouter(configWithFullTier3(), sharedDir, botId);
  router.setSessionType("s1", "productive");
  const model = router.resolveModelOverride("s1");
  assert.equal(model, "anthropic/claude-haiku-4-5");
});

// ── Spend-cap → falls back to sentinel when tier3 is empty (THE FIX) ───────

test("REGRESSION L5: spend-cap returns refuse-sentinel when tier3 is empty", () => {
  // Pre-#1767 this returned null → OC used bot default → cost continued.
  // #1767 substituted hardcoded haiku → cost capped, but hardcoded.
  // Now: returns an unresolvable sentinel → OC fails turn → bot stops.
  const sharedDir = mkTmpDir("spendcap-tier3-empty");
  const botId = "team_bot_a";
  writeSpendCapFlag(sharedDir, botId);

  const router = new ModelRouter(configWithEmptyTier3(), sharedDir, botId);
  router.setSessionType("s1", "productive");
  const model = router.resolveModelOverride("s1");
  assert.equal(
    model, REFUSE_SENTINEL,
    "spend-cap breaker MUST return the unresolvable refuse-sentinel " +
    "when tier3 is empty — OC will fail to resolve it and refuse the " +
    "turn, which is the correct breaker behavior (stop cost). " +
    "Hardcoding a real model name here violates the no-hardcoded-models " +
    "principle; returning null lies about whether the breaker fired.",
  );
  // The sentinel must NOT look like a real model anyone might have keyed.
  // "evolve/..." is reserved — no LLM provider uses this prefix.
  assert.ok(
    model.startsWith("evolve/"),
    `refuse-sentinel must use the evolve/ prefix; got ${JSON.stringify(model)}`,
  );
});

// ── Spend-cap → driver still reports spend_cap (telemetry honesty) ─────────

test("spend-cap driver is stamped even when refusing the turn", () => {
  const sharedDir = mkTmpDir("spendcap-driver");
  const botId = "team_bot_a";
  writeSpendCapFlag(sharedDir, botId);

  const router = new ModelRouter(configWithEmptyTier3(), sharedDir, botId);
  router.setSessionType("s1", "productive");
  router.resolveModelOverride("s1");
  const driver = router.getLastDecisionDriver("s1");
  assert.equal(
    driver, "spend_cap",
    "Driver attribution must remain accurate when the safety net refuses " +
    "the turn — telemetry would otherwise underreport breaker activity. " +
    "Audits should be able to distinguish 'breaker fired successfully " +
    "(downgrade to tier3)' from 'breaker fired but refused (no tier3)'.",
  );
});

// ── Startup validation: warn when safety nets are wired but tier3 empty ────

test("constructor warns when safety nets are wired and tier3 is empty", () => {
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const sharedDir = mkTmpDir("startup-warn");
    new ModelRouter(configWithEmptyTier3(), sharedDir, "team_bot_a");
  } finally {
    console.warn = originalWarn;
  }
  assert.ok(
    warnings.some(w => w.includes("'fast' role has no models")),
    `Expected a startup warning about tier3 being empty; got: ${JSON.stringify(warnings)}`
  );
  assert.ok(
    warnings.some(w => w.includes(REFUSE_SENTINEL)),
    `Expected startup warning to mention the refuse-sentinel; got: ${JSON.stringify(warnings)}`
  );
  assert.ok(
    warnings.some(w => w.toUpperCase().includes("REFUSE") || w.includes("refuse")),
    `Expected startup warning to clearly say it will REFUSE turns; got: ${JSON.stringify(warnings)}`
  );
});

test("constructor does NOT warn when tier3 has models configured", () => {
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const sharedDir = mkTmpDir("startup-no-warn");
    new ModelRouter(configWithFullTier3(), sharedDir, "team_bot_a");
  } finally {
    console.warn = originalWarn;
  }
  assert.equal(
    warnings.filter(w => w.includes("'fast' role has no models")).length,
    0,
    `Did not expect a startup warning when tier3 is configured; got: ${JSON.stringify(warnings)}`
  );
});

test("constructor does not warn when safety nets are disabled even if tier3 is empty", () => {
  // A bot that explicitly disables runaway and has no sharedDir/botId
  // wired for spend-cap has no working breaker anyway — no need to warn.
  const cfg = {
    tiers: { tier1: { models: ["x"] }, tier2: { models: ["y"] } }, // no tier3
    routing: { enabled: true },
    runawayRateCap: { enabled: false },
  };
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    new ModelRouter(cfg, "", "");  // no sharedDir, no botId
  } finally {
    console.warn = originalWarn;
  }
  assert.equal(
    warnings.filter(w => w.includes("'fast' role has no models")).length,
    0,
    `Did not expect warning when safety nets aren't wired; got: ${JSON.stringify(warnings)}`
  );
});

// ── One-shot warn on first refusal ─────────────────────────────────────────

test("logs once when refuse-sentinel fires (subsequent refusals are silent)", () => {
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args.join(" "));
  let router;
  try {
    const sharedDir = mkTmpDir("oneshot-warn");
    const botId = "team_bot_a";
    writeSpendCapFlag(sharedDir, botId);
    router = new ModelRouter(configWithEmptyTier3(), sharedDir, botId);
    // Clear startup warns to focus on the fire-time warn
    warnings.length = 0;
    router.setSessionType("s1", "productive");
    router.resolveModelOverride("s1");
    router.setSessionType("s2", "productive");
    router.resolveModelOverride("s2");
    router.setSessionType("s3", "productive");
    router.resolveModelOverride("s3");
  } finally {
    console.warn = originalWarn;
  }
  const refusalWarns = warnings.filter(w => w.includes("safety-net fired"));
  assert.equal(
    refusalWarns.length, 1,
    `Expected exactly one refusal warning (one-shot); got ${refusalWarns.length}: ${JSON.stringify(refusalWarns)}`
  );
});
