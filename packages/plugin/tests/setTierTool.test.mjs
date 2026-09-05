/**
 * Tests for SetTierTool — `session.set_tier` MCP tool (Phase 2).
 *
 * Validates: input validation, choice→ModelRouter mapping, consent_source
 * classification, "auto" clears override, missing session context handling.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/setTierTool.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { createSetTierToolFactory } from "../dist/tools/SetTierTool.js";
import { ModelRouter } from "../dist/observer/ModelRouter.js";

const CFG = {
  rungs: [
    { id: "haiku-class", models: ["grunt/model"], costClass: "low" },
    { id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" },
    { id: "opus-class", models: ["power/model"], costClass: "high" },
  ],
  roles: {
    fast: "haiku-class",
    standard: "sonnet-class",
    power: "opus-class",
  },
  routing: { enabled: true },
};

function fakeLogger() {
  const records = { debug: [], info: [], warn: [], error: [] };
  return {
    debug: (m) => records.debug.push(m),
    info: (m) => records.info.push(m),
    warn: (m) => records.warn.push(m),
    error: (m) => records.error.push(m),
    records,
  };
}

function buildTool(options = {}) {
  const router = options.router ?? new ModelRouter(CFG, "", "");
  const logger = options.logger ?? fakeLogger();
  const factory = createSetTierToolFactory(
    {
      botId: options.botId ?? "team_bot_a",
      modelRouter: router,
      hasRecentAskHint: options.hasRecentAskHint,
    },
    logger,
  );
  // Explicit-undefined check: only default to "sess-1" if the option
  // key is absent (not when caller explicitly passed undefined).
  const ctx = {};
  if ("sessionKey" in options) {
    if (options.sessionKey !== undefined) ctx.sessionKey = options.sessionKey;
  } else {
    ctx.sessionKey = "sess-1";
  }
  if ("sessionId" in options) {
    if (options.sessionId !== undefined) ctx.sessionId = options.sessionId;
  }
  const tool = factory(ctx);
  return { tool, router, logger };
}

function parseResult(toolReturn) {
  if (toolReturn.isError) {
    return { isError: true, text: toolReturn.content[0].text };
  }
  return { isError: false, ...JSON.parse(toolReturn.content[0].text) };
}

// ── Schema metadata ──────────────────────────────────────────────────────────

test("tool exposes name + parameters schema", () => {
  const { tool } = buildTool();
  // Anthropic's Messages API enforces ^[a-zA-Z0-9_-]{1,128}$ on tool
  // names — the dot in the previous "session.set_tier" form caused
  // every Anthropic-routed turn to fail with a regex error after
  // PR #1742 surfaced this tool to the model. Renamed 2026-05-28.
  assert.equal(tool.name, "session_set_tier");
  assert.match(tool.name, /^[a-zA-Z0-9_-]{1,128}$/);
  assert.ok(typeof tool.description === "string");
  assert.ok(tool.parameters);  // TypeBox schema
});

// ── Input validation ────────────────────────────────────────────────────────

test("rejects unknown choice", async () => {
  const { tool } = buildTool();
  const result = parseResult(await tool.execute("call-1", { choice: "opus" }));
  assert.equal(result.isError, true);
  assert.ok(result.text.includes("unknown choice"));
});

test("rejects empty/null params", async () => {
  const { tool } = buildTool();
  const result = parseResult(await tool.execute("call-1", null));
  assert.equal(result.isError, true);
});

test("rejects when no session context", async () => {
  const { tool } = buildTool({ sessionKey: undefined, sessionId: undefined });
  const result = parseResult(await tool.execute("call-1", { choice: "power" }));
  assert.equal(result.isError, true);
  assert.ok(result.text.includes("session context"));
});

test("case-insensitive choice (POWER works)", async () => {
  const { tool, router } = buildTool();
  const result = parseResult(await tool.execute("call-1", { choice: "POWER" }));
  assert.equal(result.isError, false);
  assert.equal(result.applied_choice, "power");
  // Verify it actually applied to ModelRouter (resolveModelOverride returns the tier1 model).
  assert.equal(router.resolveModelOverride("sess-1"), "power/model");
});

// ── Each choice value applies correctly ─────────────────────────────────────

test("choice=fast → tier3 model resolved", async () => {
  const { tool, router } = buildTool();
  await tool.execute("call-1", { choice: "fast" });
  assert.equal(router.resolveModelOverride("sess-1"), "grunt/model");
});

test("choice=standard → tier2 model resolved", async () => {
  const { tool, router } = buildTool();
  await tool.execute("call-1", { choice: "standard" });
  assert.equal(router.resolveModelOverride("sess-1"), "workhorse/model");
});

test("choice=power → tier1 model resolved", async () => {
  const { tool, router } = buildTool();
  await tool.execute("call-1", { choice: "power" });
  assert.equal(router.resolveModelOverride("sess-1"), "power/model");
});

test("choice=auto → clears override (null returned)", async () => {
  const { tool, router } = buildTool();
  // First set a tier
  await tool.execute("call-1", { choice: "power" });
  assert.equal(router.resolveModelOverride("sess-1"), "power/model");
  // Then clear it
  const result = parseResult(await tool.execute("call-2", { choice: "auto" }));
  assert.equal(result.isError, false);
  assert.equal(result.applied_choice, "auto");
  // resolveModelOverride returns null (no user override) — classifier-based
  // routing kicks back in (no session class set → returns null).
  assert.equal(router.resolveModelOverride("sess-1"), null);
});

// ── Consent-source classification ───────────────────────────────────────────

test("consent_source defaults to bot_initiated (no ask-hint hook wired)", async () => {
  const { tool, router } = buildTool();
  const result = parseResult(await tool.execute("call-1", { choice: "power" }));
  assert.equal(result.consent_source, "bot_initiated");
  assert.equal(router.getConsentSource("sess-1"), "bot_initiated");
});

test("consent_source = ask_hint_agreed when hook returns true", async () => {
  const askHintSeen = new Set(["sess-1"]);
  const { tool, router } = buildTool({
    hasRecentAskHint: (k) => askHintSeen.has(k),
  });
  const result = parseResult(await tool.execute("call-1", { choice: "power" }));
  assert.equal(result.consent_source, "ask_hint_agreed");
  assert.equal(router.getConsentSource("sess-1"), "ask_hint_agreed");
});

test("consent_source = bot_initiated when hook returns false for THIS session", async () => {
  // Hook exists but returns false for sess-1 specifically.
  const askHintSeen = new Set(["other-session"]);
  const { tool, router } = buildTool({
    hasRecentAskHint: (k) => askHintSeen.has(k),
  });
  const result = parseResult(await tool.execute("call-1", { choice: "power" }));
  assert.equal(result.consent_source, "bot_initiated");
});

// ── Reason field ────────────────────────────────────────────────────────────

test("optional reason field is logged", async () => {
  const { tool, logger } = buildTool();
  await tool.execute("call-1", {
    choice: "power",
    reason: "user said this needs deep analysis",
  });
  const lastInfo = logger.records.info[logger.records.info.length - 1];
  assert.ok(lastInfo.includes("reason="));
  assert.ok(lastInfo.includes("deep analysis"));
});

test("missing reason works (it's optional)", async () => {
  const { tool } = buildTool();
  const result = parseResult(await tool.execute("call-1", { choice: "fast" }));
  assert.equal(result.isError, false);
});

// ── Integration with ModelRouter precedence ─────────────────────────────────

test("set_tier choice still loses to runaway-rate trip (safety)", async () => {
  // After runaway trips, tier1 user choice should NOT override tier3 force.
  const { tool, router } = buildTool();
  router.recordTurnCost("sess-1", 25.0, 1000);  // way over default $20
  router.checkRunawayRate("sess-1", 1000);  // trips
  await tool.execute("call-1", { choice: "power" });
  // Even though we asked for power, runaway forces tier3.
  assert.equal(router.resolveModelOverride("sess-1"), "grunt/model");
});

// ── Round-trip: tool → ModelRouter → getConsentSource ──────────────────────

test("setting then clearing via auto removes consent_source too", async () => {
  const { tool, router } = buildTool();
  await tool.execute("call-1", { choice: "power" });
  assert.equal(router.getConsentSource("sess-1"), "bot_initiated");
  await tool.execute("call-2", { choice: "auto" });
  assert.equal(router.getConsentSource("sess-1"), null);
});

// ── Tier1 cost gate (L6-P1) ─────────────────────────────────────────────────
// Mirrors the gate-isolated tests in modelRouter.tier1CostGate.test.mjs;
// these exercise the end-to-end "bot calls tool, gate blocks, tool
// downgrades 'power' → 'standard'" flow that home_chat_routes.py already
// performs for the chip path.

function buildToolWithOverride(override) {
  const cfg = { ...CFG, userTierOverride: override };
  return buildTool({ router: new ModelRouter(cfg, "", "") });
}

test("L6-P1: choice=power when userTierOverride.enabled=false → downgraded to standard", async () => {
  const { tool, router } = buildToolWithOverride({ enabled: false });
  const result = parseResult(await tool.execute("call-1", { choice: "power" }));
  assert.equal(result.ok, true, "tool reports ok=true (call succeeded, just downgraded)");
  assert.equal(result.applied_choice, "standard");
  assert.equal(result.requested_choice, "power");
  assert.equal(result.tier1_blocked_reason, "feature_disabled");
  // ModelRouter received "standard", not "power"
  assert.equal(router.resolveModelOverride("sess-1"), "workhorse/model");
});

test("L6-P1: choice=power when allowBotInitiated=false → downgraded", async () => {
  const { tool, router } = buildToolWithOverride({
    enabled: true, allowBotInitiated: false,
  });
  const result = parseResult(await tool.execute("call-1", { choice: "power" }));
  assert.equal(result.applied_choice, "standard");
  assert.equal(result.tier1_blocked_reason, "bot_initiated_disabled");
  assert.ok(
    /chip/i.test(result.tier1_blocked_detail),
    "detail should explain the chip path still works",
  );
  assert.equal(router.resolveModelOverride("sess-1"), "workhorse/model");
});

test("L6-P1: choice=power when daily cap exhausted → downgraded", async () => {
  const { tool, router } = buildToolWithOverride({ enabled: true, dailyCap: 1 });
  // First call: succeeds at tier1
  let result = parseResult(await tool.execute("call-1", { choice: "power" }));
  assert.equal(result.applied_choice, "power", "first power call within cap");
  assert.equal(router.resolveModelOverride("sess-1"), "power/model");
  // Second call (new session): hits cap → downgraded
  const factory2 = createSetTierToolFactory(
    { botId: "team_bot_a", modelRouter: router },
    fakeLogger(),
  );
  const tool2 = factory2({ sessionKey: "sess-2" });
  result = parseResult(await tool2.execute("call-2", { choice: "power" }));
  assert.equal(result.applied_choice, "standard");
  assert.equal(result.tier1_blocked_reason, "daily_cap_exhausted");
  assert.ok(
    /1\/1/.test(result.tier1_blocked_detail),
    `cap detail should show the count/cap ratio; got: ${result.tier1_blocked_detail}`
  );
});

test("L6-P1: gate does NOT block choice=standard or choice=fast", async () => {
  const { tool, router } = buildToolWithOverride({ enabled: false });
  // Even with the kill-switch on, non-power choices pass through.
  const fast = parseResult(await tool.execute("call-1", { choice: "fast" }));
  assert.equal(fast.applied_choice, "fast");
  assert.equal(fast.tier1_blocked_reason, undefined);
  // ModelRouter accepted "fast" — would route to tier3.
  assert.equal(router.resolveModelOverride("sess-1"), "grunt/model");
});

test("L6-P1: blocked tier1 call still records consent_source on the actual choice", async () => {
  // When power → standard downgrade fires, the operator/cascade audit
  // layer still needs to know SOMETHING happened. We record
  // consent_source = bot_initiated against the standard tier the
  // session actually landed on, not the blocked power request.
  const { tool, router } = buildToolWithOverride({ enabled: true, dailyCap: 0 });
  await tool.execute("call-1", { choice: "power" });
  assert.equal(
    router.getConsentSource("sess-1"),
    "bot_initiated",
    "consent source recorded for the downgraded choice",
  );
});

test("L6-P1: blocked tier1 attempt is logged with WARN", async () => {
  const logger = fakeLogger();
  const cfg = { ...CFG, userTierOverride: { enabled: false } };
  const router = new ModelRouter(cfg, "", "");
  const factory = createSetTierToolFactory(
    { botId: "team_bot_a", modelRouter: router }, logger,
  );
  const tool = factory({ sessionKey: "sess-1" });
  await tool.execute("call-1", { choice: "power" });
  const warned = logger.records.warn.some(
    m => /tier1.*BLOCKED|feature_disabled|downgrad/i.test(m)
  );
  assert.ok(
    warned,
    `expected WARN log about the blocked tier1 attempt; got warn=${JSON.stringify(logger.records.warn)}`
  );
});
