/**
 * App turns attribute to the app that ran — the three misses of incident
 * 2026-09-20 §4 (internal/incident-post-mortem-2026-09-20-image-turn-cache-
 * thrash-and-poll-loop.md), each fixed at its source, plus the control that
 * notices when an app runs and the turn still resolves "none".
 *
 *   1. Shortcut: `Xplay` + a screenshot, served by the bot's SESSION (the
 *      route declined), attributes explicitly via ``app_route``; so does a
 *      text-only `xplay` when the installed manifest's
 *      ``usage.trigger_recognition.requires_keyword`` is true.
 *   2. Integrity: `cd <workspace> && scripts/crossplay_coach audit …` — the
 *      extensionless launcher behind the app's allowed cd prefix — is a
 *      script-middleware match.
 *   3. Control: a launcher named in a turn that resolves none is ledgered
 *      once per (app, day); the resolved result is untouched.
 *
 * Placeholder names throughout. Run from packages/plugin (after build):
 *   node --test tests/appAttribution.shortcutAndLauncher.test.mjs
 */
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  configureAppAttribution,
  noteAppExecuted,
  recordExplicit,
  resolveForTurn,
  MISMATCH_LEDGER_FILENAME,
  _resetForTests,
} from "../dist/apps/AppAttribution.js";
import { shortcutAttribution, matchShortcut } from "../dist/apps/AppRoutes.js";
import {
  knownScriptsForManifest,
  launcherMentionsForManifest,
  lookupCommand,
  mentionedAppIds,
} from "../dist/integrity/appScriptRegistry.js";
import { makeAppIntegrityHandler } from "../dist/integrity/AppIntegrityMiddleware.js";
import { TurnObserver, _manifestStatusAllowsTriggers } from "../dist/observer/TurnObserver.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GALLERY_MANIFEST = path.resolve(HERE, "../../../gallery/crossplay-coach/p-be5885a2.json");
const BOT = "word_bot";
const APP = "crossplay-coach";

/** The gallery manifest as installed — the real declaration, not a stub. */
const manifest = () => JSON.parse(fs.readFileSync(GALLERY_MANIFEST, "utf8"));

/** A bot workspace with the app's manifest installed. */
function makeWorkspace({ requiresKeyword } = {}) {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-attr-ws-"));
  fs.mkdirSync(path.join(ws, "manifests"), { recursive: true });
  const m = manifest();
  if (requiresKeyword !== undefined) m.usage.trigger_recognition.requires_keyword = requiresKeyword;
  fs.writeFileSync(path.join(ws, "manifests", `${APP}.json`), JSON.stringify(m));
  return ws;
}

// The incident turn's inputs (§3 / §4), with the bot name placeholdered.
const INCIDENT_PROMPT =
  "[media attached: media://inbound/board-1.png (image/png) \"IMG_0001.png\"]\nXplay";
const INCIDENT_EXEC =
  "cd /Users/word_bot/.openclaw/workspace && scripts/crossplay_coach audit --request crossplay-data/request.json";
const WS_ROOT = "/Users/word_bot/.openclaw/workspace";

beforeEach(() => _resetForTests());

// ── 1. Shortcut ─────────────────────────────────────────────────────────────

test("the manifest this suite relies on still says what the tests assume", () => {
  const m = manifest();
  assert.equal(m.app_id, APP);
  assert.equal(m.app_route.trigger.shortcut, "xplay");
  assert.equal(m.app_route.trigger.requires_image, true);
  // Checked, not assumed (brief): the keyword alone is required.
  assert.equal(m.usage.trigger_recognition.requires_keyword, true);
});

test("`Xplay` + an image attributes to the app, whatever the case", () => {
  const ws = makeWorkspace();
  assert.equal(shortcutAttribution(INCIDENT_PROMPT, ws, () => true), APP);
  assert.equal(shortcutAttribution(INCIDENT_PROMPT.replace("Xplay", "XPLAY game g"), ws, () => true), APP);
});

test("plain `xplay` text attributes because the manifest requires the keyword", () => {
  const ws = makeWorkspace();
  assert.equal(shortcutAttribution("xplay what were my best alternatives?", ws, () => true), APP);
  // A hook that saw the attachment another way can say so.
  const off = makeWorkspace({ requiresKeyword: false });
  assert.equal(shortcutAttribution("xplay", off, () => true, { hasImage: true }), APP);
});

test("without requires_keyword, a text-only shortcut does not attribute", () => {
  const ws = makeWorkspace({ requiresKeyword: false });
  assert.equal(shortcutAttribution("xplay what were my best alternatives?", ws, () => true), null);
  assert.equal(shortcutAttribution(INCIDENT_PROMPT, ws, () => true), APP);
});

test("unrelated, mid-sentence, uninstalled and inactive stay unattributed", () => {
  const ws = makeWorkspace();
  assert.equal(shortcutAttribution("what's the weather tomorrow?", ws, () => true), null);
  assert.equal(shortcutAttribution("I played xplay yesterday", ws, () => true), null);
  assert.equal(shortcutAttribution("xplayer mode", ws, () => true), null);
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-attr-empty-"));
  assert.equal(shortcutAttribution(INCIDENT_PROMPT, empty, () => true), null);
  assert.equal(shortcutAttribution(INCIDENT_PROMPT, ws, () => false), null);
  assert.equal(matchShortcut("hello"), null);
});

test("matchShortcut finds the shortcut without the route's image requirement", () => {
  const hit = matchShortcut("xplay game g");
  assert.equal(hit.spec.appId, APP);
  assert.equal(hit.line, "xplay game g");
  assert.deepEqual(hit.mediaRefs, []);
  assert.deepEqual(matchShortcut(INCIDENT_PROMPT).mediaRefs, ["media://inbound/board-1.png"]);
});

// ── 2. Integrity match through the allowed `cd … &&` prefix ─────────────────

test("the extensionless launcher behind `cd … &&` is a script-integrity match", () => {
  const scripts = knownScriptsForManifest(manifest(), WS_ROOT);
  const paths = scripts.map((s) => s.relPath);
  assert.ok(paths.includes("scripts/crossplay_coach"), `launcher missing from ${paths}`);
  assert.ok(paths.includes("scripts/crossplay_coach.py"), "files[] .py missing");
  assert.ok(!paths.includes("scripts/crossplay_lexicon.json"), "data file must never be a script");
  assert.deepEqual(lookupCommand(INCIDENT_EXEC, scripts), {
    appId: APP, script: path.join(WS_ROOT, "scripts/crossplay_coach"),
  });
  assert.equal(lookupCommand("scripts/crossplay_coach solve --request r.json --save", scripts)?.appId, APP);
  // Recognition bounds hold for the new launcher: arguments are not runs.
  assert.equal(lookupCommand("cat scripts/crossplay_coach", scripts), null);
  assert.equal(lookupCommand("ls scripts/crossplay_coach_old", scripts), null);
});

test("an extensionless file outside a program dir is not a launcher", () => {
  const m = { app_id: "demo-app", files: [{ path: "data/wordlist" }, { path: "bin/demo" }] };
  const rel = knownScriptsForManifest(m, WS_ROOT).map((s) => s.relPath);
  assert.deepEqual(rel, ["bin/demo"]);
});

test("a cli program outside the bot workspace is never a known script", () => {
  // Fix-forward of #4693 (review finding 1): an interpreter named as the
  // command's program must not become an app's launcher — it would attribute
  // every `/bin/bash -lc …` turn in the pod to this app and hand its integrity
  // harness other apps' failures. The unexpanded `${workspace}` placeholder
  // lands at the filesystem root and is dropped by the same containment test.
  const m = {
    app_id: "demo-app",
    interface_contract: { cli: [{ command: "/bin/bash ${workspace}/scripts/demo-cron" }] },
  };
  const scripts = knownScriptsForManifest(m, WS_ROOT);
  assert.deepEqual(scripts, []);
  assert.equal(lookupCommand('/bin/bash -lc "git status"', scripts), null);
  assert.deepEqual(mentionedAppIds('/bin/bash -lc "git status"', launcherMentionsForManifest(m, WS_ROOT)), []);
  // An in-workspace program is still a launcher.
  const inWs = { app_id: "demo-app", interface_contract: { cli: [{ command: "scripts/demo run" }] } };
  assert.deepEqual(knownScriptsForManifest(inWs, WS_ROOT).map((s) => s.relPath), ["scripts/demo"]);
});

test("the integrity middleware records script_middleware for the incident command", () => {
  const scripts = knownScriptsForManifest(manifest(), WS_ROOT);
  const registry = { lookup: (c) => lookupCommand(c, scripts) };
  const handler = makeAppIntegrityHandler(registry, { info() {}, warn() {}, error() {}, debug() {} });
  handler(
    { toolCallId: "t1", toolName: "exec", args: { command: INCIDENT_EXEC },
      result: { content: [{ type: "text", text: "PASS" }] } },
    { runtime: "openclaw", runId: "run-exec", sessionId: "sess-exec" },
  );
  assert.deepEqual(resolveForTurn("run-exec", "sess-exec"), {
    app_id: APP, app_attribution: "explicit", app_confidence: 1.0,
    app_attribution_source: "script_middleware",
  });
});

// ── 3. The control ──────────────────────────────────────────────────────────

test("launcher mentions are position-free and bounded", () => {
  const mentions = launcherMentionsForManifest(manifest(), WS_ROOT);
  assert.deepEqual(mentionedAppIds(`bash -lc "${INCIDENT_EXEC}"`, mentions), [APP]);
  assert.deepEqual(mentionedAppIds("python3 /Users/word_bot/.openclaw/workspace/scripts/crossplay_coach.py status", mentions), [APP]);
  assert.deepEqual(mentionedAppIds("ls scripts/crossplay_coach_old myscripts/crossplay_coach", mentions), []);
  assert.deepEqual(mentionedAppIds("cat crossplay-data/request.json", mentions), []);
});

test("a declared data file is not a launcher mention", () => {
  // Fix-forward of #4693 (review finding 2): the control's path SET is the
  // program-dir launchers; only its matching is position-free.
  const m = {
    app_id: "demo-app",
    files: [{ path: "data/wordlist" }, { path: "bin/demo" }],
    realized_files: [{ path: "memory/notes" }],
  };
  const mentions = launcherMentionsForManifest(m, WS_ROOT);
  assert.deepEqual(mentions.map((s) => s.relPath), ["bin/demo"]);
  assert.deepEqual(mentionedAppIds("cat memory/notes", mentions), []);
  assert.deepEqual(mentionedAppIds("grep x data/wordlist", mentions), []);
  assert.deepEqual(mentionedAppIds('bash -lc "bin/demo go"', mentions), ["demo-app"]);
});

function ledgerLines(shared) {
  const f = path.join(shared, BOT, MISMATCH_LEDGER_FILENAME);
  return fs.existsSync(f) ? fs.readFileSync(f, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
}

test("an app that ran in a none turn is ledgered once per day and the result is untouched", () => {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-attr-shared-"));
  const warns = [];
  configureAppAttribution({ sharedDir: shared, botId: BOT }, { warn: (m) => warns.push(m), debug() {} });

  noteAppExecuted("run-a", "sess-a", APP);
  const r1 = resolveForTurn("run-a", "sess-a");
  assert.equal(r1.app_attribution, "none");
  assert.equal(r1.app_id, null);
  noteAppExecuted("run-b", "sess-b", APP);
  resolveForTurn("run-b", "sess-b");

  const lines = ledgerLines(shared);
  assert.equal(lines.length, 1, "once per (app, day)");
  assert.equal(lines[0].app_id, APP);
  assert.equal(lines[0].day, new Date().toISOString().slice(0, 10));
  assert.equal(lines[0].resolved, "none");
  assert.equal(warns.filter((w) => w.includes(APP)).length, 1);
  if (process.platform !== "win32") {
    assert.equal(fs.statSync(path.join(shared, BOT, MISMATCH_LEDGER_FILENAME)).mode & 0o777, 0o644);
  }
});

test("an attributed turn that ran its app is not a mismatch; notes are consumed", () => {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-attr-shared-"));
  configureAppAttribution({ sharedDir: shared, botId: BOT }, { warn() {}, debug() {} });
  noteAppExecuted("run-c", "sess-c", APP);
  recordExplicit("run-c", "sess-c", APP, "script_middleware");
  assert.equal(resolveForTurn("run-c", "sess-c").app_id, APP);
  // The note was consumed with the turn — a later none turn on a fresh
  // session carries no stale launcher.
  assert.equal(resolveForTurn("run-d", "sess-d").app_attribution, "none");
  assert.deepEqual(ledgerLines(shared), []);
});

test("a middleware context without a runId still reaches the control via the session", () => {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-attr-shared-"));
  configureAppAttribution({ sharedDir: shared, botId: BOT }, { warn() {}, debug() {} });
  noteAppExecuted(null, "sess-e", "other-app");
  assert.equal(resolveForTurn("run-e", "sess-e").app_attribution, "none");
  assert.equal(ledgerLines(shared)[0]?.app_id, "other-app");
});

// ── Replay: the incident turn through TurnObserver's hooks ──────────────────

function observerHarness(ws) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-attr-obs-"));
  const lines = [];
  const logger = { info: (m) => lines.push(String(m)), warn: (m) => lines.push(String(m)), error: (m) => lines.push(String(m)), debug: () => {} };
  const observer = new TurnObserver({
    botId: BOT, role: "member", networkId: "n", sharedDir: shared, tier: "full",
    capabilities: { observer: true, injectPodConduct: true, injectKeywords: true, modelRouting: true, deferTool: false, recordApplicationTool: false },
    tierClassification: "session", enableLLMSummarization: false, minTurns: 1, keywordConfidenceThreshold: 0.7,
  }, logger, undefined);
  // Routing is not under test: every router method is an inert stub.
  const routerBase = { getSessionType: () => "conversation", isSpendCapForced: () => false };
  observer.modelRouter = new Proxy(routerBase, {
    get: (t, k) => (k in t ? t[k] : () => null),
  });
  observer.handleBeforeModelResolve = async () => ({ systemAppend: undefined });
  const hooks = new Map();
  // A gateway WITHOUT runtime.mediaUnderstanding / runtime.llm: the route
  // declines and the session serves the shortcut — the incident's shape.
  observer.register({
    runtime: { state: { resolveStateDir: () => path.dirname(ws) } },
    on: (name, handler) => { if (!hooks.has(name)) hooks.set(name, []); hooks.get(name).push(handler); },
    registerHook: () => {},
  });
  const fire = async (name, event, ctx) => {
    let out;
    for (const h of hooks.get(name) ?? []) { const r = await h(event, ctx); if (r !== undefined) out = r; }
    return out;
  };
  return { shared, fire, lines };
}

const readAnnotations = (shared) => {
  const f = path.join(shared, "annotations", BOT, `${new Date().toISOString().slice(0, 10)}.jsonl`);
  return fs.existsSync(f) ? fs.readFileSync(f, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
};

test("replay: the incident turn, session-served, lands app_id crossplay-coach on the annotation", async () => {
  const ws = makeWorkspace();
  const h = observerHarness(ws);
  const ctx = {
    trigger: "user", runId: "run-incident", sessionId: "sess-reset-1",
    sessionKey: "agent:main:telegram:direct:4242", workspaceDir: ws,
    modelProviderId: "anthropic", modelId: "claude-sonnet-5", senderId: "4242", channelId: "telegram",
  };
  await h.fire("before_agent_run", { userMessage: INCIDENT_PROMPT, sessionId: ctx.sessionId, channelId: "telegram" }, ctx);
  await h.fire("before_model_resolve", { prompt: INCIDENT_PROMPT }, ctx);
  assert.equal(await h.fire("before_agent_reply", { cleanedBody: INCIDENT_PROMPT }, ctx), undefined,
    "the route must decline on this gateway — the session serves the turn");
  await h.fire("agent_end", {
    messages: [{ role: "user", content: INCIDENT_PROMPT }, { role: "assistant", content: "Best overall: HILLO at O3 Down for 33." }],
    success: true, durationMs: 1,
  }, ctx);
  const ann = readAnnotations(h.shared);
  assert.equal(ann.length, 1);
  assert.equal(ann[0].app_id, APP);
  assert.equal(ann[0].app_attribution, "explicit");
  assert.equal(ann[0].app_attribution_source, "app_route");
});

test("replay: an unrelated turn on the same bot stays none", async () => {
  const ws = makeWorkspace();
  const h = observerHarness(ws);
  const ctx = {
    trigger: "user", runId: "run-other", sessionId: "sess-other",
    sessionKey: "agent:main:telegram:direct:4242", workspaceDir: ws, senderId: "4242", channelId: "telegram",
  };
  await h.fire("before_agent_run", { userMessage: "remind me to call the dentist", sessionId: ctx.sessionId, channelId: "telegram" }, ctx);
  await h.fire("before_model_resolve", { prompt: "remind me to call the dentist" }, ctx);
  await h.fire("agent_end", {
    messages: [{ role: "user", content: "remind me to call the dentist" }, { role: "assistant", content: "Done." }],
    success: true, durationMs: 1,
  }, ctx);
  const ann = readAnnotations(h.shared);
  assert.equal(ann.length, 1);
  assert.equal(ann[0].app_id, null);
  assert.equal(ann[0].app_attribution, "none");
});

test("the status predicate the observer passes is the lifecycle gate", () => {
  assert.equal(typeof _manifestStatusAllowsTriggers, "function");
  assert.equal(_manifestStatusAllowsTriggers({ status: "active" }), true);
});
