/**
 * A tripped cost breaker must not change WHICH MODEL answers a user turn.
 *
 * Chip: internal/dispatch/done/breaker-reactivate-accepts-the-window.md.
 * Standing rule: "a tripped cap is a checkpoint, not a downshift."
 *
 * The hazard this file pins. Two of the four helpers behind the subagent
 * cost-breaker gate DECIDE the answering model: the preflight intent router
 * picks the rung for this turn, and the tier classifier fixes the session's
 * tier for every turn after it. On a bot whose cap action is not
 * ``checkpoint`` (D-CC1: an L1 trip pauses heartbeat + background work but
 * USER CHAT KEEPS WORKING), a person's turn is allowed through while the
 * breaker is tripped. If the gate refuses those two helpers on that turn,
 * the turn is not stopped — each call site catches the refusal and takes its
 * own heuristic fallback, so the turn is answered on a DIFFERENT rung than
 * it would have been, and the operator is never told. That is a breaker
 * moving a turn between rungs instead of stopping it.
 *
 * These tests drive the real PreflightIntentRouter and the real ModelRouter
 * and compare the RESOLVED MODEL STRING tripped vs clear — not the fact that
 * a call was made. The background half is asserted in the same file so the
 * carve-out cannot quietly widen: a heartbeat on the same tripped bot still
 * gets nothing.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/breakerAnsweringModel.test.mjs
 */
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PreflightIntentRouter } from "../dist/observer/PreflightIntentRouter.js";
import { LLMTierClassifier } from "../dist/observer/LLMTierClassifier.js";
import { ModelRouter } from "../dist/observer/ModelRouter.js";
import {
  configureSubagentBreakerGate,
  _resetSubagentBreakerGateForTest,
} from "../dist/observer/subagentRun.js";

const BOT = "team_bot_a";

/** A prompt that abstains through bot_prior and both regex layers. */
const AMBIGUOUS = "Can you read my last email from Sarah?";

const TIERS_CONFIG = {
  enabled: true,
  rungs: [
    { id: "haiku-class", models: ["anthropic/claude-haiku-4-5"], costClass: "low" },
    { id: "sonnet-class", models: ["anthropic/claude-sonnet-4-6"], costClass: "medium" },
    { id: "opus-class", models: ["anthropic/claude-opus-4-6"], costClass: "high" },
  ],
  roles: { fast: "haiku-class", standard: "sonnet-class", power: "opus-class" },
  routing: { enabled: true, backgroundRole: "fast", maintenanceRole: "fast" },
  tierCascade: ["tier2", "tier3", "tier1"],
};

function fakeLogger() {
  return { debug: () => {}, info: () => {}, warn: () => {}, error: () => {} };
}

/** Shared dir with the classifier gate OPEN — the haiku layer costs money
 *  and is off by default (D-OH2), so every test here opens it explicitly. */
function gateOpenSharedDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-answering-model-"));
  fs.writeFileSync(
    path.join(dir, "network.json"),
    JSON.stringify({ cascade: { classifiers: { enabled: true } } }),
  );
  return dir;
}

/** A tripped per-bot cost breaker with NO checkpoint: D-CC1's "user chat
 *  keeps working" shape, which is the configuration the hazard needs. */
function tripBreakerNoCheckpoint(shared) {
  const bdir = path.join(shared, "breakers", BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), JSON.stringify({
    bot_id: BOT, type: "cost", state: "tripped",
    tripped_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
    initiated_by: "auto:spend_alert",
    reason: "per-bot daily cap exceeded: $27.49 >= $5.00",
    trip_id: "deadbeef", checkpoint: null,
  }));
}

function clearBreaker(shared) {
  fs.rmSync(path.join(shared, "breakers", BOT, "cost.json"), { force: true });
}

/** Fake subagent runtime that answers every classification with `answer`. */
function makeApi(answer) {
  const runs = [];
  return {
    runs,
    runtime: {
      subagent: {
        run: async (input) => { runs.push(input); return { runId: `r${runs.length}` }; },
        waitForRun: async () => ({ lastMessage: answer }),
      },
    },
  };
}

/**
 * Route one interactive turn end to end and return the model string OC
 * would be handed, plus the router's own decision.
 */
async function routeOneTurn(shared, api, { trigger = "user" } = {}) {
  const router = new PreflightIntentRouter(
    { botId: BOT, sharedDir: shared }, fakeLogger(), api,
  );
  const decision = await router.classify({
    userMessage: AMBIGUOUS, botId: BOT, trigger,
  });
  const modelRouter = new ModelRouter(TIERS_CONFIG, shared, BOT);
  const sessionKey = "agent:main:slack:channel:C123";
  if (decision.tier !== null) {
    modelRouter.setSessionPreflightDecision(sessionKey, {
      tier: decision.tier, reason: decision.reason,
    });
  }
  return {
    decision,
    model: modelRouter.resolveModelOverride(sessionKey),
    driver: modelRouter.getLastDecisionDriver(sessionKey),
  };
}

beforeEach(() => { _resetSubagentBreakerGateForTest(); });

// ── The rule ────────────────────────────────────────────────────────────────

test("the preflight router answers on the SAME model tripped or clear", async () => {
  const shared = gateOpenSharedDir();
  configureSubagentBreakerGate({ sharedDir: shared, botId: BOT }, fakeLogger());

  // Breaker clear — the reference reading.
  const clearApi = makeApi("TIER1");
  const clear = await routeOneTurn(shared, clearApi);

  // Same bot, same prompt, breaker tripped with no checkpoint hold.
  tripBreakerNoCheckpoint(shared);
  const trippedApi = makeApi("TIER1");
  const tripped = await routeOneTurn(shared, trippedApi);

  assert.equal(
    tripped.model, clear.model,
    "a tripped breaker changed the answering model",
  );
  assert.equal(tripped.decision.tier, clear.decision.tier);
  assert.equal(tripped.decision.layer, clear.decision.layer);
  assert.equal(tripped.driver, clear.driver);

  // Anti-vacuity: the reference reading has to be a real routing decision,
  // not the bot default that an abstain would also produce.
  assert.equal(clear.decision.layer, "haiku");
  assert.equal(clear.model, "anthropic/claude-opus-4-6");
  assert.equal(clear.driver, "preflight");
  assert.equal(trippedApi.runs.length, 1, "the routing call must still happen");
});

test("the same holds for a tier3 verdict — it is not a tier1 accident", async () => {
  const shared = gateOpenSharedDir();
  configureSubagentBreakerGate({ sharedDir: shared, botId: BOT }, fakeLogger());
  const clear = await routeOneTurn(shared, makeApi("TIER3"));
  tripBreakerNoCheckpoint(shared);
  const tripped = await routeOneTurn(shared, makeApi("TIER3"));
  assert.equal(tripped.model, clear.model);
  assert.equal(clear.model, "anthropic/claude-haiku-4-5");
  assert.equal(clear.decision.layer, "haiku");
});

test("the tier classifier reaches the same verdict tripped or clear", async () => {
  // The other model-deciding helper. Its verdict fixes the session's tier
  // for every later turn, so a refusal here re-routes the rest of the
  // conversation onto the keyword fallback's class instead.
  const shared = gateOpenSharedDir();
  configureSubagentBreakerGate({ sharedDir: shared, botId: BOT }, fakeLogger());
  const config = { botId: BOT, sharedDir: shared, classifierKeywordConfidenceFloor: 0.99 };

  const clearApi = makeApi("MAINTENANCE");
  const clear = await new LLMTierClassifier(config, fakeLogger(), clearApi)
    .classify(AMBIGUOUS, undefined, { trigger: "user" });

  tripBreakerNoCheckpoint(shared);
  const trippedApi = makeApi("MAINTENANCE");
  const tripped = await new LLMTierClassifier(config, fakeLogger(), trippedApi)
    .classify(AMBIGUOUS, undefined, { trigger: "user" });

  assert.deepEqual(tripped, clear);
  // Anti-vacuity: the reference reading is the LLM's verdict, not the
  // keyword fallback both sides would agree on for the wrong reason.
  assert.deepEqual(clear.signals, ["llm-classified"]);
  assert.equal(clear.class, "maintenance");
  assert.equal(trippedApi.runs.length, 1);
});

// ── The carve-out stops exactly there ───────────────────────────────────────

test("a heartbeat on the same tripped bot is still refused", async () => {
  // The paused bot's background machinery stays stopped — that is where the
  // 10,407 -> 13,282 preflight bleed lived. The carve-out buys an
  // interactive turn its own routing, nothing else.
  const shared = gateOpenSharedDir();
  configureSubagentBreakerGate({ sharedDir: shared, botId: BOT }, fakeLogger());
  tripBreakerNoCheckpoint(shared);
  const api = makeApi("TIER1");
  const out = await routeOneTurn(shared, api, { trigger: "heartbeat" });
  assert.equal(api.runs.length, 0, "background work must not spend");
  assert.equal(out.decision.tier, null);
  assert.equal(out.decision.layer, "abstain");
});

test("a cron tick on the same tripped bot is still refused", async () => {
  const shared = gateOpenSharedDir();
  configureSubagentBreakerGate({ sharedDir: shared, botId: BOT }, fakeLogger());
  tripBreakerNoCheckpoint(shared);
  const api = makeApi("TIER1");
  const out = await routeOneTurn(shared, api, { trigger: "cron" });
  assert.equal(api.runs.length, 0);
  assert.equal(out.decision.layer, "abstain");
});

test("an unreadable breaker record does not re-route an interactive turn either", async () => {
  // The gate fails CLOSED on unknown state — but the re-routing hazard does
  // not care why the gate would refuse, so the carve-out covers it too.
  const shared = gateOpenSharedDir();
  configureSubagentBreakerGate({ sharedDir: shared, botId: BOT }, fakeLogger());
  const clear = await routeOneTurn(shared, makeApi("TIER1"));
  const bdir = path.join(shared, "breakers", BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), "{ not json");
  const tripped = await routeOneTurn(shared, makeApi("TIER1"));
  assert.equal(tripped.model, clear.model);
  assert.equal(clear.decision.layer, "haiku");
});

test("reactivation is a no-op for routing — it was never re-routed", async () => {
  // Closes the loop the other way: the model an interactive turn resolves to
  // is the same before the trip, during it, and after the operator clears it.
  const shared = gateOpenSharedDir();
  configureSubagentBreakerGate({ sharedDir: shared, botId: BOT }, fakeLogger());
  const before = await routeOneTurn(shared, makeApi("TIER2"));
  tripBreakerNoCheckpoint(shared);
  const during = await routeOneTurn(shared, makeApi("TIER2"));
  clearBreaker(shared);
  const after = await routeOneTurn(shared, makeApi("TIER2"));
  assert.equal(during.model, before.model);
  assert.equal(after.model, before.model);
  assert.equal(before.model, "anthropic/claude-sonnet-4-6");
});
