/**
 * A tripped cost breaker refuses SILENTLY — one message per trip per
 * conversation for a person, none at all for background work.
 *
 * Chip: internal/dispatch/done/breaker-reactivate-accepts-the-window.md.
 *
 * WHY THE REFUSAL MOVED HOOKS. ``before_agent_run`` cannot refuse quietly.
 * OC's ``resolveBlockMessage`` (verified in the fleet build 2026.9.2,
 * dist/hook-runner-global-*.js) turns every ``block`` decision into text —
 * with no ``message`` field it still emits "Your message could not be sent:
 * blocked by evolve" — and that text comes back as the turn's
 * ``promptError``, which the channel posts. One post per blocked heartbeat,
 * cron tick and inbound message is what filled a bot's Slack workspace on
 * 2026-09-07, addressed to people who could do nothing about it.
 *
 * ``before_agent_reply`` can. It fires once per run for triggers
 * cron|heartbeat|user, and a truthy ``handled`` makes OC short-circuit to
 * ``{text: "NO_REPLY"}`` — its silent sentinel — and return BEFORE
 * ``executePreparedEmbeddedRun``, so the model is never dispatched and
 * ``before_agent_run`` never runs (dist/embedded-agent-*.js,
 * buildHandledBeforeAgentReplyPayloads). Zero spend, zero bubbles.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/breakerRefusalSilence.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { TurnObserver } from "../dist/observer/TurnObserver.js";

const BOT = "team_bot_a";

function localDateYMD(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** Mirrors breakers.store.trip + spend_caps.write_enforcement_flag on disk. */
function armBreaker(shared, { checkpoint = "pending", tripId = "a1b2c3d4" } = {}) {
  const bdir = path.join(shared, "breakers", BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), JSON.stringify({
    bot_id: BOT, type: "cost", state: "tripped",
    tripped_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
    initiated_by: "auto:spend_alert",
    reason: "per-bot daily cap exceeded: $27.49 ≥ $5.00",
    trip_id: tripId, checkpoint,
  }));
  const sdir = path.join(shared, "spend-caps");
  fs.mkdirSync(sdir, { recursive: true });
  fs.writeFileSync(
    path.join(sdir, `${BOT}-${localDateYMD()}.json`),
    JSON.stringify({
      bot_id: BOT, date: localDateYMD(), action: "checkpoint",
      spend_at_trigger: 27.49, cap: 5.0, cleared: false,
    }),
  );
}

/** The operator's Reactivate: the breaker file is gone. */
function clearBreaker(shared) {
  fs.rmSync(path.join(shared, "breakers", BOT, "cost.json"), { force: true });
}

function makeHarness({
  injectPodConduct = true, injectKeywords = true,
} = {}) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-refusal-"));
  const logs = [];
  const logger = {
    info: (m) => logs.push(String(m)), warn: (m) => logs.push(String(m)),
    error: (m) => logs.push(String(m)), debug: () => {},
  };
  const observer = new TurnObserver({
    botId: BOT, role: "member", networkId: "n", sharedDir: shared,
    tier: "full",
    capabilities: {
      observer: true, injectPodConduct, injectKeywords,
      modelRouting: true, deferTool: false, recordApplicationTool: false,
    },
    tierClassification: "session", enableLLMSummarization: false,
    minTurns: 1, keywordConfidenceThreshold: 0.7,
  }, logger, undefined);
  observer.modelRouter = {
    setUserTier: () => {}, setSessionUserKey: () => {},
    setSessionType: () => {}, getSessionType: () => "conversation",
    isSpendCapForced: () => false,
    resolveModelOverride: () => null,
    resolveAuthProfileOverride: () => null,
    getLastDecisionDriver: () => null,
    clearSession: () => {},
  };
  observer._renderCapabilitiesBlock = async () => "";
  observer._renderDirectoryDigestBlock = async () => "";
  observer._recordCheckpointAnswer = async () => "recorded";

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
  /** One inbound turn, through the reply-claim hook first (as OC does). */
  const reply = (trigger, cleanedBody, conversation = "slack:C123") => fire(
    "before_agent_reply",
    { cleanedBody },
    {
      runId: `run-${Math.random()}`, trigger,
      sessionKey: conversation, channelId: "C123",
    },
  );
  const hookCount = (name) => (hooks.get(name) ?? []).length;
  return { shared, observer, fire, reply, logs, hookCount };
}

// ── Background triggers: no message, ever ────────────────────────────────────

test("a heartbeat under a tripped breaker is claimed silently", async () => {
  const h = makeHarness();
  armBreaker(h.shared);
  const res = await h.reply("heartbeat", "heartbeat");
  assert.equal(res.handled, true);
  // No `reply` field: OC's buildHandledBeforeAgentReplyPayloads defaults a
  // handled claim with no reply to `{text: "NO_REPLY"}` — the silent
  // sentinel. Anything we put here would be POSTED.
  assert.equal(res.reply, undefined);
});

test("a cron tick under a tripped breaker is claimed silently", async () => {
  const h = makeHarness();
  armBreaker(h.shared);
  const res = await h.reply("cron", "scheduled job");
  assert.equal(res.handled, true);
  assert.equal(res.reply, undefined);
});

test("background triggers are never claimed when nothing is tripped", async () => {
  const h = makeHarness();
  assert.equal(await h.reply("heartbeat", "heartbeat"), undefined);
  assert.equal(await h.reply("cron", "job"), undefined);
});

test("refusals log once a minute, not once per attempt", async () => {
  const h = makeHarness();
  armBreaker(h.shared);
  for (let i = 0; i < 25; i += 1) await h.reply("heartbeat", "heartbeat");
  const refusalLines = h.logs.filter((l) => l.includes("outcome=breaker_refused"));
  assert.equal(refusalLines.length, 1, `expected 1 log line, got ${refusalLines.length}`);
  assert.match(refusalLines[0], /trigger=heartbeat/);
});

// ── Human inbound: exactly one reply per trip per conversation ───────────────

test("the first human turn of a trip is let through so the hold can speak", async () => {
  const h = makeHarness();
  armBreaker(h.shared);
  assert.equal(await h.reply("user", "what's the weather?"), undefined);
});

test("every later human turn of the same trip is silent", async () => {
  const h = makeHarness();
  armBreaker(h.shared);
  await h.reply("user", "what's the weather?");
  for (const msg of ["hello?", "you there?", "anyone home"]) {
    const res = await h.reply("user", msg);
    assert.equal(res.handled, true, `"${msg}" should have been silenced`);
    assert.equal(res.reply, undefined);
  }
});

test("a second conversation gets its own single reply", async () => {
  const h = makeHarness();
  armBreaker(h.shared);
  await h.reply("user", "hi", "slack:C_ONE");
  assert.equal((await h.reply("user", "hi again", "slack:C_ONE")).handled, true);
  // A different channel has not been told anything yet.
  assert.equal(await h.reply("user", "hi", "slack:C_TWO"), undefined);
});

test("a NEW trip speaks again in a conversation it already answered", async () => {
  const h = makeHarness();
  armBreaker(h.shared, { tripId: "trip-one" });
  await h.reply("user", "hi");
  assert.equal((await h.reply("user", "hi again")).handled, true);
  armBreaker(h.shared, { tripId: "trip-two" });
  assert.equal(await h.reply("user", "hi once more"), undefined);
});

test('"continue" and "stop" are never silenced — the cap must stay liftable', async () => {
  const h = makeHarness();
  armBreaker(h.shared);
  // Burn the conversation's one message so the silence gate is armed.
  await h.reply("user", "hello");
  assert.equal((await h.reply("user", "hello again")).handled, true);
  // The owner's answer still reaches before_agent_run.
  assert.equal(await h.reply("user", "continue"), undefined);
  assert.equal(await h.reply("user", "stop"), undefined);
});

test("a user turn is not silenced when no checkpoint holds the bot", async () => {
  // D-CC1: an L1 trip alone does not gate user chat. Only a live
  // checkpoint hold does, and only after it has spoken once.
  const h = makeHarness();
  armBreaker(h.shared, { checkpoint: null });
  await h.reply("user", "hello");
  assert.equal(await h.reply("user", "hello again"), undefined);
});

// ── Reactivation ────────────────────────────────────────────────────────────

test("reactivation restores speech, including in a conversation already told", async () => {
  const h = makeHarness();
  armBreaker(h.shared);
  await h.reply("user", "hi");
  assert.equal((await h.reply("user", "hi again")).handled, true);
  clearBreaker(h.shared);
  assert.equal(await h.reply("user", "hi once more"), undefined);
  assert.equal(await h.reply("heartbeat", "heartbeat"), undefined);
});

// ── Unknown breaker state fails CLOSED ──────────────────────────────────────
//
// "Every Evolve switch fails closed; an unreadable config disables the
// feature" (D-OH5). A truncated write during a trip is exactly when
// cost.json is unreadable, and that is the moment the breaker must hold.
// An ABSENT file is the other thing entirely: it legitimately means "not
// tripped", and is the normal state of a healthy bot.

/** A cost.json that exists and cannot be used. */
function corruptBreaker(shared) {
  const bdir = path.join(shared, "breakers", BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), "{ not json");
}

test("a corrupt breaker file under a heartbeat is claimed silently", async () => {
  const h = makeHarness();
  corruptBreaker(h.shared);
  const res = await h.reply("heartbeat", "heartbeat");
  assert.equal(res.handled, true);
  assert.equal(res.reply, undefined);
});

test("a corrupt breaker file under a cron tick is claimed silently", async () => {
  const h = makeHarness();
  corruptBreaker(h.shared);
  const res = await h.reply("cron", "scheduled job");
  assert.equal(res.handled, true);
  assert.equal(res.reply, undefined);
});

test("a MISSING breaker file is not — absent is not unknown", async () => {
  const h = makeHarness();
  assert.equal(await h.reply("heartbeat", "heartbeat"), undefined);
  assert.equal(await h.reply("cron", "job"), undefined);
  assert.equal(await h.reply("user", "hello"), undefined);
});

test("a corrupt breaker file never silences a PERSON — D-CC1 still holds", async () => {
  // Unknown state stops background work, not conversation. Only a live
  // checkpoint hold silences a person (D-CC1: an L1 trip alone does not gate
  // user chat), and the hold is read out of the very record that is
  // unreadable — so there is no hold to enforce and every human turn passes
  // through. Failing closed here would mute somebody's conversation on a
  // half-written file, which is the opposite of the flood this fixes.
  const h = makeHarness();
  armBreaker(h.shared);            // writes the spend-caps checkpoint file
  corruptBreaker(h.shared);        // ...then the breaker record goes bad
  assert.equal(await h.reply("user", "hello"), undefined);
  assert.equal(await h.reply("user", "hello again"), undefined);
  // Background work on the same bot is still stopped.
  assert.equal((await h.reply("heartbeat", "heartbeat")).handled, true);
});

// ── The silent path exists on EVERY tier ────────────────────────────────────
//
// before_agent_reply is the only hook that can refuse quietly: a
// before_agent_run `block` is turned into channel text by OC's
// resolveBlockMessage even with no `message` field. While the registration
// sat inside `if (caps.injectPodConduct || caps.injectKeywords)`, a tier
// without those capabilities had no silent path at all and posted one
// "could not be sent" per blocked attempt — the flood, differently worded.

test("a heartbeat is claimed silently with BOTH capabilities off", async () => {
  const h = makeHarness({ injectPodConduct: false, injectKeywords: false });
  armBreaker(h.shared);
  const res = await h.reply("heartbeat", "heartbeat");
  assert.equal(res.handled, true);
  assert.equal(res.reply, undefined);
});

test("with both capabilities off the hook is still registered at all", async () => {
  // Guards the registration itself rather than its verdict: with no
  // `before_agent_reply` handler the harness's fire() returns undefined for
  // every input, which would make the assertion above vacuous if the hook
  // vanished for a different reason.
  const h = makeHarness({ injectPodConduct: false, injectKeywords: false });
  assert.equal(h.hookCount("before_agent_reply"), 1);
});

test("a cron tick is claimed silently with BOTH capabilities off", async () => {
  const h = makeHarness({ injectPodConduct: false, injectKeywords: false });
  armBreaker(h.shared);
  const res = await h.reply("cron", "scheduled job");
  assert.equal(res.handled, true);
  assert.equal(res.reply, undefined);
});

test("an ineligible trigger is left alone even while tripped", async () => {
  // OC only fires this hook for cron|heartbeat|user, but the gate is a
  // strict allowlist rather than a heuristic, so an unexpected trigger
  // passes through instead of being silenced on a guess.
  const h = makeHarness();
  armBreaker(h.shared);
  assert.equal(await h.reply("subagent", "internal"), undefined);
});
