/**
 * App routes: an installed app's shortcut is answered by the app — two
 * bounded model calls, no session turn.
 *
 * Brief: internal/dispatch/done/crossplay-coach-as-app.md. One `xplay`
 * screenshot cost ~$2.35 and ten minutes as a session tool loop (13 model
 * calls, 17 tool calls; internal/finding-cost-forensics-power-bot-2026-09-04.md
 * §2), then $22.15 (118 calls) and $65.98 (304 calls). Here the vision call
 * and the commentary call are faked at the OC runtime seam; the app's REAL
 * route script (gallery/crossplay-coach/files/scripts/crossplay_route.py)
 * runs under python3 against a throwaway workspace, so the pipeline, the
 * saved-state diff and the reply format are exercised end to end.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/appRoutes.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  APP_ROUTES, CROSSPLAY_ROUTE, ROUTE_LIMITS, AppRouteHandler, acceptCallLine,
  checkRouteInstalled, estimateImageTokens, matchAppRoute, parseMediaRefs,
  resolveInboundImage, runAppRoute, openClawRouteRuntime,
} from "../dist/apps/AppRoutes.js";
import { TurnObserver, _manifestStatusAllowsTriggers } from "../dist/observer/TurnObserver.js";
import { ModelRouter } from "../dist/observer/ModelRouter.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(HERE, "../../../gallery/crossplay-coach");
const SCRIPTS = path.join(APP, "files", "scripts");
const VISION = path.join(APP, "tests", "fixtures", "vision");
const WORDS = [
  "AL", "YEZ", "DIVULGE", "JO", "THEORY", "ON", "CORBIES", "AXES", "VARY",
  "ADJOINED", "LION", "YET", "YETI", "MOUTHS", "TRACKERS", "NIQABS", "GIE",
  "HE", "OXEN", "WOWS", "WO", "AGOUTIES", "FACTIOUS", "HILLO",
];
const BOT = "word_bot";

const reading = (name) => JSON.parse(fs.readFileSync(path.join(VISION, name), "utf8"));
const sha = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");

/** A 2-pixel PNG header is all resolveInboundImage and the token estimate read. */
function pngBytes(width = 1170, height = 2532) {
  const buf = Buffer.alloc(64);
  buf.writeUInt32BE(0x89504e47, 0);
  buf.writeUInt32BE(0x0d0a1a0a, 4);
  buf.writeUInt32BE(13, 8);
  buf.write("IHDR", 12, "ascii");
  buf.writeUInt32BE(width, 16);
  buf.writeUInt32BE(height, 20);
  return buf;
}

/** A bot workspace with the app installed as the install spine leaves it. */
function makeWorkspace({ status = undefined, driftSha = false } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-approute-"));
  const state = path.join(root, "state");
  const ws = path.join(state, "workspace");
  fs.mkdirSync(path.join(ws, "scripts"), { recursive: true });
  for (const f of ["crossplay_coach.py", "crossplay_route.py", "crossplay_lexicon.json"]) {
    fs.copyFileSync(path.join(SCRIPTS, f), path.join(ws, "scripts", f));
  }
  fs.mkdirSync(path.join(ws, "crossplay-data"), { recursive: true });
  fs.writeFileSync(path.join(ws, "crossplay-data", "lexicon.txt"), WORDS.join("\n") + "\n");
  fs.mkdirSync(path.join(ws, "manifests"), { recursive: true });
  const files = ["scripts/crossplay_coach.py", "scripts/crossplay_route.py"].map((p) => ({
    path: p, sha256: driftSha ? "0".repeat(64) : sha(path.join(ws, p)),
  }));
  fs.writeFileSync(path.join(ws, "manifests", "crossplay-coach.json"), JSON.stringify({
    app_id: "crossplay-coach", ...(status ? { status } : {}), package: { files },
  }));
  fs.mkdirSync(path.join(state, "media", "inbound"), { recursive: true });
  const image = path.join(state, "media", "inbound", "board-1.png");
  fs.writeFileSync(image, pngBytes());
  return { root, state, ws, image };
}

const PROMPT = "Xplay game g\n[media attached: media://inbound/board-1.png (image/png) \"IMG_0001.png\"]";

/** The OC runtime seam, recording what each call was asked. */
function fakeApi(state, { visions = [], completion = "Call: HILLO on the 3W — 33 now and it keeps DT.", visionThrows = false, llmReasoningRejected = false } = {}) {
  const seen = { vision: [], llm: [] };
  const queue = [...visions];
  const api = {
    runtime: {
      config: { current: () => ({ fake: true }) },
      state: { resolveStateDir: () => state },
      mediaUnderstanding: {
        async extractStructuredWithModel(params) {
          seen.vision.push(params);
          if (visionThrows) throw new Error("provider 529");
          const next = queue.shift();
          return { text: JSON.stringify(next), parsed: next, provider: params.provider, model: params.model };
        },
      },
      llm: {
        async complete(params) {
          seen.llm.push({ ...params });
          if (llmReasoningRejected && params.reasoning !== undefined) {
            throw new Error('Thinking level "off" is not supported for x/y.');
          }
          return {
            text: completion, provider: "anthropic", model: "claude-sonnet-5",
            usage: { inputTokens: 612, outputTokens: 41, costUsd: 0.00163 },
          };
        },
      },
    },
  };
  return { api, seen };
}

const ctxFor = (w, extra = {}) => ({
  trigger: "user", runId: `run-${Math.random().toString(36).slice(2)}`,
  sessionId: "sess-main-1", sessionKey: "agent:main:telegram:direct:4242",
  workspaceDir: w.ws, modelProviderId: "anthropic", modelId: "claude-sonnet-5",
  senderId: "4242", channelId: "telegram", ...extra,
});

const quietLogger = () => {
  const lines = [];
  return { lines, info: (m) => lines.push(String(m)), warn: (m) => lines.push(String(m)), error: (m) => lines.push(String(m)), debug: () => {} };
};

// ── Matching ─────────────────────────────────────────────────────────────────

test("the shortcut with a screenshot matches; case and gateway envelopes do not matter", () => {
  const m = matchAppRoute(PROMPT);
  assert.equal(m.spec.appId, "crossplay-coach");
  assert.deepEqual(m.args, { gameId: "g", fresh: false });
  assert.deepEqual(m.mediaRefs, ["media://inbound/board-1.png"]);
  const stamped = "[media attached: /x/state/media/inbound/b.jpg (image/jpeg)]\n[Sat 2026-09-20 20:05 UTC] xplay fresh";
  assert.deepEqual(matchAppRoute(stamped).args, { gameId: "default", fresh: true });
  // OC puts system events and thread context ahead of the user's text.
  const withEvents = "[media attached: media://inbound/c.jpg (image/jpeg)]\nSystem: [2026-09-20 20:05] Model switched.\n\nxplay game alice";
  assert.deepEqual(matchAppRoute(withEvents).args, { gameId: "alice", fresh: false });
});

test("no screenshot, or the word anywhere but first, is not the route", () => {
  assert.equal(matchAppRoute("xplay what were my best alternatives?"), null);
  assert.equal(matchAppRoute("can you xplay this\n[media attached: media://inbound/a.png (image/png)]"), null);
  // A shortcut quoted in chat history is someone else's earlier message.
  assert.equal(matchAppRoute("[media attached: media://inbound/a.png (image/png)]\nChat history since last reply:\n[bob] xplay\n\nnice board"), null);
  assert.equal(matchAppRoute("xplay\n[media attached: media://inbound/a.pdf (application/pdf)]"), null);
});

test("media notes: the multi-file form, and non-images skipped", () => {
  const refs = parseMediaRefs(
    "[media attached: 2 files]\n[media attached 1/2: media://inbound/a.png (image/png)]\n" +
    "[media attached 2/2: media://inbound/b.ogg (audio/ogg)]",
  );
  assert.deepEqual(refs, ["media://inbound/a.png"]);
});

// ── Install check and image confinement ──────────────────────────────────────

test("a route runs only for an installed, active, sha-verified app", () => {
  const ok = makeWorkspace();
  assert.deepEqual(checkRouteInstalled(CROSSPLAY_ROUTE, ok.ws, _manifestStatusAllowsTriggers), { ok: true });
  const drift = makeWorkspace({ driftSha: true });
  assert.match(checkRouteInstalled(CROSSPLAY_ROUTE, drift.ws, _manifestStatusAllowsTriggers).reason, /does not match/);
  const paused = makeWorkspace({ status: "paused" });
  assert.match(checkRouteInstalled(CROSSPLAY_ROUTE, paused.ws, _manifestStatusAllowsTriggers).reason, /not active/);
  const none = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-approute-none-"));
  assert.equal(checkRouteInstalled(CROSSPLAY_ROUTE, none, _manifestStatusAllowsTriggers).ok, false);
});

test("a typed media note cannot send a file from outside the inbound store", () => {
  const w = makeWorkspace();
  const outside = path.join(w.root, "secret.png");
  fs.writeFileSync(outside, pngBytes());
  assert.equal(resolveInboundImage(outside, w.state)?.file ?? null, null);   // basename not in inbound
  assert.equal(resolveInboundImage("../../../etc/passwd", w.state), null);
  fs.writeFileSync(path.join(w.state, "media", "inbound", "notes.png"), "not an image");
  assert.equal(resolveInboundImage("media://inbound/notes.png", w.state), null);
  const img = resolveInboundImage("media://inbound/board-1.png", w.state);
  assert.equal(img.mime, "image/png");
  assert.equal(img.width, 1170);
  assert.ok(estimateImageTokens(img.width, img.height) < 1600);
});

// ── The pipeline (real app script, faked model calls) ────────────────────────

async function seedConfirmed(w) {
  // First sighting of the game: a whole-board reading.
  const { api } = fakeApi(w.state, { visions: [reading("anchor-followup.full.json")] });
  const runtime = openClawRouteRuntime(api, { workspaceDir: w.ws, entryScript: CROSSPLAY_ROUTE.entryScript, provider: "anthropic", model: "claude-sonnet-5", purpose: "t" });
  const image = resolveInboundImage("media://inbound/board-1.png", w.state);
  return runAppRoute(matchAppRoute(PROMPT), image, runtime, quietLogger());
}

test("one move: one vision call + one tool-free commentary call, the PoC's play in the PoC's format", async () => {
  const w = makeWorkspace();
  assert.equal((await seedConfirmed(w)).outcome, "solved");

  const { api, seen } = fakeApi(w.state, { visions: [reading("wows-followup.delta.json")] });
  const runtime = openClawRouteRuntime(api, { workspaceDir: w.ws, entryScript: CROSSPLAY_ROUTE.entryScript, provider: "anthropic", model: "claude-sonnet-5", purpose: "t" });
  const image = resolveInboundImage("media://inbound/board-1.png", w.state);
  const res = await runAppRoute(matchAppRoute(PROMPT), image, runtime, quietLogger());

  assert.equal(res.outcome, "solved");
  assert.equal(res.mode, "delta");
  assert.deepEqual(res.calls.map((c) => c.kind), ["vision", "commentary"]);
  // Saved state first: the vision call was asked for the delta only.
  assert.match(seen.vision[0].instructions, /already confirmed on it and must not be repeated/);
  assert.match(seen.vision[0].instructions, /- THEORY G4 across/);
  assert.equal(seen.vision[0].jsonMode, true);
  // The commentary call: no tools, thinking off, bounded.
  assert.equal(seen.llm.length, 1);
  assert.equal(seen.llm[0].tools, undefined);
  assert.equal(seen.llm[0].reasoning, "off");
  assert.ok(seen.llm[0].maxTokens <= ROUTE_LIMITS.commentaryMaxTokens);
  assert.deepEqual(Object.keys(seen.llm[0].messages[0]).sort(), ["content", "role"]);

  assert.match(res.reply, /^Crossplay Coach — \d+ legal moves · rack IDOLLTH · you 299–321 · bag 15/);
  assert.match(res.reply, /Board: saved game 'g' \+ FACTIOUS G10 across, WOWS N7 down/);
  assert.match(res.reply, /1\. HILLO — O3 Down — 33 points/);
  assert.match(res.reply, /   Place: O3=H, O4=I, O5=L, O6=L, O7=O/);
  assert.match(res.reply, /\nCall: HILLO on the 3W — 33 now and it keeps DT\.\n/);
  assert.match(res.reply, /Lexicon: lexicon\.txt/);
  assert.equal(res.calls[1].usageSource, "provider");
  assert.equal(res.calls[0].usageSource, "estimated");
});

test("a saved board several plays behind costs one whole-board re-read, never a loop", async () => {
  const w = makeWorkspace();
  const first = fakeApi(w.state, { visions: [reading("anchor-regression.full.json")] });
  const image = resolveInboundImage("media://inbound/board-1.png", w.state);
  const rt = (api) => openClawRouteRuntime(api, { workspaceDir: w.ws, entryScript: CROSSPLAY_ROUTE.entryScript, provider: "anthropic", model: "claude-sonnet-5", purpose: "t" });
  assert.equal((await runAppRoute(matchAppRoute(PROMPT), image, rt(first.api), quietLogger())).outcome, "solved");

  const delta = { ...reading("wows-followup.full.json"), confirmed_words_missing: [] };
  const { api, seen } = fakeApi(w.state, { visions: [delta, reading("wows-followup.full.json")] });
  const res = await runAppRoute(matchAppRoute(PROMPT), image, rt(api), quietLogger());
  assert.equal(res.outcome, "solved");
  assert.deepEqual(res.calls.map((c) => c.kind), ["vision", "vision", "commentary"]);
  assert.match(seen.vision[1].instructions, /^Transcribe this Crossplay screenshot/);
  assert.match(res.reply, /Board: read whole — saved game 'g' was \d+ plays behind/);
});

test("a refused board ends the move with its reason and makes no commentary call", async () => {
  const w = makeWorkspace();
  const bad = { ...reading("anchor-followup.full.json"), uncertain: ["H11"] };
  const { api, seen } = fakeApi(w.state, { visions: [bad] });
  const runtime = openClawRouteRuntime(api, { workspaceDir: w.ws, entryScript: CROSSPLAY_ROUTE.entryScript, provider: "anthropic", model: "claude-sonnet-5", purpose: "t" });
  const res = await runAppRoute(matchAppRoute(PROMPT), resolveInboundImage("media://inbound/board-1.png", w.state), runtime, quietLogger());
  assert.equal(res.outcome, "refused");
  assert.match(res.reply, /couldn't read H11/);
  assert.equal(seen.llm.length, 0);
  assert.equal(res.calls.length, 1);
});

test("a failed vision call ends the move; nothing is retried", async () => {
  const w = makeWorkspace();
  const { api, seen } = fakeApi(w.state, { visionThrows: true });
  const runtime = openClawRouteRuntime(api, { workspaceDir: w.ws, entryScript: CROSSPLAY_ROUTE.entryScript, provider: "anthropic", model: "claude-sonnet-5", purpose: "t" });
  const res = await runAppRoute(matchAppRoute(PROMPT), resolveInboundImage("media://inbound/board-1.png", w.state), runtime, quietLogger());
  assert.equal(res.outcome, "vision_failed");
  assert.equal(seen.vision.length, 1);
  assert.equal(seen.llm.length, 0);
  assert.match(res.reply, /couldn't finish this move/);
});

test("a commentary line that names no solver play is replaced by the solver's call", async () => {
  assert.equal(acceptCallLine("Call: play QUIXOTIC for 200", ["HILLO", "WO"], "Call: HILLO."), "Call: HILLO.");
  assert.equal(acceptCallLine("I think HILLO", ["HILLO"], "Call: HILLO."), "Call: HILLO.");
  assert.equal(acceptCallLine("call: hillo, easily", ["HILLO"], "x"), "Call: hillo, easily");
  const w = makeWorkspace();
  const { api } = fakeApi(w.state, { visions: [reading("wows-followup.full.json")], completion: "Here are some thoughts…" });
  const runtime = openClawRouteRuntime(api, { workspaceDir: w.ws, entryScript: CROSSPLAY_ROUTE.entryScript, provider: "anthropic", model: "claude-sonnet-5", purpose: "t" });
  const res = await runAppRoute(matchAppRoute(PROMPT), resolveInboundImage("media://inbound/board-1.png", w.state), runtime, quietLogger());
  assert.match(res.reply, /\nCall: HILLO at O3 Down for 33/);
});

test("a model without an 'off' thinking level is asked again at its default, before any spend", async () => {
  const w = makeWorkspace();
  const { api, seen } = fakeApi(w.state, { visions: [reading("wows-followup.full.json")], llmReasoningRejected: true });
  const runtime = openClawRouteRuntime(api, { workspaceDir: w.ws, entryScript: CROSSPLAY_ROUTE.entryScript, provider: "anthropic", model: "claude-sonnet-5", purpose: "t" });
  const res = await runAppRoute(matchAppRoute(PROMPT), resolveInboundImage("media://inbound/board-1.png", w.state), runtime, quietLogger());
  assert.equal(res.outcome, "solved");
  assert.equal(seen.llm.length, 2);
  assert.equal(seen.llm[1].reasoning, undefined);
});

// ── Through TurnObserver's hooks: the session never runs ─────────────────────

function observerHarness(w, apiExtras, { router } = {}) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-approute-shared-"));
  const logger = quietLogger();
  const observer = new TurnObserver({
    botId: BOT, role: "member", networkId: "n", sharedDir: shared, tier: "full",
    capabilities: { observer: true, injectPodConduct: true, injectKeywords: true, modelRouting: true, deferTool: false, recordApplicationTool: false },
    tierClassification: "session", enableLLMSummarization: false, minTurns: 1, keywordConfidenceThreshold: 0.7,
  }, logger, undefined);
  let routed = 0;
  observer.modelRouter = {
    setUserTier: () => { routed++; }, setSessionUserKey: () => {}, setSessionType: () => {},
    getSessionType: () => "conversation", isSpendCapForced: () => false,
    resolveModelOverride: () => { routed++; return null; }, resolveAuthProfileOverride: () => null,
    getLastDecisionDriver: () => null, clearSession: () => {},
  };
  if (router) observer.modelRouter = router;
  // The preflight router and keyword injection spend on a turn the session
  // never runs — counted so a claimed prompt can prove it skipped them.
  let preflight = 0;
  const _handleBMR = observer.handleBeforeModelResolve.bind(observer);
  observer.handleBeforeModelResolve = (...a) => { preflight++; return _handleBMR(...a); };
  const hooks = new Map();
  observer.register({
    ...apiExtras,
    on: (name, handler) => { if (!hooks.has(name)) hooks.set(name, []); hooks.get(name).push(handler); },
    registerHook: () => {},
  });
  const fire = async (name, event, ctx) => {
    let out;
    for (const h of hooks.get(name) ?? []) { const r = await h(event, ctx); if (r !== undefined) out = r; }
    return out;
  };
  return { shared, fire, logger, routedCount: () => routed, preflightCount: () => preflight };
}

const readJsonl = (file) => fs.existsSync(file)
  ? fs.readFileSync(file, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l))
  : [];

test("the shortcut is claimed before the model; the receipt lands on the app, the session records no turn", async () => {
  const w = makeWorkspace();
  const { api, seen } = fakeApi(w.state, { visions: [reading("wows-followup.full.json")] });
  const h = observerHarness(w, api);
  // A model the built-in price table knows, so the receipt carries a price.
  const ctx = ctxFor(w, { modelId: "claude-sonnet-4-6" });

  // before_model_resolve: the turn is routed like any other (the app's own
  // calls run on the model it resolves — here the stub has no override), but
  // the preflight router and keyword injection are skipped.
  assert.deepEqual(await h.fire("before_model_resolve", { prompt: PROMPT }, ctx), {});
  assert.ok(h.routedCount() > 0, "routing (tier preference + override) must run for a claimed prompt");
  assert.equal(h.preflightCount(), 0);

  const claim = await h.fire("before_agent_reply", { cleanedBody: PROMPT }, ctx);
  assert.equal(claim.handled, true);
  assert.match(claim.reply.text, /1\. HILLO — O3 Down — 33 points/);
  assert.equal(seen.vision.length, 1);
  assert.equal(seen.llm.length, 1);
  // The vision call ran on the model OC resolved for this turn.
  assert.equal(seen.vision[0].provider, "anthropic");
  assert.equal(seen.vision[0].model, "claude-sonnet-4-6");

  const day = new Date().toISOString().slice(0, 10);
  const turns = readJsonl(path.join(h.shared, BOT, "turns", `turns-${day}.jsonl`));
  assert.equal(turns.length, 2);
  for (const t of turns) {
    assert.equal(t.app_id, "crossplay-coach");
    assert.equal(t.app_attribution, "explicit");
    assert.equal(t.source, "user");
    assert.match(t.session_id, /^app-route:crossplay-coach:run-/);
  }
  const ann = readJsonl(path.join(h.shared, "annotations", BOT, `${day}.jsonl`));
  assert.equal(ann.length, 1);
  assert.equal(ann[0].app_id, "crossplay-coach");
  assert.equal(ann[0].app_attribution_source, "app_route");
  assert.equal(ann[0].app_route.model_calls, 2);
  assert.equal(ann[0].app_route.outcome, "solved");
  assert.equal(ann[0].app_route.user_id, "4242");
  assert.ok(ann[0].cost_estimated > 0);

  // If OC still reports the run ended, the conversation gets no turn record.
  await h.fire("agent_end", { messages: [], success: true, durationMs: 1 }, ctx);
  assert.equal(readJsonl(path.join(h.shared, BOT, "turns", `turns-${day}.jsonl`)).length, 2);
  assert.equal(readJsonl(path.join(h.shared, "annotations", BOT, `${day}.jsonl`)).length, 1);
});

test("an active spend cap reaches the app route: a claimed prompt gets the downgrade, not {}", async () => {
  const w = makeWorkspace();
  const { api } = fakeApi(w.state, { visions: [reading("wows-followup.full.json")] });
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-approute-cap-"));
  const d = new Date();
  const ymd = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  fs.mkdirSync(path.join(shared, "spend-caps"), { recursive: true });
  fs.writeFileSync(
    path.join(shared, "spend-caps", `${BOT}-${ymd}.json`),
    JSON.stringify({ action: "downgrade-tier", cleared: false }),
  );
  const router = new ModelRouter({
    rungs: [
      { id: "fast-class", models: ["anthropic/claude-haiku-4-5"], costClass: "low" },
      { id: "standard-class", models: ["anthropic/claude-sonnet-4-6"], costClass: "medium" },
      { id: "power-class", models: ["anthropic/claude-opus-4-7"], costClass: "high" },
    ],
    roles: { fast: "fast-class", standard: "standard-class", power: "power-class" },
    routing: { enabled: true },
  }, shared, BOT);
  const h = observerHarness(w, api, { router });
  const ctx = ctxFor(w);
  assert.equal(router.isSpendCapForced(ctx.sessionKey), true);

  const out = await h.fire("before_model_resolve", { prompt: PROMPT }, ctx);
  assert.notDeepEqual(out, {}, "a spend-capped bot must not run the app route on its unrouted default");
  assert.equal(out.modelOverride, "claude-haiku-4-5");
  assert.equal(out.providerOverride, "anthropic");
  assert.equal(router.getLastDecisionDriver(ctx.sessionKey), "spend_cap");
  // Still no preflight spend on a turn the session never runs.
  assert.equal(h.preflightCount(), 0);
});

test("background triggers, text-only questions and uninstalled apps stay with the session", async () => {
  const w = makeWorkspace();
  const { api, seen } = fakeApi(w.state, { visions: [reading("wows-followup.full.json")] });
  const h = observerHarness(w, api);
  assert.equal(await h.fire("before_agent_reply", { cleanedBody: PROMPT }, ctxFor(w, { trigger: "heartbeat" })), undefined);
  assert.equal(await h.fire("before_agent_reply", { cleanedBody: "xplay what was my best move?" }, ctxFor(w)), undefined);
  const drift = makeWorkspace({ driftSha: true });
  const h2 = observerHarness(drift, fakeApi(drift.state).api);
  assert.equal(await h2.fire("before_agent_reply", { cleanedBody: PROMPT }, ctxFor(drift)), undefined);
  assert.equal(seen.vision.length, 0);
});

test("a gateway without the runtime surfaces leaves the shortcut in the session", async () => {
  const w = makeWorkspace();
  const h = observerHarness(w, {});
  assert.equal(await h.fire("before_agent_reply", { cleanedBody: PROMPT }, ctxFor(w)), undefined);
  assert.notDeepEqual(await h.fire("before_model_resolve", { prompt: PROMPT }, ctxFor(w)), undefined);
});

// ── Declared under the contract ──────────────────────────────────────────────

test("the route table matches what the app declares in its spec", () => {
  const spec = JSON.parse(fs.readFileSync(path.join(APP, "p-be5885a2.json"), "utf8"));
  const declared = spec.app_route;
  assert.ok(declared, "gallery/crossplay-coach/p-be5885a2.json must declare app_route");
  assert.equal(APP_ROUTES.length, 1);
  assert.equal(declared.app_id, CROSSPLAY_ROUTE.appId);
  assert.ok(CROSSPLAY_ROUTE.trigger.test(declared.trigger.shortcut));
  assert.equal(declared.trigger.requires_image, CROSSPLAY_ROUTE.requiresImage);
  assert.equal(declared.entry_script, CROSSPLAY_ROUTE.entryScript);
  assert.deepEqual(declared.tools, [...CROSSPLAY_ROUTE.tools]);
  assert.equal(declared.delivery, CROSSPLAY_ROUTE.delivery);
  assert.equal(declared.identity, CROSSPLAY_ROUTE.identity);
  assert.equal(declared.model_calls.vision_max, ROUTE_LIMITS.maxVisionCalls);
  assert.equal(declared.model_calls.commentary_max, ROUTE_LIMITS.maxCommentaryCalls);
  assert.equal(declared.model_calls.commentary_max_output_tokens, ROUTE_LIMITS.commentaryMaxTokens);
  assert.ok(declared.cost.est_usd_per_move > 0 && declared.cost.est_usd_per_move < 0.10);
  // Every verified file is shipped in the files-pack.
  const pack = JSON.parse(fs.readFileSync(path.join(APP, "files", "manifest.json"), "utf8"));
  const shipped = new Set(pack.files.map((f) => f.path));
  for (const f of CROSSPLAY_ROUTE.verifiedFiles) assert.ok(shipped.has(f), `${f} not in the files-pack`);
});
