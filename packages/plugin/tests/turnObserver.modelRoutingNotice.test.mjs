/**
 * Tests for the two halves of model-routing attribution:
 *
 *   - _buildRoutingNotice — the USER-visible line that REPLACES OC's
 *     fallback banner (via reply_payload_sending) on any turn Evolve routed.
 *   - _buildCostDowngradeNotice — the BOT-visible note injected (via
 *     before_prompt_build → appendSystemContext) on the narrower set of turns
 *     a cost safety net forced down (spend_cap / runaway).
 *
 * Incident 2026-07-31 (reference pod): a bot's daily cost breaker tripped,
 * ModelRouter overrode the next user turn from the sonnet primary to haiku,
 * and OC rendered "Model Fallback: … (selected …; selected model
 * unavailable)". The reason is false — OC builds fallback reasons only from
 * provider-failure attempts, and a hook override has none, so the default
 * "selected model unavailable" text claims a provider outage that never
 * happened. Recurred 2026-09-04 in the other direction on a reference-pod
 * bot whose operator default routed every session UP to the power role: same
 * banner, same false reason, no cost breaker in sight. The gateway offers no
 * reason/label field on the before_model_resolve result
 * (mergeBeforeModelResolve keeps only modelOverride/providerOverride), so the
 * banner is corrected by replacing the payload text outright.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.modelRoutingNotice.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import fs from "node:fs";
import path from "node:path";

import {
  _buildCostDowngradeNotice,
  _buildRoutingNotice,
  _fallbackNoticeActiveModel,
  _isFallbackNoticePayload,
  _sameModelRef,
  TurnObserver,
} from "../dist/observer/TurnObserver.js";
import { buildPrefixHashRecord } from "../dist/observer/PrefixHashLedger.js";

test("spend_cap notice names the model and the daily cap", () => {
  const note = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  assert.match(note, /^\[EVOLVE COST DOWNGRADE\]/);
  assert.match(note, /anthropic\/claude-haiku-4-5/);
  assert.match(note, /daily spending cap/);
  assert.match(note, /NOT a provider outage/);
});

test("runaway notice names the runaway-rate cap, not the daily cap", () => {
  const note = _buildCostDowngradeNotice("runaway", "anthropic/claude-haiku-4-5");
  assert.match(note, /runaway-rate cost cap/);
  assert.doesNotMatch(note, /daily spending cap/);
});

test("notice tells the bot where to attribute the change, and where not to", () => {
  // The bot cannot observe its own routing. Without this it answers "why are
  // you on Haiku?" by guessing, and a provider outage is the obvious guess.
  const note = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  assert.match(note, /cost cap/);
  assert.match(note, /Do not speculate about provider availability/);
});

test("notice is deterministic for identical inputs (prompt-cache stability)", () => {
  // before_prompt_build may fire more than once for a run (rebuilds);
  // the entry is kept in _evolveRoutedRuns so re-injection must be
  // byte-identical.
  const a = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  const b = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  assert.equal(a, b);
});

// ── Prefix-hash ledger carries the new block ────────────────────────────────

test("prefix-hash record attributes the costDowngrade block", () => {
  const note = _buildCostDowngradeNotice("spend_cap", "anthropic/claude-haiku-4-5");
  const rec = buildPrefixHashRecord({
    botId: "test-bot",
    sessionId: "s1",
    turnId: "t1",
    path: "blocks",
    combined: note,
    blocks: { costDowngrade: note },
  });
  assert.equal(typeof rec.appended_block_shas.cost_downgrade, "string");
  assert.equal(rec.appended_block_shas.capabilities, null);
});

test("prefix-hash record without a downgrade records null (absent, not empty)", () => {
  const rec = buildPrefixHashRecord({
    botId: "test-bot",
    sessionId: "s1",
    turnId: "t1",
    path: "blocks",
    combined: "cap-block",
    blocks: { capabilities: "cap-block", costDowngrade: "" },
  });
  assert.equal(rec.appended_block_shas.cost_downgrade, null);
});

// ── The two-hook runId round-trip ───────────────────────────────────────────
//
// Everything above tests the pure builder. The mechanism that actually
// delivers the notice is a handshake across TWO hooks on ONE run:
//
//   before_model_resolve → resolveModelRouting() stores
//     _evolveRoutedRuns[String(ctx.runId)] = {driver, model}
//   before_prompt_build  → reads _evolveRoutedRuns[String(ctx.runId)] and
//     returns the notice in appendSystemContext
//
// A pure-builder suite cannot see that handshake break: a runId that arrives
// in a different shape (or not at all) on one of the two hooks silently drops
// the notice while every string assertion above still passes. These tests
// drive both hooks with the ctx shape OC 2026.7.1-2 actually passes (captured
// from a live gateway: event={prompt}, ctx={runId,jobId,agentId,sessionKey,
// sessionId,workspaceDir,modelProviderId,modelId,trigger,channel,...}) and
// assert the notice lands.

const LIVE_RUN_ID = "fb228b64-734a-4beb-8dce-ccaf7b823b4a";
const LIVE_SESSION_ID = "bb73ee93-1350-409b-90dd-ba47e6e9d025";
const DOWNGRADE_MODEL = "anthropic/claude-haiku-4-5";

/**
 * TurnObserver with hooks registered against a fake plugin api, a scripted
 * ModelRouter, and the two IO-bound prompt blocks stubbed out (they shell out
 * to Python / the admin socket and soft-fail to "" in production anyway) so
 * the appended context is exactly the block under test.
 */
function makeHookHarness({
  driver = "spend_cap", model = DOWNGRADE_MODEL, role = "fast",
} = {}) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-costdowngrade-"));
  const logs = [];
  const logger = {
    info: (m) => logs.push(["info", String(m)]),
    warn: (m) => logs.push(["warn", String(m)]),
    error: (m) => logs.push(["error", String(m)]),
    debug: () => {},
  };
  const config = {
    botId: "team-bot-c",
    role: "member",
    networkId: "n",
    sharedDir: shared,
    tier: "full",
    capabilities: {
      observer: true, injectPodConduct: true, injectKeywords: false,
      modelRouting: true, deferTool: false, recordApplicationTool: false,
    },
    tierClassification: "session",
    enableLLMSummarization: false,
    minTurns: 1,
    keywordConfidenceThreshold: 0.7,
  };
  const observer = new TurnObserver(config, logger, undefined);

  // Scripted router: a cost breaker has tripped, so every resolve forces the
  // fast rung and reports the safety-net driver.
  observer.modelRouter = {
    setUserTier: () => {},
    setSessionUserKey: () => {},
    setSessionType: () => {},
    getSessionType: () => "conversation",
    isSpendCapForced: () => driver === "spend_cap",
    resolveModelOverride: () => model,
    resolveAuthProfileOverride: () => null,
    getLastDecisionDriver: () => driver,
    getRoleForModel: () => role,
  };

  // Subprocess / socket blocks — stubbed so the assertion is about the
  // downgrade block, not about the test host having a pod on it.
  observer._renderCapabilitiesBlock = async () => "";
  observer._renderDirectoryDigestBlock = async () => "";

  const hooks = new Map();
  const api = {
    on: (name, handler) => {
      if (!hooks.has(name)) hooks.set(name, []);
      hooks.get(name).push(handler);
    },
    registerHook: () => {},
  };
  observer.register(api);

  /** Fire every handler registered for a hook; return the last non-undefined result. */
  const fire = async (name, event, ctx) => {
    let out;
    for (const handler of hooks.get(name) ?? []) {
      const r = await handler(event, ctx);
      if (r !== undefined) out = r;
    }
    return out;
  };

  return { observer, logs, hooks, fire };
}

/** The ctx OC hands both hooks for one channel turn (keys as logged live). */
function liveCtx(runId = LIVE_RUN_ID) {
  return {
    runId,
    jobId: "job-1",
    agentId: "main",
    sessionKey: "agent:main:telegram:dm:1000000001",
    sessionId: LIVE_SESSION_ID,
    workspaceDir: "/Users/team-bot-c/.openclaw/workspace",
    modelProviderId: "anthropic",
    modelId: "claude-sonnet-5",
    trigger: "user",
    channel: "telegram",
    messageProvider: "telegram",
    channelId: "1000000001",
  };
}

test("round-trip: the run marked by before_model_resolve gets the notice at before_prompt_build", async () => {
  const h = makeHookHarness();
  const ctx = liveCtx();

  const routed = await h.fire("before_model_resolve", { prompt: "xplay" }, ctx);
  // Step 1 fired: the override is emitted as a coherent provider/model pair.
  assert.equal(routed.modelId ?? routed.modelOverride, "claude-haiku-4-5");

  const built = await h.fire(
    "before_prompt_build", { prompt: "xplay", messages: [] }, ctx,
  );
  // Step 2: the notice is in the system context OC appends to the prompt.
  assert.ok(built?.appendSystemContext, "expected appendSystemContext");
  assert.ok(
    built.appendSystemContext.includes(
      _buildCostDowngradeNotice("spend_cap", DOWNGRADE_MODEL),
    ),
    "cost-downgrade notice missing from appendSystemContext",
  );
});

test("round-trip: runaway driver rides the same handshake", async () => {
  const h = makeHookHarness({ driver: "runaway" });
  const ctx = liveCtx();
  await h.fire("before_model_resolve", { prompt: "hi" }, ctx);
  const built = await h.fire("before_prompt_build", { prompt: "hi", messages: [] }, ctx);
  assert.match(built.appendSystemContext, /runaway-rate cost cap/);
});

test("round-trip: a numeric runId still matches (String() on both sides)", async () => {
  // The marker is keyed String(ctx.runId) and read back the same way. A hook
  // payload that carries a numeric run id must not fall through the crack.
  const h = makeHookHarness();
  const ctx = liveCtx(12345);
  await h.fire("before_model_resolve", { prompt: "hi" }, ctx);
  const built = await h.fire("before_prompt_build", { prompt: "hi", messages: [] }, ctx);
  assert.match(built.appendSystemContext, /EVOLVE COST DOWNGRADE/);
});

test("a different run (e.g. a subagent lane) does NOT inherit the notice", async () => {
  const h = makeHookHarness();
  await h.fire("before_model_resolve", { prompt: "xplay" }, liveCtx());
  const other = await h.fire(
    "before_prompt_build",
    { prompt: "sub", messages: [] },
    liveCtx("11111111-2222-3333-4444-555555555555"),
  );
  assert.ok(
    !other || !/EVOLVE COST DOWNGRADE/.test(other.appendSystemContext ?? ""),
    "notice leaked to an unrelated run",
  );
});

test("a non-safety-net driver marks nothing (classifier downgrades are not cost downgrades)", async () => {
  const h = makeHookHarness({ driver: "classifier" });
  const ctx = liveCtx();
  await h.fire("before_model_resolve", { prompt: "hi" }, ctx);
  const built = await h.fire("before_prompt_build", { prompt: "hi", messages: [] }, ctx);
  assert.ok(
    !built || !/EVOLVE COST DOWNGRADE/.test(built.appendSystemContext ?? ""),
    "classifier-driven routing must not claim a cost downgrade",
  );
});

test("a prompt rebuild on the same run re-injects byte-identically", async () => {
  // _evolveRoutedRuns deliberately keeps the entry after consumption: OC may
  // build the prompt more than once per run, and a block that appears then
  // vanishes invalidates the whole prompt cache twice.
  const h = makeHookHarness();
  const ctx = liveCtx();
  await h.fire("before_model_resolve", { prompt: "xplay" }, ctx);
  const first = await h.fire("before_prompt_build", { prompt: "xplay", messages: [] }, ctx);
  const second = await h.fire("before_prompt_build", { prompt: "xplay", messages: [] }, ctx);
  assert.equal(first.appendSystemContext, second.appendSystemContext);
});

// ── The routing notice (reply_payload_sending) ─────────────────────────────
//
// The user-facing half. OC emits its fallback banner as its OWN reply payload
// (isFallbackNotice: true), so the notice replaces that payload's text rather
// than adding a message: same slot, same cadence (one per fallback
// TRANSITION), one true sentence instead of a false one.

/** OC's fallback-notice payload, as agent-runner.runtime composes it. */
function bannerPayload(text) {
  return {
    text: text ?? "↪️ Model Fallback: anthropic/claude-haiku-4-5 " +
      "(selected anthropic/claude-sonnet-5; selected model unavailable)",
    isFallbackNotice: true,
  };
}

test("notice names the model and the cause, per driver", () => {
  assert.match(
    _buildRoutingNotice("spend_cap", DOWNGRADE_MODEL, "fast"),
    /daily spending cap/,
  );
  assert.match(
    _buildRoutingNotice("runaway", DOWNGRADE_MODEL, "fast"),
    /runaway-rate cost cap/,
  );
  assert.match(
    _buildRoutingNotice("operator_default", "anthropic/claude-opus-5", "power"),
    /this bot defaults to the power tier/,
  );
  assert.match(
    _buildRoutingNotice("user_default", "anthropic/claude-opus-5", "power"),
    /your default is the power tier/,
  );
  assert.match(
    _buildRoutingNotice("user_request", "anthropic/claude-opus-5", "power"),
    /you asked for the power tier/,
  );
  // Anything else — cascade, preflight, classifier, or a driver the router
  // did not record — states the routing without inventing a cause for it.
  for (const driver of ["cascade", "preflight", "classifier", null]) {
    assert.match(
      _buildRoutingNotice(driver, "anthropic/claude-opus-5", "power"),
      /Evolve routed this session to the power tier/,
    );
  }
  // Every variant names the model it is about.
  assert.ok(_buildRoutingNotice("spend_cap", DOWNGRADE_MODEL, "fast")
    .includes(DOWNGRADE_MODEL));
  assert.ok(_buildRoutingNotice("operator_default", "anthropic/claude-opus-5", "power")
    .includes("anthropic/claude-opus-5"));
});

test("cap-degrade notice names the tier whose budget ran out", () => {
  // The one driver that needs two roles to read correctly: "you are on
  // standard" is not the interesting half — "the power budget is gone" is.
  const notice = _buildRoutingNotice(
    "role_cap", "anthropic/claude-sonnet-5", "standard", "power",
  );
  assert.match(notice, /daily power-tier limit is used up/);
  assert.match(notice, /on the standard tier/);
  assert.ok(notice.includes("anthropic/claude-sonnet-5"));
  assert.doesNotMatch(notice, /unavailable|failed|outage/i);
  // Degrade-from missing (a marker written before the getter existed, or a
  // router that did not record it) still produces a true sentence.
  assert.match(
    _buildRoutingNotice("role_cap", "anthropic/claude-sonnet-5", "standard", null),
    /daily limit for its usual tier is used up/,
  );
});

test("notice degrades to a model-only sentence when the role is unknown", () => {
  // getRoleForModel returns null for a model outside the rung catalog (a
  // hand-pinned override, a rung mid-edit). Naming no tier beats naming a
  // wrong one.
  for (const driver of ["operator_default", "user_default", "user_request", null]) {
    const notice = _buildRoutingNotice(driver, "anthropic/claude-opus-5", null);
    assert.ok(notice.includes("anthropic/claude-opus-5"));
    assert.doesNotMatch(notice, /the null tier|undefined/);
  }
});

test("notice never claims a failure, an outage, or an unavailable model", () => {
  // The whole point: this text replaces a sentence that said all three.
  for (const driver of ["spend_cap", "runaway", "operator_default", "user_request",
                        "user_default", "cascade", "role_cap", null]) {
    const notice = _buildRoutingNotice(driver, "anthropic/claude-opus-5", "power", "max");
    assert.doesNotMatch(notice, /unavailable|fallback|failed|outage|error/i);
  }
});

test("notice is one line — it replaces a banner, not a conversation", () => {
  assert.ok(!_buildRoutingNotice("operator_default", "anthropic/claude-opus-5", "power")
    .includes("\n"));
});

test("notice is deterministic for identical inputs", () => {
  assert.equal(
    _buildRoutingNotice("spend_cap", DOWNGRADE_MODEL, "fast"),
    _buildRoutingNotice("spend_cap", DOWNGRADE_MODEL, "fast"),
  );
});

test("banner parsing: the ACTIVE model, not the selected one", () => {
  assert.equal(
    _fallbackNoticeActiveModel(bannerPayload().text),
    "anthropic/claude-haiku-4-5",
  );
  // Emoji and variation selector are both optional (same reason
  // _isFallbackNoticePayload tolerates them).
  assert.equal(
    _fallbackNoticeActiveModel("Model Fallback: xai/grok-4 (selected anthropic/claude-sonnet-5; boom)"),
    "xai/grok-4",
  );
  // Unparseable → null, which the handler treats as "leave OC's banner alone".
  assert.equal(_fallbackNoticeActiveModel("Model Fallback: no parens here"), null);
  assert.equal(_fallbackNoticeActiveModel(""), null);
});

test("model-ref comparison: exact, bare, and the negative", () => {
  assert.equal(_sameModelRef("anthropic/claude-opus-5", "anthropic/claude-opus-5"), true);
  assert.equal(_sameModelRef("Anthropic/Claude-Opus-5", "anthropic/claude-opus-5"), true);
  // A rung configured without its provider prefix: splitProviderModelRef
  // paired it with the lane's provider, so OC renders more than we stored.
  assert.equal(_sameModelRef("anthropic/claude-opus-5", "claude-opus-5"), true);
  assert.equal(_sameModelRef("anthropic/claude-opus-5", "anthropic/claude-sonnet-5"), false);
  assert.equal(_sameModelRef("", "anthropic/claude-opus-5"), false);
});

test("fallback-notice detection: flag, text prefix, and the negatives", () => {
  assert.equal(_isFallbackNoticePayload(bannerPayload()), true);
  // Flag dropped by a future gateway → the text prefix still identifies it,
  // with or without the emoji's variation selector.
  assert.equal(_isFallbackNoticePayload(
    { text: "↪️ Model Fallback: x (selected y; selected model unavailable)" }), true);
  assert.equal(_isFallbackNoticePayload({ text: "Model Fallback: x (selected y)" }), true);
  // An ordinary reply — including one that merely talks about models.
  assert.equal(_isFallbackNoticePayload(
    { text: "I switched to a different model fallback earlier." }), false);
  assert.equal(_isFallbackNoticePayload({ text: "" }), false);
  assert.equal(_isFallbackNoticePayload(null), false);
});

test("the banner for a routed run is REPLACED, not annotated", async () => {
  const h = makeHookHarness();
  const ctx = liveCtx();
  await h.fire("before_model_resolve", { prompt: "xplay" }, ctx);

  const banner = bannerPayload();
  const out = await h.fire("reply_payload_sending", {
    payload: banner, kind: "block", channel: "telegram",
    sessionKey: ctx.sessionKey, runId: LIVE_RUN_ID,
  });

  assert.ok(out?.payload, "expected a rewritten payload");
  assert.equal(
    out.payload.text,
    _buildRoutingNotice("spend_cap", DOWNGRADE_MODEL, "fast"),
  );
  // The false claim is GONE, not buried under a correction.
  assert.doesNotMatch(out.payload.text, /selected model unavailable/);
  // The flag rides along so OC's downstream handling is unchanged.
  assert.equal(out.payload.isFallbackNotice, true);
  // Never mutates OC's object in place.
  assert.match(banner.text, /selected model unavailable/);
});

test("escalation: an operator default that routes UP says so", async () => {
  // 2026-09-04: a bot's evolve-tiers.json set userTierOverride.defaultTier
  // = power, so every session ran on opus while openclaw.json still declared
  // sonnet-5 — and every session opened with a banner claiming sonnet-5 was
  // unavailable. Nothing had failed; nothing was unavailable.
  const h = makeHookHarness({
    driver: "operator_default", model: "anthropic/claude-opus-5", role: "power",
  });
  const ctx = liveCtx();
  await h.fire("before_model_resolve", { prompt: "hi" }, ctx);
  const out = await h.fire("reply_payload_sending", {
    payload: bannerPayload("↪️ Model Fallback: anthropic/claude-opus-5 " +
      "(selected anthropic/claude-sonnet-5; selected model unavailable)"),
    kind: "block", runId: LIVE_RUN_ID,
  });
  assert.equal(
    out.payload.text,
    "↪️ Model: anthropic/claude-opus-5 — this bot defaults to the power tier.",
  );
});

test("a real fallback PAST our override keeps OC's banner", async () => {
  // The interlock. OC re-fires before_model_resolve on every failover attempt
  // and resolveModelRouting stands down on the repeat, so the run stays marked
  // while the turn lands somewhere else entirely. Rewriting there would
  // replace a TRUE outage report with a routing claim — the same lie, pointed
  // the other way.
  const h = makeHookHarness({
    driver: "operator_default", model: "anthropic/claude-opus-5", role: "power",
  });
  const ctx = liveCtx();
  await h.fire("before_model_resolve", { prompt: "hi" }, ctx);
  const out = await h.fire("reply_payload_sending", {
    payload: bannerPayload("↪️ Model Fallback: xai/grok-4 " +
      "(selected anthropic/claude-sonnet-5; HTTP 529)"),
    kind: "block", runId: LIVE_RUN_ID,
  });
  assert.equal(out, undefined, "a provider failure's banner must survive");
  assert.ok(
    h.logs.some(([lvl, m]) => lvl === "info" && /delivering OC's banner unchanged/.test(m)),
    "the stand-down must leave the operator evidence",
  );
});

test("the bot's own reply payloads are untouched", async () => {
  const h = makeHookHarness();
  const ctx = liveCtx();
  await h.fire("before_model_resolve", { prompt: "xplay" }, ctx);
  const out = await h.fire("reply_payload_sending", {
    payload: { text: "I'm running into issues with the crossplay_coach script." },
    kind: "final", runId: LIVE_RUN_ID,
  });
  assert.equal(out, undefined);
});

test("one notice per run, however many payloads the turn emits", async () => {
  const h = makeHookHarness();
  const ctx = liveCtx();
  await h.fire("before_model_resolve", { prompt: "xplay" }, ctx);
  const first = await h.fire("reply_payload_sending",
    { payload: bannerPayload(), kind: "block", runId: LIVE_RUN_ID });
  const second = await h.fire("reply_payload_sending",
    { payload: bannerPayload(), kind: "block", runId: LIVE_RUN_ID });
  assert.ok(first?.payload);
  assert.equal(second, undefined, "a second payload must not re-rewrite");
});

test("a banner on a run Evolve did NOT route is delivered unchanged", async () => {
  // No before_model_resolve marker: whatever moved the model, it was not us,
  // so we have nothing truer to say than OC does.
  const h = makeHookHarness();
  const out = await h.fire("reply_payload_sending",
    { payload: bannerPayload(), kind: "block", runId: LIVE_RUN_ID });
  assert.equal(out, undefined);
});

test("a banner with no runId (unplumbed outbound path) is delivered unchanged", async () => {
  // message_sending/message_sent do not carry the run id; if a future OC
  // fires reply_payload_sending without one, correlation is impossible and
  // the correct answer is to do nothing rather than guess.
  const h = makeHookHarness();
  await h.fire("before_model_resolve", { prompt: "xplay" }, liveCtx());
  const out = await h.fire("reply_payload_sending",
    { payload: bannerPayload(), kind: "block" });
  assert.equal(out, undefined);
});
