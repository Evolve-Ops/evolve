/**
 * Tests for the "evo keyword path fails LOUD instead of confabulating" change
 * ([META:evo-asst]).
 *
 * Background: when a RECOGNIZED evo command's admin dispatch failed (auth 401,
 * connection refused, malformed envelope, empty handler output), the plugin
 * fell through to the bot's own LLM, which invented a confident-but-fake
 * OpenClaw help screen with ZERO error signal — so the break ran for days
 * until a human noticed. The fix:
 *
 *   1. EvoDispatchClient.dispatch returns a discriminated outcome
 *      ({ok:true,result} | {ok:false,reason,status?}) instead of null, so the
 *      caller can tell transport/auth/empty apart and tailor the message.
 *   2. On any failed branch the plugin delivers an honest, non-hallucinated
 *      user message and keeps the LLM silent — never a confabulation.
 *   3. Per-failure telemetry (in-process counter + structured log line).
 *   4. A one-shot startup self-test logs PASS/FAIL.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/evoDispatchFailLoud.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import fs from "node:fs";
import path from "node:path";

import {
  classifyDispatchResponse,
  evoFailureUserMessage,
} from "../dist/better/EvoDispatchClient.js";
import { TurnObserver } from "../dist/observer/TurnObserver.js";

// ── Part A: pure status → outcome classification ────────────────────────────

test("classifyDispatchResponse: 401/403 → unauthorized (with status)", () => {
  for (const code of [401, 403]) {
    const out = classifyDispatchResponse(code, { error: "device not paired" });
    assert.equal(out.ok, false);
    assert.equal(out.reason, "unauthorized");
    assert.equal(out.status, code);
  }
});

test("classifyDispatchResponse: other non-2xx → error (with status)", () => {
  for (const code of [500, 502, 404, 0]) {
    const out = classifyDispatchResponse(code, null);
    assert.equal(out.ok, false);
    assert.equal(out.reason, "error");
    assert.equal(out.status, code);
  }
});

test("classifyDispatchResponse: 2xx but malformed envelope → error", () => {
  // 200 with a body that lacks the required `mode` discriminator.
  for (const body of [null, "not an object", {}, { system_append: "hi" }]) {
    const out = classifyDispatchResponse(200, body);
    assert.equal(out.ok, false, `body=${JSON.stringify(body)}`);
    assert.equal(out.reason, "error");
  }
});

test("classifyDispatchResponse: 2xx valid envelope → ok with result", () => {
  const envelope = {
    subcommand: "help",
    role: "secondary",
    mode: "speak",
    system_append: "Respond verbatim …",
    direct_send_message: "evo help body",
  };
  const out = classifyDispatchResponse(200, envelope);
  assert.equal(out.ok, true);
  assert.equal(out.result.subcommand, "help");
  assert.equal(out.result.system_append, "Respond verbatim …");
});

// ── Part B: honest, non-hallucinated user messages ──────────────────────────

test("evoFailureUserMessage: every reason is honest and not a help screen", () => {
  const reasons = ["unreachable", "unauthorized", "empty", "error"];
  for (const reason of reasons) {
    const msg = evoFailureUserMessage(reason);
    assert.ok(msg && msg.length > 0, `${reason} message empty`);
    // Honest framing: a warning sign + names evo, never a fabricated answer.
    assert.ok(msg.includes("⚠️"), `${reason} missing warning marker`);
    assert.ok(/evo/i.test(msg), `${reason} should mention evo`);
    // Must NOT look like the confabulated OpenClaw help/command screen.
    assert.ok(!/available commands|here are the commands|help screen|usage:/i.test(msg),
      `${reason} message looks like a help screen`);
  }
});

test("evoFailureUserMessage: reasons are distinguishable (transport vs auth vs internal)", () => {
  assert.ok(/connection/i.test(evoFailureUserMessage("unreachable")));
  assert.ok(/auth|pairing/i.test(evoFailureUserMessage("unauthorized")));
  assert.ok(/internal error/i.test(evoFailureUserMessage("empty")));
  // unreachable + unauthorized both say "can't reach"; empty says "internal".
  assert.notEqual(
    evoFailureUserMessage("unreachable"),
    evoFailureUserMessage("unauthorized"),
  );
});

// ── Part C: handler integration (fail loud, no LLM fallthrough) ─────────────

/** Build a TurnObserver wired to a fake dispatch client + stubbed direct-send. */
function makeObserver({ directSendOk = true } = {}) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-failloud-"));
  const logs = [];
  const logger = {
    info: (m) => logs.push(["info", String(m)]),
    warn: (m) => logs.push(["warn", String(m)]),
    error: (m) => logs.push(["error", String(m)]),
    debug: () => {},
  };
  const config = {
    botId: "team_bot_a",
    role: "member",
    networkId: "n",
    sharedDir: shared,
    tier: "full",
    capabilities: {
      observer: true, injectPodConduct: true, injectKeywords: true,
      modelRouting: true, deferTool: true, recordApplicationTool: true,
    },
    tierClassification: "session",
    enableLLMSummarization: false,
    minTurns: 1,
    keywordConfidenceThreshold: 0.7,
    enableTaskExtraction: false,
  };
  const observer = new TurnObserver(config, logger, undefined);

  // Fake dispatch client — records calls; dispatch() returns whatever the test
  // arms via setDispatch(). findActiveWizard/wizardTurn default to "nothing".
  const calls = { dispatch: 0, wizardTurn: 0 };
  let nextOutcome = { ok: false, reason: "unreachable" };
  const fakeClient = {
    dispatch: async () => { calls.dispatch++; return nextOutcome; },
    findActiveWizard: async () => null,
    wizardTurn: async () => { calls.wizardTurn++; return null; },
  };
  observer.evoDispatchClient = fakeClient;

  // Stub the Telegram direct-send so no network happens; record the body.
  const sent = [];
  observer._sendEvoDirectToTelegram = async (_ctx, _rec, body) => {
    sent.push(body);
    return directSendOk;
  };

  return {
    observer,
    logs,
    sent,
    calls,
    setDispatch: (o) => { nextOutcome = o; },
  };
}

/** A recognized-evo before_agent_run event. */
function evoEvent(text = "evo help") {
  return { userMessage: text, sessionId: "sess1", channelId: "chan1" };
}
const userCtx = { trigger: "user", runId: "run1", sessionKey: "sess1" };

test("dispatch-null (unreachable) → honest error direct-sent, LLM stays silent, telemetry bumped", async () => {
  const h = makeObserver({ directSendOk: true });
  h.setDispatch({ ok: false, reason: "unreachable" });

  const res = await h.observer.handleBeforeAgentRun(evoEvent(), userCtx);

  // Handler passes (does not block), but the LLM is NOT left to answer freely:
  assert.deepEqual(res, { outcome: "pass" });
  // The user got the real, honest status out of band …
  assert.equal(h.sent.length, 1);
  assert.ok(h.sent[0].includes("⚠️"));
  assert.ok(/connection/i.test(h.sent[0]));
  // … and the LLM is told to STAY SILENT (stay-silent injection stored).
  const injection = h.observer._pendingKeywordInjection.get("sess1");
  assert.ok(injection, "expected a pending injection");
  assert.ok(/STAY SILENT/i.test(injection), "expected stay-silent directive");
  // Telemetry: counter bumped + greppable FAILED log line.
  assert.equal(h.observer.getEvoDispatchFailureCounts().unreachable, 1);
  assert.ok(h.logs.some(([lvl, m]) =>
    lvl === "warn" && m.includes("evo dispatch FAILED") && m.includes("reason=unreachable")));
});

test("dispatch-empty-envelope → honest internal-error, telemetry reason=empty", async () => {
  const h = makeObserver({ directSendOk: true });
  // Valid envelope, but handler produced nothing deliverable.
  h.setDispatch({
    ok: true,
    result: {
      subcommand: "help", role: "secondary", mode: "speak",
      system_append: null, direct_send_message: null,
      wizard_session_id: null, session_tier_override: null, subcommand_brief: null,
    },
  });

  const res = await h.observer.handleBeforeAgentRun(evoEvent(), userCtx);

  assert.deepEqual(res, { outcome: "pass" });
  assert.equal(h.sent.length, 1);
  assert.ok(/internal error/i.test(h.sent[0]));
  assert.equal(h.observer.getEvoDispatchFailureCounts().empty, 1);
  assert.ok(h.logs.some(([lvl, m]) =>
    lvl === "warn" && m.includes("evo dispatch FAILED") && m.includes("reason=empty")));
});

test("dispatch failure with direct-send unavailable → verbatim relay directive (no confabulation)", async () => {
  const h = makeObserver({ directSendOk: false }); // Telegram send fails
  h.setDispatch({ ok: false, reason: "unauthorized", status: 401 });

  await h.observer.handleBeforeAgentRun(evoEvent(), userCtx);

  const injection = h.observer._pendingKeywordInjection.get("sess1");
  assert.ok(injection, "expected a pending injection");
  // Relay directive must carry the honest body AND forbid invention.
  assert.ok(injection.includes("⚠️"));
  assert.ok(/do NOT invent an answer/i.test(injection));
  assert.ok(/do NOT show a help screen/i.test(injection));
  assert.equal(h.observer.getEvoDispatchFailureCounts().unauthorized, 1);
  // status is threaded into telemetry.
  assert.ok(h.logs.some(([, m]) => m.includes("status=401")));
});

test("member_bot success path direct-SENDS the fail-loud message (never hands it to the LLM-echo directive)", async () => {
  // The whole point of the fail-loud-direct-send fix: when a channel-direct emit
  // is available, the honest error reaches the user via direct-send and the LLM
  // is told to STAY SILENT — the error body is NEVER routed through the echo
  // directive, so a divergent model has nothing to narrate.
  const h = makeObserver({ directSendOk: true });
  h.setDispatch({ ok: false, reason: "unauthorized", status: 401 });

  await h.observer.handleBeforeAgentRun(evoEvent(), userCtx);

  // The honest body was direct-sent out of band …
  assert.equal(h.sent.length, 1);
  assert.ok(h.sent[0].includes("⚠️"));
  assert.ok(/auth|pairing/i.test(h.sent[0]));
  // … and the LLM is told to STAY SILENT, NOT to relay/echo the error.
  const injection = h.observer._pendingKeywordInjection.get("sess1");
  assert.ok(injection, "expected a pending injection");
  assert.ok(/STAY SILENT/i.test(injection), "direct-sent run must be stay-silent");
  // Negative guarantee: the stay-silent injection is the buildStaySilentInjection
  // anchor (body embedded only as a "do NOT repeat this" preview), NOT the
  // echo-relay directive that asks the model to OUTPUT the body. A divergent
  // model therefore has nothing to "respond verbatim with" on a direct-sent run.
  assert.ok(!/your reply for this turn has already been composed/i.test(injection),
    "direct-sent run must not use the echo-relay 'output this' directive");
  assert.ok(/do NOT repeat/i.test(injection),
    "stay-silent run frames the body as already-delivered (do not repeat), not as output-this");
});

test("hardened echo directive: presents the body as the assistant's own reply, not a 'repeat verbatim' instruction", async () => {
  // Regression guard for the live VPS leak: a divergent model narrated the old
  // "respond verbatim with: <msg>" frame as reported speech. The hardened
  // wording frames the body as the assistant's already-composed reply.
  const h = makeObserver({ directSendOk: false }); // forces the echo fallback
  h.setDispatch({ ok: false, reason: "unreachable" });

  await h.observer.handleBeforeAgentRun(evoEvent(), userCtx);

  const injection = h.observer._pendingKeywordInjection.get("sess1");
  assert.ok(injection, "expected a pending injection (echo fallback)");
  // POSITIVE: the new "your reply has already been composed — output it
  // exactly" framing is present.
  assert.ok(/already been composed/i.test(injection),
    "hardened directive should frame the body as the pre-composed reply");
  assert.ok(/output it exactly/i.test(injection),
    "hardened directive should tell the model to output the message exactly");
  // Anti-narration: the model must be told NOT to say where the message came
  // from / not to mention the instructions.
  assert.ok(/do not mention these instructions|do not say where it came from/i.test(injection),
    "hardened directive must forbid narrating the instruction");
  // NEGATIVE: the fragile "respond ONLY with the following message, verbatim"
  // frame (the exact phrasing the VPS model narrated) is gone.
  assert.ok(!/respond only with the following message, verbatim/i.test(injection),
    "the narratable 'respond verbatim with:' frame must be gone");
  // The honest body still rides along for the model to emit.
  assert.ok(injection.includes("⚠️"));
});

// ── Part C2: channel-agnostic echo invariant (non-Telegram channels) ─────────
//
// Direct-send via the Telegram Bot API is an OPTIMIZATION available only when a
// Telegram transport exists for the run. On Slack / Discord / WhatsApp (and any
// future transport) ``_sendEvoDirectToTelegram`` returns false, so the run is
// NOT direct-sent. The universal substrate is ``_llmEchoRuns`` (consumed by
// before_prompt_build): the invariant is "a run that was NOT direct-sent MUST
// get an ``_llmEchoRuns`` directive — for BOTH success and failure". These two
// tests model a member bot on a non-Telegram channel (directSendOk:false) and
// assert the echo directive is populated AND uses the hardened, non-narratable
// wording (not the old "respond verbatim with:" frame).

test("SUCCESS relay on a non-Telegram channel → _llmEchoRuns populated with the hardened verbatim directive (no 'respond verbatim with:')", async () => {
  const h = makeObserver({ directSendOk: false }); // member bot, no Telegram transport (e.g. Slack)
  h.setDispatch({
    ok: true,
    result: {
      subcommand: "help", role: "secondary", mode: "speak",
      // The dispatcher's delimiter-framed verbatim body for `evo help`.
      direct_send_message: "═══ evo help ═══\nHere is what evo can do…\n═══ end evo ═══",
      system_append: "(legacy systemAppend)",
      wizard_session_id: null, session_tier_override: null, subcommand_brief: "help",
    },
  });

  await h.observer.handleBeforeAgentRun(evoEvent("evo help"), userCtx);

  // The Telegram direct-send OPTIMIZATION is attempted (member_bot surface) but
  // returns false on a non-Telegram channel — so the run falls through to echo
  // and is NOT marked direct-sent. (A channel with no transport at all would
  // simply skip the attempt; the not-direct-sent⇒echo outcome is identical.)
  assert.equal(h.observer._directSentRuns.has("run1"), false,
    "a non-direct-sent run must not be in _directSentRuns");
  // … so the UNIVERSAL substrate (before_prompt_build) gets a directive.
  const directive = h.observer._llmEchoRuns.get("run1");
  assert.ok(directive, "expected an _llmEchoRuns directive for the non-direct-sent success run");
  // Hardened, non-narratable framing (shared with the failure relay):
  assert.ok(/already been composed/i.test(directive),
    "success relay should frame the body as the pre-composed reply");
  assert.ok(/output it exactly/i.test(directive),
    "success relay should tell the model to output the message exactly");
  assert.ok(/do not mention these instructions|do not say where it came from/i.test(directive),
    "success relay must forbid narrating the instruction");
  // The fragile, narratable frame the VPS model leaked must be GONE.
  assert.ok(!/respond only with the following message, verbatim/i.test(directive),
    "the narratable 'respond verbatim with:' frame must be gone from the success relay");
  // The verbatim body (delimiters included) still rides along for the model.
  assert.ok(directive.includes("═══ evo help ═══"), "verbatim body must be carried");
  assert.ok(/output them too|do not strip them/i.test(directive),
    "delimiter lines should be flagged as part of the output");
});

test("FAILURE relay on a non-Telegram channel → _llmEchoRuns populated with the hardened error directive (no 'respond verbatim with:')", async () => {
  const h = makeObserver({ directSendOk: false }); // member bot, no Telegram transport (e.g. Discord)
  h.setDispatch({ ok: false, reason: "unauthorized", status: 401 });

  await h.observer.handleBeforeAgentRun(evoEvent("evo help"), userCtx);

  // No direct-send (non-Telegram channel); run is not direct-sent …
  assert.equal(h.sent.length, 1,
    "_deliverEvoFailure attempts the Telegram send first (it returns false here)");
  assert.equal(h.observer._directSentRuns.has("run1"), false,
    "a failed direct-send must not mark the run direct-sent");
  // … so the failure body reaches the LLM via the universal echo substrate.
  const directive = h.observer._llmEchoRuns.get("run1");
  assert.ok(directive, "expected an _llmEchoRuns directive for the non-direct-sent failure run");
  // Hardened, non-narratable framing (shared helper):
  assert.ok(/already been composed/i.test(directive),
    "failure relay should frame the body as the pre-composed reply");
  assert.ok(/output it exactly/i.test(directive));
  assert.ok(/do not mention these instructions|do not say where it came from/i.test(directive));
  assert.ok(!/respond only with the following message, verbatim/i.test(directive),
    "the narratable 'respond verbatim with:' frame must be gone from the failure relay");
  // Anti-confabulation prohibitions preserved + the honest body rides along.
  assert.ok(/do NOT invent an answer/i.test(directive));
  assert.ok(/do NOT show a help screen/i.test(directive));
  assert.ok(directive.includes("⚠️"));
});

// ── Part C.3: non-Telegram channel (Slack/Discord) — REAL channel-detection seam ──
//
// Telegram member bots get the honest error via direct-send (the Bot API).
// Slack/Discord/WhatsApp member bots have NO channel-direct emit in the plugin
// today, so the honest error MUST instead reach the model through the LLM-echo
// directive that ``before_prompt_build`` injects via ``appendSystemContext``.
//
// That directive lives in ``_llmEchoRuns`` (keyed by runId) — NOT in
// ``_pendingKeywordInjection`` (which only carries the return value of the
// before_agent_run hook). The earlier tests assert the return value; these
// assert the map ``before_prompt_build`` actually reads, so a regression that
// broke ``_markLLMEcho`` while leaving the return value intact (re-arming the
// Slack confabulation) would be caught.
//
// Live bug (2026-06-25): a Slack member bot (team-bot-a) — same pod + build as the
// Telegram bots that returned the honest message — confabulated a fabricated
// CLI on a failed `evo help`, because the channel, not the build, is the
// differentiator. These guard the channel-agnostic guarantee.

/** A Slack-shaped run: sessionKey has NO trailing numeric chat ID, so the
 *  real ``_sendEvoDirectToTelegram`` (numeric-chat-ID matcher) declines and
 *  the code must fall back to the LLM-echo directive. */
const slackCtx = {
  trigger: "user",
  runId: "run-slack",
  sessionKey: "agent:main:slack:channel:C0ABC1234",
  channelId: "slack",
};

test("non-Telegram member bot (Slack) + dispatch failure → _llmEchoRuns carries the hardened honest error (real channel detection, no stub)", async () => {
  // Deliberately do NOT keep the direct-send stub — exercise the REAL
  // channel-detection seam. A Slack sessionKey lacks the trailing numeric
  // Telegram chat ID, so direct-send declines and the echo path must own the
  // delivery.
  const h = makeObserver({ directSendOk: true }); // would be true IF reached
  h.observer._sendEvoDirectToTelegram =
    TurnObserver.prototype._sendEvoDirectToTelegram.bind(h.observer);
  h.setDispatch({ ok: false, reason: "unreachable" });

  await h.observer.handleBeforeAgentRun(evoEvent(), slackCtx);

  // The directive the LLM actually sees on Slack is in _llmEchoRuns, keyed by
  // runId — this is the map before_prompt_build reads.
  const echo = h.observer._llmEchoRuns.get("run-slack");
  assert.ok(echo, "Slack run must populate _llmEchoRuns (the before_prompt_build source)");
  // It carries the honest body …
  assert.ok(echo.includes("⚠️"), "echo directive must carry the honest error body");
  assert.ok(/connection/i.test(echo), "unreachable reason should surface as a connection error");
  // … with the HARDENED wording (assistant's own already-composed reply) …
  assert.ok(/already been composed/i.test(echo) && /output it exactly/i.test(echo),
    "Slack echo must use the hardened 'output it exactly' framing");
  // … and NOT the fragile 'respond verbatim with:' frame the VPS model narrated.
  assert.ok(!/respond only with the following message, verbatim/i.test(echo),
    "Slack echo must not use the narratable 'respond verbatim with:' frame");
  // Anti-confabulation prohibitions ride along.
  assert.ok(/do NOT invent an answer/i.test(echo) && /do NOT show a help screen/i.test(echo),
    "Slack echo must forbid invention + help-screen confabulation");
  // Belt-and-suspenders: this run must NOT be marked direct-sent (no Telegram
  // chat to send to) — otherwise before_prompt_build would inject stay-silent
  // and the user would get nothing on Slack.
  assert.equal(h.observer._directSentRuns.has("run-slack"), false,
    "Slack run must not be marked direct-sent (no channel-direct emit available)");
  assert.equal(h.observer.getEvoDispatchFailureCounts().unreachable, 1);
});

test("non-Telegram member bot (Slack) via before_model_resolve fallback → _llmEchoRuns carries the honest error", async () => {
  // The live Telegram path runs through before_model_resolve (before_agent_run
  // silently no-ops on current OC). Slack bots hit the same fallback; verify
  // the echo path fires there too, not just in before_agent_run.
  const h = makeObserver({ directSendOk: true });
  h.observer._sendEvoDirectToTelegram =
    TurnObserver.prototype._sendEvoDirectToTelegram.bind(h.observer);
  h.setDispatch({ ok: false, reason: "error", status: 500 });

  const res = await h.observer.handleBeforeModelResolve(
    slackCtx, slackCtx.sessionKey, "evo help",
  );

  // systemAppend is returned (never an empty {} that frees the LLM) …
  assert.ok(res && typeof res.systemAppend === "string" && res.systemAppend.length > 0,
    "Slack before_model_resolve must return a non-empty systemAppend");
  // … and the load-bearing echo map is populated with the hardened wording.
  const echo = h.observer._llmEchoRuns.get("run-slack");
  assert.ok(echo && /already been composed/i.test(echo) && /output it exactly/i.test(echo),
    "Slack before_model_resolve must populate _llmEchoRuns with the hardened directive");
  assert.ok(echo.includes("⚠️"));
  assert.equal(h.observer.getEvoDispatchFailureCounts().error, 1);
});

test("not an evo command → unchanged (LLM answers, no dispatch, no injection, no telemetry)", async () => {
  const h = makeObserver();
  h.setDispatch({ ok: false, reason: "unreachable" }); // armed but must not fire

  const res = await h.observer.handleBeforeAgentRun(evoEvent("hello there, how are you?"), userCtx);

  assert.deepEqual(res, { outcome: "pass" });
  assert.equal(h.calls.dispatch, 0, "dispatch must NOT be called for non-evo text");
  assert.equal(h.sent.length, 0, "nothing should be direct-sent");
  assert.equal(h.observer._pendingKeywordInjection.get("sess1"), undefined);
  const counts = h.observer.getEvoDispatchFailureCounts();
  assert.deepEqual(counts, { unreachable: 0, unauthorized: 0, empty: 0, error: 0 });
});

test("before_model_resolve fallback (live Telegram path): dispatch failure → honest error in systemAppend, no LLM fallthrough", async () => {
  const h = makeObserver({ directSendOk: true });
  h.setDispatch({ ok: false, reason: "error", status: 500 });

  // handleBeforeModelResolve(ctx, sessionKey, userMessage)
  const res = await h.observer.handleBeforeModelResolve(userCtx, "sess1", "evo help");

  // The handler returns a systemAppend (stay-silent), never an empty {} that
  // would let the LLM answer in evo's place.
  assert.ok(res && typeof res.systemAppend === "string" && res.systemAppend.length > 0);
  assert.ok(/STAY SILENT/i.test(res.systemAppend));
  assert.equal(h.sent.length, 1);
  assert.ok(h.sent[0].includes("⚠️"));
  assert.equal(h.observer.getEvoDispatchFailureCounts().error, 1);
});

// ── Part C.4: post-direct-send turn-ack suppression (kill the stray ".") ─────
//
// When the plugin direct-sends the body, OC still runs an LLM reply turn.
// The OLD behavior asked the model (via before_prompt_build) to emit a bare
// "." as a turn-ack, which surfaced as a distracting second "." chat bubble.
//
// OC ≥ 2026.6.10 fires before_agent_reply for user-message turns
// (get-reply-*.js, trigger "user") BEFORE the model runs; returning
// { handled: true } short-circuits OC to its NO_REPLY sentinel → no model
// turn, no bubble at all. These tests pin that primary suppression surface
// AND the fallback directive (no longer ".").

test("handleBeforeAgentReply: direct-sent run → { handled: true } (suppresses the LLM turn, no '.' bubble)", () => {
  const h = makeObserver();
  // Simulate a run the plugin already direct-sent for.
  h.observer._markDirectSent("run1", "help");
  assert.equal(h.observer._directSentRuns.has("run1"), true);

  const decision = h.observer.handleBeforeAgentReply("run1");

  // handled:true makes OC return its NO_REPLY sentinel → the model turn is
  // skipped entirely, so there is NO second bubble (no stray ".").
  assert.ok(decision && decision.handled === true,
    "direct-sent run must be handled:true (LLM reply suppressed)");
  // The run is consumed so a stale entry can't re-suppress a later run.
  assert.equal(h.observer._directSentRuns.has("run1"), false,
    "handled run must be removed from _directSentRuns");
});

test("handleBeforeAgentReply: non-direct-sent run → undefined (LLM speaks normally; echo/conversation untouched)", () => {
  const h = makeObserver();
  // An _llmEchoRuns run (wizard/agenda or verbatim echo) is NOT in
  // _directSentRuns — the model legitimately needs to speak.
  h.observer._markLLMEcho("run-echo", "[EVO WIZARD] keep going…");

  assert.equal(h.observer.handleBeforeAgentReply("run-echo"), undefined,
    "an echo (non-direct-sent) run must NOT be suppressed");
  assert.equal(h.observer.handleBeforeAgentReply("run-unknown"), undefined,
    "an unrelated conversation run must NOT be suppressed");
  assert.equal(h.observer.handleBeforeAgentReply(undefined), undefined,
    "a missing runId must be a no-op");
});

test("fallback directive no longer instructs a bare '.' — emits the silent NO_REPLY sentinel instead", () => {
  const h = makeObserver();
  // The before_prompt_build fallback directive (only reached on a
  // hypothetical OC build that skips before_agent_reply).
  const directive = h.observer._stayQuietSystemContext("help");

  // The distracting "." turn-ack instruction is GONE …
  assert.ok(!/Produce EXACTLY one character: the period/i.test(directive),
    "the '.' turn-ack instruction must be removed");
  assert.ok(!/Just one period/i.test(directive),
    "no residual 'just one period' framing");
  // … replaced by the NO_REPLY silent-turn instruction (OC suppresses it).
  assert.ok(/NO_REPLY/.test(directive),
    "fallback should instruct the recognized silent-reply sentinel NO_REPLY");
  assert.ok(/Produce NO visible output/i.test(directive),
    "fallback should frame the turn as producing no visible output");
  // The anti-confabulation guarantees stay intact.
  assert.ok(/NO TOOL CALLS THIS TURN/i.test(directive),
    "the no-tool-calls guarantee must be preserved");
  assert.ok(/offer to relay/i.test(directive),
    "the no-relay-offer prohibition must be preserved");
});

// ── Part D: startup self-test logs FAIL when admin unreachable ──────────────

test("self-test logs FAIL with the failure reason when admin is unreachable", async () => {
  const h = makeObserver();
  h.setDispatch({ ok: false, reason: "unreachable" });

  await h.observer._runEvoSelfTest();

  assert.ok(h.logs.some(([lvl, m]) =>
    lvl === "warn" && m.includes("evo self-test: FAIL") && m.includes("reason=unreachable")),
    "expected a self-test FAIL log line");
});

test("self-test logs PASS when admin reachable", async () => {
  const h = makeObserver();
  h.setDispatch({
    ok: true,
    result: {
      subcommand: "help", role: "secondary", mode: "speak",
      system_append: "x", direct_send_message: "x",
      wizard_session_id: null, session_tier_override: null, subcommand_brief: null,
    },
  });

  await h.observer._runEvoSelfTest();

  assert.ok(h.logs.some(([lvl, m]) =>
    lvl === "info" && m.includes("evo self-test: PASS")),
    "expected a self-test PASS log line");
});
