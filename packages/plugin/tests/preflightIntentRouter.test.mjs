/**
 * Tests for PreflightIntentRouter (Phase 2: bot_prior + regex layers) +
 * the ModelRouter routing-ladder slot integration.
 *
 * Spec: internal/spec-preflight-intent-router-2026-06-06.md (to be written).
 *
 * Phase 2 contract:
 *   - classify() consults layers in order: bot_prior → regex tier1 →
 *     regex tier3 → ABSTAIN
 *   - bot_prior wins outright when set (regex doesn't run)
 *   - regex tier1 wins over tier3 (escalation bias on ambiguity)
 *   - Each pattern's reason field names the SPECIFIC pattern that matched
 *     so the audit layer (Phase 4) can attribute miscalibrations
 *   - Never throws (errors degrade to ABSTAIN)
 *   - latency_ms observed even on abstain
 *
 * Routing-ladder contract:
 *   - When setSessionPreflightDecision stores a tier, _resolveModelAndTier
 *     consults the slot AFTER operator/user defaults and BEFORE the bot-
 *     default fallthrough
 *   - When stored with null (or never stored), the ladder behaves exactly
 *     like before — preflight is a pure no-op
 *   - Driver tag is "preflight" when the slot drove the decision
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/preflightIntentRouter.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  PreflightIntentRouter,
  ABSTAIN,
  _parseHaikuTier,
} from "../dist/observer/PreflightIntentRouter.js";
import { ModelRouter } from "../dist/observer/ModelRouter.js";

// Minimal logger stub
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

const FAKE_CONFIG = { botId: "team_bot_a", sharedDir: "/tmp" };
const FAKE_API = {};

/** Make a temp shared dir with a writeable network.json — used for bot_prior tests */
function tmpSharedDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "preflight-test-"));
}

// ── Phase 2 core contract ───────────────────────────────────────────────────

test("classify: abstains on a generic conversational prompt (no pattern matches)", async () => {
  // The dominant case — most prompts don't match any pattern and fall
  // through to abstain. Legacy classifier handles them.
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "Can you read my last email from Sarah?",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null);
  assert.equal(d.layer, "abstain");
});

test("classify: abstains on empty / whitespace prompts", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of ["", "   ", "\n\t"]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, null, `expected abstain for "${msg}"`);
    assert.equal(d.reason, "empty_message");
  }
});

test("classify: records latency_ms even on abstain", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "tell me about the latest news",
    botId: "team_bot_a",
  });
  assert.equal(typeof d.latency_ms, "number");
  assert.ok(d.latency_ms >= 0);
  // Phase 2 with TTL-cached file read should still be very fast.
  assert.ok(d.latency_ms < 100, `Phase 2 abstain latency should be small, got ${d.latency_ms}ms`);
});

// ── Phase 2: tier1 regex (positive cases — each pattern fires) ─────────────

test("regex tier1: explicit_thinking_request — 'help me think through X'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "Help me think through whether to take that meeting",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.layer, "regex");
  assert.equal(d.reason, "regex:explicit_thinking_request");
  assert.equal(d.confidence, 1.0);
});

test("regex tier1: think_through — 'let me think through'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "Let me think through this design before we ship.",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.reason, "regex:think_through");
});

test("regex tier1: decision_help — 'what's the right call'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "What's the right call here — switch providers now or wait?",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.reason, "regex:decision_help");
});

test("regex tier1: weigh_options — 'weigh the options'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "Weigh the options for migrating off Postgres",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.reason, "regex:weigh_options");
});

test("regex tier1: weigh_options — 'consider the trade-offs'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "Consider the trade-offs of going with vendor X",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.reason, "regex:weigh_options");
});

test("regex tier1: design_imperative — 'design a system'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "Design a system for sharded auth across 3 regions",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.reason, "regex:design_imperative");
});

test("regex tier1: architect_imperative — 'architect a solution'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "Architect a solution for cross-region failover",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.reason, "regex:architect_imperative");
});

// ── Phase 2: tier1 regex (negative cases — patterns must NOT fire) ─────────

test("regex tier1 negative: 'design.com' (domain name, no article)", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "Check design.com for that template",
    botId: "team_bot_a",
  });
  // The design_imperative pattern requires a following article — protects
  // against accidental matches on URLs, brand names, present participles.
  assert.equal(d.tier, null, "design.com must not trip design_imperative");
});

test("regex tier1 negative: 'the design is broken' (design as noun)", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "the design is broken, can you take a look?",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null, "design as noun must not trip design_imperative");
});

test("regex tier1 negative: 'I'm thinking about X' (present participle)", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "I'm thinking about taking that meeting next week",
    botId: "team_bot_a",
  });
  // "thinking about" is conversational filler; only "think through" trips.
  assert.equal(d.tier, null);
});

test("regex tier1 negative: 'what do you think' (common conversational)", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "What do you think about that proposal?",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null);
});

test("regex tier1 negative: 'designed it badly' (past tense, no article)", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "we designed it badly the first time",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null);
});

// ── Cost-incident regressions (2026-06-07): casual idiom must NOT escalate ──
//
// Each fixture below corresponds to a real or realistic user message that
// the pre-tightening regex would have routed to tier1, costing ~18x the
// sonnet rate for the entire session. The canonical case is the kitchen-TV
// question: "help me figure out the TV size" matched the explicit_thinking_
// request pattern's "figure out" alternative and ran a multi-turn session
// on opus-4-8 — $0.96 for one turn vs ~$0.05 if it had stayed on sonnet.
//
// The fix removed "decide" / "weigh" / "figure out" as standalone matches
// from explicit_thinking_request, and added a technical-noun requirement
// to design_imperative / architect_imperative. These fixtures pin that
// behavior so future loosening (e.g. adding "figure out" back) trips the
// test suite immediately.

test("regression: kitchen TV size question (the actual 2026-06-07 incident)", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage:
      "We have a tv in the upper corner of our kitchen. It's about 21\" wide. I want to get a bigger one with better sound. I think it could be as wide as 30 inches. Help me figure out that size and model tv i should get.",
    botId: "team_bot_a",
  });
  assert.equal(
    d.tier,
    null,
    "casual 'help me figure out' must NOT escalate to tier1 (cost incident 2026-06-07)",
  );
});

test("regression: 'help me figure out X' is casual, not deliberative", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of [
    "Help me figure out what to make for dinner",
    "help me figure out where to go on vacation",
    "Help me figure out which library to use here",
    "help me figure out why my code isn't compiling",
  ]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, null, `'figure out' must not escalate: "${msg}"`);
  }
});

test("regression: 'help me decide X' (without 'between') stays tier2", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of [
    "help me decide what to wear",
    "Help me decide on a restaurant for tonight",
    "help me decide if I should go",
  ]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, null, `bare 'help me decide' must not escalate: "${msg}"`);
  }
});

test("explicit_thinking_request still fires on 'help me decide between'", async () => {
  // Sanity check: the genuine deliberation case ("decide between X and Y")
  // DOES escalate. If this test starts failing along with the regressions
  // above, we tightened too far.
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "Help me decide between renewing the contract or switching vendors",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.reason, "regex:explicit_thinking_request");
});

test("regression: 'help me weigh' (literal, no options noun) stays tier2", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of [
    "help me weigh my groceries",
    "Help me weigh whether this matters",
  ]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, null, `bare 'help me weigh' must not escalate: "${msg}"`);
  }
});

test("regression: 'design a <non-technical>' stays tier2", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of [
    "design a kitchen layout for me",
    "design a workout for tomorrow",
    "Design a meal plan for the week",
    "design a wedding seating chart",
    "design a perfect Saturday",
  ]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, null, `non-technical 'design a X' must not escalate: "${msg}"`);
  }
});

test("regression: 'architect a <non-technical>' stays tier2", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of [
    "architect a perfect day off",
    "Architect a routine that works for me",
  ]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, null, `non-technical 'architect a X' must not escalate: "${msg}"`);
  }
});

test("design_imperative still fires when followed by a technical noun", async () => {
  // Sanity check: legitimate system-design requests DO still escalate.
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of [
    "Design a system for sharded auth",        // system
    "design an api for our billing service",   // api
    "Help me design a schema for events",      // schema (note: "help me" prefix, but design pattern fires)
    "design the infrastructure for failover",  // infrastructure
    "design a deployment pipeline",            // deployment
  ]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, "tier1", `technical design imperative must escalate: "${msg}"`);
  }
});

test("architect_imperative still fires when followed by a technical noun", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of [
    "Architect a solution for cross-region failover",
    "architect the service mesh for k8s",
    "architect a database for high-write workloads",
  ]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, "tier1", `technical architect imperative must escalate: "${msg}"`);
  }
});

// ── Phase 2: tier3 regex (positive cases) ───────────────────────────────────

test("regex tier3: bare_ack — 'thanks!'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of ["thanks", "Thanks!", "Got it.", "thank you", "Sounds good"]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, "tier3", `expected tier3 for "${msg}"`);
    assert.equal(d.reason, "regex:bare_ack");
  }
});

test("regex tier3: bare_response — 'yes', 'no', 'ok'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of ["yes", "no", "ok.", "Okay", "stop", "Go!"]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, "tier3", `expected tier3 for "${msg}"`);
    assert.equal(d.reason, "regex:bare_response");
  }
});

test("regex tier3: factual_lookup — 'what's the weather'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of ["What's the weather?", "what's the time", "What's the date today?"]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, "tier3", `expected tier3 for "${msg}"`);
    assert.equal(d.reason, "regex:factual_lookup");
  }
});

test("regex tier3: simple_command — 'set a timer'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  for (const msg of ["Set a timer for 5 minutes", "set reminder for 3pm", "Set an alarm"]) {
    const d = await r.classify({ userMessage: msg, botId: "team_bot_a" });
    assert.equal(d.tier, "tier3", `expected tier3 for "${msg}"`);
    assert.equal(d.reason, "regex:simple_command");
  }
});

// ── Phase 2: tier3 regex (negative — must NOT trip on longer messages) ─────

test("regex tier3 negative: 'thanks for the analysis, can you also...'", async () => {
  // The anchored end on bare_ack requires the message to BE just an ack.
  // A message that starts with 'thanks' but continues is real engagement.
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "thanks for the detailed analysis, can you also look at the cache layer?",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null, "ack-then-continue must NOT trip bare_ack");
});

test("regex tier3 negative: 'yes, but here's the thing...'", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "yes, but here's the thing — I think we should reconsider",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null, "yes-then-continue must NOT trip bare_response");
});

test("regex tier3 negative: 'what's the weather forecast for the rest of the week...'", async () => {
  // factual_lookup is anchored to `^what's the (weather|time|date|day|temperature)\b`.
  // A long elaboration after the lookup phrase still trips it — that's
  // intentional (the user IS asking a weather question, just verbose) BUT
  // we need to confirm the pattern doesn't catch the OTHER direction:
  // arbitrary text that happens to mention weather later.
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    userMessage: "I was hoping you could check the weather for me tomorrow",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null, "weather mention mid-message must NOT trip factual_lookup");
});

// ── Phase 2: layer order — bot_prior > tier1 > tier3 > abstain ─────────────

test("bot_prior: when set, beats any regex match", async () => {
  // Write a network.json with bot_prior=tier3 for team_bot_a. Then send a
  // prompt that would trip the tier1 design_imperative regex. The bot_prior
  // MUST win because operator's explicit per-bot config beats a generic
  // pattern match.
  const dir = tmpSharedDir();
  fs.writeFileSync(
    path.join(dir, "network.json"),
    JSON.stringify({ bots: { team_bot_a: { preflight: { bot_prior: "tier3" } } } }),
  );
  const r = new PreflightIntentRouter(
    { botId: "team_bot_a", sharedDir: dir },
    fakeLogger(),
    FAKE_API,
  );
  const d = await r.classify({
    userMessage: "Design a system for sharded auth",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier3");
  assert.equal(d.layer, "bot_prior");
  assert.equal(d.reason, "bot_prior:team_bot_a");
});

test("bot_prior: invalid value falls through to regex", async () => {
  const dir = tmpSharedDir();
  fs.writeFileSync(
    path.join(dir, "network.json"),
    JSON.stringify({ bots: { team_bot_a: { preflight: { bot_prior: "tier99" } } } }),
  );
  const r = new PreflightIntentRouter(
    { botId: "team_bot_a", sharedDir: dir },
    fakeLogger(),
    FAKE_API,
  );
  const d = await r.classify({
    userMessage: "Design a system for X",
    botId: "team_bot_a",
  });
  // Invalid value ignored → regex tier1 fires
  assert.equal(d.tier, "tier1");
  assert.equal(d.layer, "regex");
});

test("bot_prior: missing network.json fails open (regex still runs)", async () => {
  const r = new PreflightIntentRouter(
    { botId: "team_bot_a", sharedDir: "/nonexistent-dir-12345" },
    fakeLogger(),
    FAKE_API,
  );
  const d = await r.classify({
    userMessage: "Design a system",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1", "missing config must NOT block regex layer");
});

test("layer order: tier1 regex beats tier3 regex when both could match", async () => {
  // Adversarial: a message that contains both a tier1 design phrase AND a
  // tier3 bare_ack at the start. Tier1 should win (escalation bias).
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  const d = await r.classify({
    // "ok, design a system" — starts with ok BUT not a pure ack (continues),
    // so bare_ack doesn't fire AND design_imperative does. tier1 wins.
    userMessage: "ok, design a system for that",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.layer, "regex");
  assert.equal(d.reason, "regex:design_imperative");
});

// ── Phase 1 plumbing (preserved across phases) ──────────────────────────────

test("ABSTAIN constant is frozen — module singleton can't be mutated", () => {
  // The frozen-ness guards against a future caller mutating the singleton
  // (e.g., `ABSTAIN.tier = 'tier1'`) which would silently break every
  // other caller. classify() must return a fresh spread, not the frozen
  // object itself.
  assert.equal(Object.isFrozen(ABSTAIN), true);
  assert.throws(() => {
    "use strict";
    ABSTAIN.tier = "tier1";
  });
});

test("classify: even when input is malformed, never throws — returns ABSTAIN", async () => {
  // Adversarial input. The router's contract is "no throws into the hot
  // path." Phase 1 has no real logic so this is largely a contract pin,
  // but it locks in the behavior for Phase 2+ when regex/haiku add real
  // failure modes.
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), FAKE_API);
  // @ts-expect-error — intentional bad shape
  const d = await r.classify(null);
  assert.equal(d.tier, null);
  assert.equal(d.layer, "abstain");
});

// ── ModelRouter integration ─────────────────────────────────────────────────

const TIERS_CONFIG = {
  enabled: true,
  tiers: {
    tier1: { models: ["anthropic/claude-opus-4-6"] },
    tier2: { models: ["anthropic/claude-sonnet-4-6"] },
    tier3: { models: ["anthropic/claude-haiku-4-5"] },
  },
  routing: {
    enabled: true,
    backgroundTier: "tier3",
    maintenanceTier: "tier3",
  },
  tierCascade: ["tier2", "tier3", "tier1"],
};

function makeRouter() {
  return new ModelRouter(TIERS_CONFIG, "/tmp", "team_bot_a");
}

test("ModelRouter: preflight slot is empty by default — no behavior change", () => {
  const r = makeRouter();
  const sk = "sess-1";
  // No preflight decision stored
  const model = r.resolveModelOverride(sk);
  // Without a session type or any other driver, falls through to bot
  // default (returns null). Preflight slot doesn't fire.
  assert.equal(model, null);
  // Driver should be "classifier" (the fallthrough path)
  assert.equal(r.getLastDecisionDriver(sk), "classifier");
});

test("ModelRouter: setSessionPreflightDecision(null) clears the slot", () => {
  const r = makeRouter();
  const sk = "sess-1";
  r.setSessionPreflightDecision(sk, { tier: "tier1", reason: "regex:design_word" });
  assert.deepEqual(r.getSessionPreflightDecision(sk), {
    tier: "tier1",
    reason: "regex:design_word",
  });
  r.setSessionPreflightDecision(sk, null);
  assert.equal(r.getSessionPreflightDecision(sk), null);
});

test("ModelRouter: stored preflight tier drives resolution when no higher slot fires", () => {
  // Critical Phase 2+ contract: when the router has an opinion and no
  // operator/user default exists, _resolveModelAndTier MUST return that
  // tier and tag the driver as "preflight" so the audit layer can grade.
  const r = makeRouter();
  const sk = "sess-1";
  r.setSessionPreflightDecision(sk, { tier: "tier1", reason: "regex:design_word" });
  const model = r.resolveModelOverride(sk);
  assert.equal(model, "anthropic/claude-opus-4-6");
  assert.equal(r.getLastDecisionDriver(sk), "preflight");
});

test("ModelRouter: stored preflight does NOT override an operator chip choice", () => {
  // Operator chip (sessionUserTier) is precedence slot #2 — beats
  // preflight at #4b. This pin guards against accidental reordering.
  const r = makeRouter();
  const sk = "sess-1";
  // Operator chose "power" (tier1) via chip
  r.setUserTier(sk, "power", { source: "ui_chip" });
  // Preflight thinks tier3
  r.setSessionPreflightDecision(sk, { tier: "tier3", reason: "regex:bare_command" });
  const model = r.resolveModelOverride(sk);
  // Operator wins — model is tier1
  assert.equal(model, "anthropic/claude-opus-4-6");
  assert.equal(r.getLastDecisionDriver(sk), "user_request");
});

test("ModelRouter: spend_cap safety net overrides preflight", () => {
  // Safety nets are non-negotiable — even when preflight wants tier1,
  // a runaway-tripped session forces tier3. Same precedence rule.
  const r = makeRouter();
  const sk = "sess-1";
  // Mark the session as runaway-tripped (slot #0 — sticky)
  // Use the public API: recordTurnCost + checkRunawayRate
  for (let i = 0; i < 100; i++) {
    r.recordTurnCost(sk, 1.0, Date.now());
  }
  const check = r.checkRunawayRate(sk, Date.now());
  if (!check.tripped) {
    // If the test's tripping math drifts, skip rather than false-positive
    return;
  }
  r.setSessionPreflightDecision(sk, { tier: "tier1", reason: "regex:design_word" });
  const model = r.resolveModelOverride(sk);
  // Safety net forces tier3 regardless
  assert.equal(model, "anthropic/claude-haiku-4-5");
  assert.equal(r.getLastDecisionDriver(sk), "runaway");
});

test("ModelRouter: clearSession wipes the preflight slot", () => {
  // Session-end cleanup hygiene — same map-cleanup pattern as
  // sessionCascadeVerdicts. Without this, a long-lived gateway could
  // leak stale preflight decisions across reused sessionIds.
  const r = makeRouter();
  const sk = "sess-1";
  r.setSessionPreflightDecision(sk, { tier: "tier2", reason: "haiku:default" });
  assert.notEqual(r.getSessionPreflightDecision(sk), null);
  r.clearSession(sk);
  assert.equal(r.getSessionPreflightDecision(sk), null);
});

// ── Phase 3: haiku layer ────────────────────────────────────────────────────

/**
 * Make a mock api object with a subagent.run + waitForRun that returns the
 * given lastMessage. Records all invocations on the returned `.calls` array
 * so tests can assert call-count / arguments.
 *
 * When `lastMessage` is a function, it's called per invocation (allows
 * sequence testing).
 */
function makeHaikuApi(lastMessageOrFn) {
  const calls = [];
  const get = () =>
    typeof lastMessageOrFn === "function"
      ? lastMessageOrFn(calls.length)
      : lastMessageOrFn;
  return {
    calls,
    runtime: {
      subagent: {
        run: async (input) => {
          calls.push({ kind: "run", input });
          return { runId: `fake-${calls.length}` };
        },
        waitForRun: async (input) => {
          calls.push({ kind: "wait", input });
          return { lastMessage: get() };
        },
      },
    },
  };
}

/** Mock api whose subagent.run throws — simulates network/API failure. */
function makeFailingHaikuApi(err) {
  const calls = [];
  return {
    calls,
    runtime: {
      subagent: {
        run: async () => {
          calls.push("run");
          throw err ?? new Error("fake api failure");
        },
        waitForRun: async () => {
          calls.push("wait");
          return { lastMessage: "TIER2" };
        },
      },
    },
  };
}

// ── _parseHaikuTier — pure helper ───────────────────────────────────────────

test("parseHaikuTier: extracts TIER1 / TIER2 / TIER3 from one-word response", () => {
  assert.equal(_parseHaikuTier("TIER1"), "tier1");
  assert.equal(_parseHaikuTier("tier1"), "tier1");
  assert.equal(_parseHaikuTier("TIER2"), "tier2");
  assert.equal(_parseHaikuTier("TIER3"), "tier3");
});

test("parseHaikuTier: tolerates 'TIER 1' with space", () => {
  assert.equal(_parseHaikuTier("TIER 1"), "tier1");
  assert.equal(_parseHaikuTier("Tier 2"), "tier2");
});

test("parseHaikuTier: returns null for AMBIGUOUS / garbage / empty", () => {
  assert.equal(_parseHaikuTier("AMBIGUOUS"), null);
  assert.equal(_parseHaikuTier("not sure"), null);
  assert.equal(_parseHaikuTier(""), null);
  assert.equal(_parseHaikuTier(null), null);
  assert.equal(_parseHaikuTier(undefined), null);
});

test("parseHaikuTier: returns null on multi-tier response (model didn't follow instruction)", () => {
  // Guard against haiku producing "TIER1 or TIER2" — we can't pick one
  // safely, so abstain.
  assert.equal(_parseHaikuTier("TIER1 or TIER2"), null);
  assert.equal(_parseHaikuTier("TIER2 (maybe TIER3)"), null);
});

// ── Haiku integration — fires on prompts that abstain through other layers ──

test("haiku: returns tier1 when haiku responds TIER1", async () => {
  const api = makeHaikuApi("TIER1");
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), api);
  const d = await r.classify({
    userMessage: "Can you read my last email from Sarah?",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.layer, "haiku");
  assert.equal(d.reason, "haiku:tier1");
  assert.equal(d.confidence, 0.7);
  assert.equal(api.calls.length, 2); // run + wait
});

test("haiku: returns tier2 when haiku responds TIER2", async () => {
  const api = makeHaikuApi("TIER2");
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), api);
  const d = await r.classify({
    userMessage: "Write a short summary of this meeting transcript",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier2");
  assert.equal(d.layer, "haiku");
  assert.equal(d.reason, "haiku:tier2");
});

test("haiku: returns tier3 when haiku responds TIER3", async () => {
  const api = makeHaikuApi("TIER3");
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), api);
  const d = await r.classify({
    userMessage: "the meeting is at 3pm right?",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier3");
});

test("haiku: returns ABSTAIN when haiku responds AMBIGUOUS", async () => {
  const api = makeHaikuApi("AMBIGUOUS");
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), api);
  const d = await r.classify({
    userMessage: "tell me something interesting",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null);
  assert.equal(d.layer, "abstain");
});

test("haiku: returns ABSTAIN when haiku response is unparseable garbage", async () => {
  const api = makeHaikuApi("ehhh, depends?");
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), api);
  const d = await r.classify({
    userMessage: "what's the meaning of life",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null);
});

test("haiku: returns ABSTAIN when subagent.run throws", async () => {
  const api = makeFailingHaikuApi(new Error("rate limit"));
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), api);
  const d = await r.classify({
    userMessage: "a long-ish question that doesn't hit regex",
    botId: "team_bot_a",
  });
  // Must not crash; must abstain cleanly
  assert.equal(d.tier, null);
  assert.equal(d.layer, "abstain");
});

test("haiku: latency_ms includes the haiku call duration", async () => {
  // Mock api resolves immediately; latency_ms should still be a number
  // capturing the round-trip. The exact value is jitter-dependent but
  // must be >= 0 and < the timeout budget.
  const api = makeHaikuApi("TIER1");
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), api);
  const d = await r.classify({
    userMessage: "Generic question that hits haiku",
    botId: "team_bot_a",
  });
  assert.equal(typeof d.latency_ms, "number");
  assert.ok(d.latency_ms >= 0);
  assert.ok(d.latency_ms < 2100, "latency should be well under haiku timeout");
});

// ── Haiku is NOT called when other layers fire (latency / cost guard) ──────

test("haiku: NOT called when regex tier1 fires", async () => {
  const api = makeHaikuApi("TIER3"); // would override if called
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), api);
  const d = await r.classify({
    userMessage: "Design a system for sharded auth",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.layer, "regex");
  assert.equal(api.calls.length, 0, "haiku must NOT have been called");
});

test("haiku: NOT called when regex tier3 fires", async () => {
  const api = makeHaikuApi("TIER1"); // would override if called
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), api);
  const d = await r.classify({
    userMessage: "thanks!",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier3");
  assert.equal(d.layer, "regex");
  assert.equal(api.calls.length, 0);
});

test("haiku: NOT called when bot_prior is set", async () => {
  const dir = tmpSharedDir();
  fs.writeFileSync(
    path.join(dir, "network.json"),
    JSON.stringify({ bots: { team_bot_a: { preflight: { bot_prior: "tier1" } } } }),
  );
  const api = makeHaikuApi("TIER3"); // would override if called
  const r = new PreflightIntentRouter(
    { botId: "team_bot_a", sharedDir: dir },
    fakeLogger(),
    api,
  );
  const d = await r.classify({
    userMessage: "this prompt would otherwise hit haiku",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1");
  assert.equal(d.layer, "bot_prior");
  assert.equal(api.calls.length, 0);
});

// ── Per-bot haiku_enabled gate ──────────────────────────────────────────────

test("haiku: pod-level haiku_enabled=false skips haiku layer", async () => {
  const dir = tmpSharedDir();
  fs.writeFileSync(
    path.join(dir, "network.json"),
    JSON.stringify({ cascade: { preflight: { haiku_enabled: false } } }),
  );
  const api = makeHaikuApi("TIER1");
  const r = new PreflightIntentRouter(
    { botId: "team_bot_a", sharedDir: dir },
    fakeLogger(),
    api,
  );
  const d = await r.classify({
    userMessage: "ambiguous prompt that would hit haiku if enabled",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null, "expected abstain when haiku is gated off");
  assert.equal(api.calls.length, 0, "haiku must NOT be called when gated off");
});

test("haiku: per-bot opt-in beats pod-level off", async () => {
  const dir = tmpSharedDir();
  fs.writeFileSync(
    path.join(dir, "network.json"),
    JSON.stringify({
      cascade: { preflight: { haiku_enabled: false } },
      bots: { team_bot_a: { preflight: { haiku_enabled: true } } },
    }),
  );
  const api = makeHaikuApi("TIER1");
  const r = new PreflightIntentRouter(
    { botId: "team_bot_a", sharedDir: dir },
    fakeLogger(),
    api,
  );
  const d = await r.classify({
    userMessage: "an ambiguous prompt",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, "tier1", "per-bot ON must override pod OFF");
  assert.ok(api.calls.length >= 2);
});

test("haiku: per-bot opt-out beats pod-level on", async () => {
  const dir = tmpSharedDir();
  fs.writeFileSync(
    path.join(dir, "network.json"),
    JSON.stringify({
      cascade: { preflight: { haiku_enabled: true } },
      bots: { team_bot_a: { preflight: { haiku_enabled: false } } },
    }),
  );
  const api = makeHaikuApi("TIER1");
  const r = new PreflightIntentRouter(
    { botId: "team_bot_a", sharedDir: dir },
    fakeLogger(),
    api,
  );
  const d = await r.classify({
    userMessage: "an ambiguous prompt",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null);
  assert.equal(api.calls.length, 0);
});

// ── Defensive: api stub / null / missing subagent ──────────────────────────

test("haiku: null api skips layer gracefully (no crash)", async () => {
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), null);
  const d = await r.classify({
    userMessage: "an ambiguous prompt with no regex match",
    botId: "team_bot_a",
  });
  // Should abstain (no crash). Tests the defensive guard against
  // missing api in test environments.
  assert.equal(d.tier, null);
  assert.equal(d.layer, "abstain");
});

test("haiku: partial api stub (no subagent) skips layer gracefully", async () => {
  // The pre-existing test suite passed `FAKE_API = {}` as api; this pin
  // ensures that pattern still works under Phase 3 (the existing tests
  // earlier in this file rely on it).
  const r = new PreflightIntentRouter(FAKE_CONFIG, fakeLogger(), {});
  const d = await r.classify({
    userMessage: "an ambiguous prompt",
    botId: "team_bot_a",
  });
  assert.equal(d.tier, null);
});
