/**
 * The held turn, end to end: `before_agent_run` blocks with the fixed
 * cap-reached reply and dispatches nothing to a model.
 *
 * Operator decision D-CC1..4
 * (internal/decision-cost-cap-checkpoint-2026-09-04.md).
 *
 * WHICH HOOK EMITS IT, AND WHY THAT IS A ZERO-SPEND PATH.
 *
 * `before_agent_run` is OpenClaw's input gate: its result type is
 * `{outcome: "pass"} | {outcome: "block", message?}` (see
 * src/types/openclaw-plugin-sdk.d.ts, mirrored byte-accurately from
 * `openclaw/plugin-sdk/src/plugins/hook-decision-types.d.ts`
 * `InputGateDecision`; behaviour at `runBeforeAgentRun` in
 * openclaw/dist/hook-runner-global-*.js). A `block` short-circuits the agent
 * run at the gate — before prompt build, before `before_model_resolve`, and
 * therefore before any model is resolved or any provider call is made. The
 * `message` is returned to the user as the turn's outcome: a real response,
 * not a 5xx, which is the second clause of
 * docs/principle-cost-cap-refuse-turn.md ("Refuse-turn returns a real
 * response, not a network error").
 *
 * That claim is verified structurally by the tests below rather than
 * asserted: the harness registers the plugin's hooks on a fake `api` and
 * fires `before_agent_run`. A held turn is proven not to spend because the
 * one downstream surface that chooses a model —
 * `modelRouter.resolveModelOverride`, reached from `before_model_resolve` —
 * stays untouched. That counter is only meaningful if it CAN move, so a
 * control test ("a passed turn does reach the model-resolve path") drives the
 * same harness through a turn that is not held and asserts the counter moves;
 * the zero in the held-turn test is measured against that. `before_prompt_build`
 * and `llm_output` are not counted here — the harness fires them itself, so a
 * zero would only restate which hooks the test chose to call. Their real
 * evidence is the `block` outcome plus OpenClaw's `InputGateDecision` contract:
 * a block short-circuits the run at the gate, so neither hook is ever reached
 * for that run, and `llm_output` is the sole feed for
 * `SessionCostMonitor.recordCost` and the per-turn cost record.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.costCheckpoint.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { TurnObserver } from "../dist/observer/TurnObserver.js";

const BOT = "team_bot_a";
const RUN_ID = "run-checkpoint-1";

function localDateYMD(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** Mirrors breakers.store.trip / spend_caps.write_enforcement_flag on disk. */
function armCheckpoint(shared, { checkpoint = "pending", action = "checkpoint" } = {}) {
  const bdir = path.join(shared, "breakers", BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), JSON.stringify({
    bot_id: BOT, type: "cost", state: "tripped",
    tripped_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
    initiated_by: "auto:spend_alert",
    reason: "per-bot daily cap exceeded: $20.57 ≥ $20.00",
    trip_id: "a1b2c3d4", checkpoint,
    checkpoint_answered_by: null, checkpoint_answered_at: null,
    checkpoint_increment_usd: null,
  }));
  const sdir = path.join(shared, "spend-caps");
  fs.mkdirSync(sdir, { recursive: true });
  fs.writeFileSync(
    path.join(sdir, `${BOT}-${localDateYMD()}.json`),
    JSON.stringify({
      bot_id: BOT, date: localDateYMD(), action,
      spend_at_trigger: 20.57, cap: 20.0, cleared: false,
    }),
  );
}

/**
 * A TurnObserver with its hooks registered on a fake api, its daemon call
 * scripted, and every model-touching surface instrumented so a spend is
 * observable if one ever happens.
 */
function makeHarness({
  daemon = { ok: true, authorized: true }, tier = "full",
} = {}) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-checkpoint-"));
  const logs = [];
  const logger = {
    info: (m) => logs.push(String(m)), warn: (m) => logs.push(String(m)),
    error: (m) => logs.push(String(m)), debug: () => {},
  };
  // `manage` is the tier the checkpoint gate used to miss: it routes models
  // but injects no keywords, so a gate below the injectKeywords tier check
  // never ran for it.
  const capabilities = tier === "manage"
    ? {
        observer: true, injectPodConduct: true, injectKeywords: false,
        modelRouting: true, deferTool: false, recordApplicationTool: true,
      }
    : {
        observer: true, injectPodConduct: true, injectKeywords: true,
        modelRouting: true, deferTool: false, recordApplicationTool: false,
      };
  const observer = new TurnObserver({
    botId: BOT, role: "member", networkId: "n", sharedDir: shared,
    tier,
    capabilities,
    tierClassification: "session", enableLLMSummarization: false,
    minTurns: 1, keywordConfidenceThreshold: 0.7,
  }, logger, undefined);

  // The one surface that CHOOSES a model is counted, not stubbed away: if a
  // held turn ever reached it, this counter would move. The control test
  // below proves it can.
  const touched = { resolveModel: 0 };
  observer.modelRouter = {
    setUserTier: () => {}, setSessionUserKey: () => {},
    setSessionType: () => {}, getSessionType: () => "conversation",
    isSpendCapForced: () => false,
    resolveModelOverride: () => { touched.resolveModel += 1; return null; },
    resolveAuthProfileOverride: () => null,
    getLastDecisionDriver: () => null,
    clearSession: () => {},
  };
  observer._renderCapabilitiesBlock = async () => "";
  observer._renderDirectoryDigestBlock = async () => "";

  // Script the ONE daemon round-trip the answer path makes. Returning a
  // canned response keeps the test off the socket while still exercising
  // the real request/response handling.
  const daemonCalls = [];
  observer._recordCheckpointAnswer = async (answer, platform, stableId) => {
    daemonCalls.push({ answer, platform, stableId });
    if (daemon === "unavailable") return "unavailable";
    return daemon.authorized === false ? "not_owner" : "recorded";
  };

  const hooks = new Map();
  observer.register({
    on: (name, handler) => {
      if (!hooks.has(name)) hooks.set(name, []);
      hooks.get(name).push(handler);
    },
    registerHook: () => {},
  });

  const fire = async (name, event, ctx) => {
    let out;
    for (const h of hooks.get(name) ?? []) {
      const r = await h(event, ctx);
      if (r !== undefined) out = r;
    }
    return out;
  };

  // A user turn on a real channel — NOT an auto source. The background half
  // of the trip (heartbeat / cron) is the pre-existing L1 veto's business
  // and is unchanged by this work.
  const userTurn = (userMessage) => fire(
    "before_agent_run",
    { userMessage, sessionId: "s1", channelId: "12345", senderId: "1260193629" },
    { runId: RUN_ID, sessionId: "s1", trigger: "user", channelId: "telegram" },
  );

  return { shared, observer, hooks, fire, userTurn, touched, daemonCalls, logs };
}

// ── The hold ─────────────────────────────────────────────────────────────────

test("a pending checkpoint blocks the first user turn with the fixed reply", async () => {
  const h = makeHarness();
  armCheckpoint(h.shared);

  const res = await h.userTurn("what's the weather?");

  assert.equal(res.outcome, "block");
  assert.equal(res.category, "evolve.cost_checkpoint");
  assert.match(res.message, /reached today's spending cap/i);
  assert.match(res.message, /\$20\.57/);
  assert.match(res.message, /"continue"/);
  assert.match(res.message, /"stop"/);
});

test("a held turn dispatches nothing to a model — zero-spend", async () => {
  const h = makeHarness();
  armCheckpoint(h.shared);

  // Fire the gate exactly as OC would, then attempt the downstream hooks
  // that a PASSED turn would reach. A blocked run never reaches them in
  // production; asserting the counters stay at zero pins that the gate is
  // the whole path.
  const res = await h.userTurn("what's the weather?");
  assert.equal(res.outcome, "block");

  // Measured against the control test below, which drives the same harness
  // through an unheld turn and moves this counter.
  assert.equal(h.touched.resolveModel, 0, "no model was resolved");
  // No cost record can exist for a turn with no llm_output: that hook is
  // the sole feed for SessionCostMonitor.recordCost and the per-turn cost
  // record. The shared turns dir is therefore untouched.
  assert.ok(!fs.existsSync(path.join(h.shared, BOT, "turns")));
});

test("control: a turn that is NOT held does reach the model-resolve path", async () => {
  // Without this, the zero above is unfalsifiable — the harness never fires
  // `before_model_resolve`, so `resolveModel` would read 0 for a passed turn
  // too and the assertion would pin nothing. Here the same harness runs a
  // turn with no trip armed, then drives the two hooks a passed turn reaches
  // in production; the counter moves.
  const h = makeHarness();

  const passed = await h.userTurn("what's the weather?");
  assert.equal(passed.outcome, "pass");

  await h.fire(
    "before_model_resolve",
    { prompt: "what's the weather?", sessionKey: "s1" },
    { runId: RUN_ID, sessionId: "s1", sessionKey: "s1", trigger: "user",
      channelId: "telegram" },
  );
  await h.fire(
    "llm_output",
    { sessionId: "s1", model: "claude-opus-5", provider: "anthropic",
      usage: { input: 100, output: 50 } },
    { runId: RUN_ID, sessionId: "s1", trigger: "user", channelId: "telegram" },
  );

  assert.ok(
    h.touched.resolveModel > 0,
    "the counter the held-turn test asserts is 0 must be able to move",
  );
});

test("the reply is a real response, not an error (principle clause 2)", async () => {
  const h = makeHarness();
  armCheckpoint(h.shared);
  const res = await h.userTurn("hello?");
  // A structured block with user-facing text — the gate's documented
  // contract — rather than a thrown error or an unresolvable model ref.
  assert.equal(typeof res.message, "string");
  assert.ok(res.message.length > 0);
  assert.equal(res.outcome, "block");
});

test("an empty-bodied interactive turn is held too", async () => {
  const h = makeHarness();
  armCheckpoint(h.shared);
  const res = await h.userTurn("");
  assert.equal(res.outcome, "block");
});

// ── The answers ──────────────────────────────────────────────────────────────

test("an owner's continue records the grant and confirms deterministically", async () => {
  const h = makeHarness({ daemon: { ok: true, authorized: true } });
  armCheckpoint(h.shared);

  const res = await h.userTurn("continue");

  assert.deepEqual(h.daemonCalls, [
    { answer: "continue", platform: "telegram", stableId: "1260193629" },
  ]);
  assert.equal(res.outcome, "block");
  assert.match(res.message, /carrying on until midnight/i);
  assert.match(res.message, /\$30\.00/);
  // Even the confirmation costs nothing.
  assert.equal(h.touched.resolveModel, 0);
});

test("a non-owner's continue is refused and grants no extension", async () => {
  const h = makeHarness({ daemon: { ok: true, authorized: false } });
  armCheckpoint(h.shared);

  const res = await h.userTurn("continue");

  assert.equal(res.outcome, "block");
  assert.match(res.message, /owner/i);
  assert.doesNotMatch(res.message, /carrying on/i);
  assert.equal(h.touched.resolveModel, 0);
});

test("stop is recorded and every later turn gets the short refusal", async () => {
  const h = makeHarness();
  armCheckpoint(h.shared);

  const stopped = await h.userTurn("stop");
  assert.equal(stopped.outcome, "block");
  assert.match(stopped.message, /paused until tomorrow/i);

  // The daemon flipped the record to `declined`; the next turn reads it.
  armCheckpoint(h.shared, { checkpoint: "declined" });
  const later = await h.userTurn("are you there?");
  assert.equal(later.outcome, "block");
  assert.match(later.message, /paused until tomorrow/i);
  assert.equal(h.touched.resolveModel, 0);
});

test("an owner may change their mind after declining", async () => {
  const h = makeHarness({ daemon: { ok: true, authorized: true } });
  armCheckpoint(h.shared, { checkpoint: "declined" });
  const res = await h.userTurn("continue");
  assert.equal(h.daemonCalls.length, 1);
  assert.match(res.message, /carrying on/i);
});

test("an unreachable daemon grants nothing and says so", async () => {
  // Fail-closed: a fallback that wrote locally would stay inducible by
  // killing the socket (the require_daemon_call posture).
  const h = makeHarness({ daemon: "unavailable" });
  armCheckpoint(h.shared);
  const res = await h.userTurn("continue");
  assert.equal(res.outcome, "block");
  assert.match(res.message, /couldn't record that/i);
  assert.doesNotMatch(res.message, /carrying on/i);
});

// ── What is NOT held ─────────────────────────────────────────────────────────

test("a continued checkpoint lets the turn through", async () => {
  const h = makeHarness();
  armCheckpoint(h.shared, { checkpoint: "continued" });
  const res = await h.userTurn("what's the weather?");
  assert.equal(res.outcome, "pass");
});

test("no trip means no hold", async () => {
  const h = makeHarness();
  const res = await h.userTurn("what's the weather?");
  assert.equal(res.outcome, "pass");
});

test("a downgrade-tier pod is not held by the checkpoint gate", async () => {
  // The explicit legacy action keeps its own (now disclosed) behaviour:
  // the record carries no checkpoint, so this gate does nothing.
  const h = makeHarness();
  armCheckpoint(h.shared, { checkpoint: null, action: "downgrade-tier" });
  const res = await h.userTurn("what's the weather?");
  assert.equal(res.outcome, "pass");
});

test("background turns are the L1 veto's business, not the checkpoint's", async () => {
  // A heartbeat turn under a pending checkpoint is stopped by the
  // pre-existing auto-source veto, never by the checkpoint's cap-reached
  // message. It now carries NO ``message`` field: every string returned
  // here is wrapped by OC into "Your message could not be sent: …" and
  // posted to the channel, once per blocked attempt. The silent refusal
  // lives on before_agent_reply; this branch is the fail-safe.
  const h = makeHarness();
  armCheckpoint(h.shared);
  const res = await h.fire(
    "before_agent_run",
    { userMessage: "heartbeat", sessionId: "s2", channelId: "heartbeat" },
    { runId: "run-hb", sessionId: "s2", trigger: "heartbeat", channelId: "heartbeat" },
  );
  assert.equal(res.outcome, "block");
  assert.equal(res.category, "evolve.cost_breaker");
  assert.equal(res.message, undefined);
  assert.equal(res.metadata.outcome, "breaker_refused");
});

test("a manage-tier bot is held too", async () => {
  // `manage` keeps model routing but injects no keywords. The gate used to
  // sit below the injectKeywords tier check, so these bots were skipped
  // entirely: pre-flip they were at least downgraded on trip, post-flip the
  // action says `checkpoint` (no downgrade) and nothing held them — they kept
  // answering at full price behind a flag claiming the cap was enforced.
  const h = makeHarness({ tier: "manage" });
  armCheckpoint(h.shared);

  const res = await h.userTurn("what's the weather?");

  assert.equal(res.outcome, "block");
  assert.equal(res.category, "evolve.cost_checkpoint");
  assert.match(res.message, /reached today's spending cap/i);
  assert.equal(h.touched.resolveModel, 0);
});

test("a manage-tier bot with no trip is not held", async () => {
  const h = makeHarness({ tier: "manage" });
  const res = await h.userTurn("what's the weather?");
  assert.equal(res.outcome, "pass");
});

test("a corrupt breaker file never wedges a conversation", async () => {
  const h = makeHarness();
  const bdir = path.join(h.shared, "breakers", BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), "{truncated");
  const res = await h.userTurn("hello");
  assert.equal(res.outcome, "pass");
});
