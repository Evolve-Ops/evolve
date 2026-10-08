/**
 * Evolve overhead breaker (D-OH5), subagent turn-row attribution, and the
 * hook-fire ledger.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/evolveOverheadBreaker.test.mjs
 */
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  runPinnedSubagent,
  evolveSubagentTag,
  SubagentBreakerRefusal,
  configureSubagentBreakerGate,
  _resetSubagentPinDenialForTest,
  _resetSubagentBreakerGateForTest,
} from "../dist/observer/subagentRun.js";
import { readEvolveOverheadDecision } from "../dist/breakers/EvolveOverheadBreaker.js";
import { HookFireLedger } from "../dist/observer/HookFireLedger.js";
import { TurnObserver } from "../dist/observer/TurnObserver.js";
import { LLMTierClassifier } from "../dist/observer/LLMTierClassifier.js";
import { PreflightIntentRouter } from "../dist/observer/PreflightIntentRouter.js";
import { SessionStruggleJudge } from "../dist/observer/SessionStruggleJudge.js";

const BOT = "team_bot_a";
const TAGS = ["session-summary", "tier-classifier", "session-judge", "preflight"];

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "evolve-oh-"));
function trip(shared, file, body) {
  const d = path.join(shared, "breakers", BOT);
  fs.mkdirSync(d, { recursive: true });
  fs.writeFileSync(path.join(d, file), typeof body === "string" ? body : JSON.stringify(body));
}
const overhead = (extra = {}) => ({
  bot_id: BOT, type: "evolve_overhead", tripped_at: new Date().toISOString(),
  expires_at: null, reason: "evolve spend over budget", trip_id: "t1", detail: {}, ...extra,
});
function makeLogger() {
  const info = [];
  return { info: (m) => info.push(m), warn() {}, error() {}, debug() {}, _info: info };
}
function makeApi() {
  const runs = [];
  return { runs, runtime: { subagent: { run: async (p) => { runs.push(p); return { runId: "r" }; } } } };
}

beforeEach(() => {
  _resetSubagentPinDenialForTest();
  _resetSubagentBreakerGateForTest();
});

async function attempt(shared, tag, interactiveTurn) {
  const logger = makeLogger();
  configureSubagentBreakerGate({ sharedDir: shared, botId: BOT }, logger);
  const api = makeApi();
  try {
    await runPinnedSubagent(api, logger, {
      idempotencyKey: `evolve:${tag}:${BOT}:1`, message: "m", interactiveTurn,
    });
    return { ran: api.runs.length === 1, err: null, logger };
  } catch (err) {
    return { ran: false, err, logger };
  }
}

test("overhead trip refuses every tag, including on interactive turns", async () => {
  const shared = tmp();
  trip(shared, "evolve_overhead.json", overhead());
  for (const tag of TAGS) {
    for (const interactive of [false, true]) {
      const r = await attempt(shared, tag, interactive);
      assert.ok(r.err instanceof SubagentBreakerRefusal, `${tag}/${interactive}`);
      assert.equal(r.err.scope, "evolve_overhead");
      assert.match(r.err.message, /Evolve overhead breaker/);
    }
  }
});

test("refusal log names the overhead breaker, says the bot keeps answering", async () => {
  const shared = tmp();
  trip(shared, "evolve_overhead.json", overhead());
  const r = await attempt(shared, "preflight", true);
  const line = r.logger._info.join("\n");
  assert.match(line, /Evolve overhead breaker/);
  assert.match(line, /bot keeps answering/);
});

test("cost-only trip still excuses tier-classifier/preflight on interactive turns", async () => {
  const shared = tmp();
  trip(shared, "cost.json", {
    bot_id: BOT, type: "cost", state: "tripped", tripped_at: new Date().toISOString(),
    expires_at: null, reason: "cap", trip_id: "c1",
  });
  assert.equal((await attempt(shared, "tier-classifier", true)).ran, true);
  assert.equal((await attempt(shared, "preflight", true)).ran, true);
  const j = await attempt(shared, "session-judge", true);
  assert.ok(j.err instanceof SubagentBreakerRefusal);
  assert.notEqual(j.err.scope, "evolve_overhead");
});

test("expired trip is ignored; absent file proceeds; unreadable refuses", async () => {
  const shared = tmp();
  assert.equal((await attempt(shared, "session-summary", false)).ran, true);
  trip(shared, "evolve_overhead.json", overhead({ expires_at: new Date(Date.now() - 1000).toISOString() }));
  assert.equal((await attempt(shared, "session-summary", false)).ran, true);
  trip(shared, "evolve_overhead.json", "{truncated");
  const r = await attempt(shared, "session-summary", false);
  assert.ok(r.err instanceof SubagentBreakerRefusal);
  assert.equal(r.err.scope, "evolve_overhead");
});

test("readEvolveOverheadDecision shape", () => {
  const shared = tmp();
  assert.deepEqual(readEvolveOverheadDecision({ sharedDir: shared, botId: BOT }),
    { tripped: false, unreadable: false, reason: "" });
  trip(shared, "evolve_overhead.json", overhead());
  const d = readEvolveOverheadDecision({ sharedDir: shared, botId: BOT });
  assert.equal(d.tripped, true);
  assert.equal(d.unreadable, false);
  assert.equal(d.tripId, "t1");
  trip(shared, "evolve_overhead.json", "nope");
  const u = readEvolveOverheadDecision({ sharedDir: shared, botId: BOT });
  assert.equal(u.tripped, true);
  assert.equal(u.unreadable, true);
});

// ── call sites degrade rather than throw ────────────────────────────────────

test("call sites degrade under an overhead trip", async () => {
  const shared = tmp();
  trip(shared, "evolve_overhead.json", overhead());
  const logger = makeLogger();
  configureSubagentBreakerGate({ sharedDir: shared, botId: BOT }, logger);
  const api = makeApi();
  api.runtime.subagent.waitForRun = async () => ({ lastMessage: "TIER1" });

  const cls = new LLMTierClassifier({ classifierModel: "m", sharedDir: shared, botId: BOT }, logger, api);
  const c = await cls.classify("hmm maybe do the thing with the stuff", undefined, { trigger: "user" });
  assert.ok(c && typeof c.class === "string");

  const router = new PreflightIntentRouter({ sharedDir: shared, botId: BOT }, logger, api);
  const d = await router.classify({
    userMessage: "should I use postgres or sqlite for this?", botId: BOT,
    lastAssistantMessage: "", trigger: "user",
  });
  assert.ok(d === null || d.layer !== "haiku");

  const judge = new SessionStruggleJudge({ sharedDir: shared, botId: BOT }, logger, api);
  const v = await judge.judge({
    botId: BOT, conversationSnippet: "user: help\nbot: ok", triggeredBy: "shell_paste",
  });
  assert.equal(v.verdict, "AMBIGUOUS");
  assert.equal(api.runs.length, 0, "no model call may have been made");
});

// ── tag helper ──────────────────────────────────────────────────────────────

test("evolveSubagentTag", () => {
  assert.equal(evolveSubagentTag("agent:main:explicit:evolve:preflight:bot-a:1"), "preflight");
  assert.equal(evolveSubagentTag("evolve:tier-classifier:1"), "tier-classifier");
  assert.equal(evolveSubagentTag("agent:main:explicit:evolve:session-judge:bot-a:9"), "session-judge");
  assert.equal(evolveSubagentTag("evolve:session-summary:5"), "session-summary");
  assert.equal(evolveSubagentTag("agent:main:telegram:direct:123"), null);
  assert.equal(evolveSubagentTag(undefined), null);
});

// ── TurnObserver harness: turn rows + hook-fire ledger ──────────────────────

function makeObserver() {
  const shared = tmp();
  const logger = makeLogger();
  const observer = new TurnObserver({
    botId: BOT, role: "member", networkId: "n", sharedDir: shared, tier: "full",
    capabilities: {
      observer: true, injectPodConduct: true, injectKeywords: true,
      modelRouting: true, deferTool: true, recordApplicationTool: true,
    },
    tierClassification: "session", enableLLMSummarization: false, minTurns: 1,
    keywordConfidenceThreshold: 0.7,
  }, logger, undefined);
  observer.preflightRouter.classify = async () => ({ tier: null, reason: "s", layer: "s", confidence: 0, latency_ms: 0 });
  observer._isPreflightEnabled = () => true;
  const handlers = new Map();
  observer.register({
    on: (name, fn) => { handlers.set(name, fn); },
    registerTool: () => {}, runtime: {},
  });
  return { shared, observer, handlers };
}
const readTurns = (shared) => {
  const dir = path.join(shared, BOT, "turns");
  return fs.readdirSync(dir).filter((f) => f.startsWith("turns-")).flatMap((f) =>
    fs.readFileSync(path.join(dir, f), "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)));
};

test("subagent turn row carries evolve_key/evolve_tag; ordinary rows do not", async () => {
  const { shared, observer, handlers } = makeObserver();
  const key = "agent:main:explicit:evolve:preflight:team_bot_a:1757345700944";
  await handlers.get("llm_output")(
    { sessionId: "sess-sub", model: "claude-haiku-4-5", provider: "anthropic", usage: { input: 100, output: 10 } },
    { sessionId: "sess-sub", sessionKey: key },
  );
  observer.writeTurnToShared("sess-user", { model: "m", provider: "p", source: "user", channel: "telegram" });
  const rows = readTurns(shared);
  const sub = rows.find((r) => r.session_id === "sess-sub");
  const usr = rows.find((r) => r.session_id === "sess-user");
  assert.ok(sub && usr);
  assert.equal(sub.evolve_key, key);
  assert.equal(sub.evolve_tag, "preflight");
  assert.equal("evolve_key" in usr, false);
  assert.equal("evolve_tag" in usr, false);
});

test("hook records fires for an Evolve-subagent key and the file matches the contract", async () => {
  const { shared, observer, handlers } = makeObserver();
  const key = "agent:main:explicit:evolve:preflight:team_bot_a:1";
  await handlers.get("before_model_resolve")({ prompt: "router prompt" }, { sessionKey: key, runId: "r1" });
  await handlers.get("before_model_resolve")({ prompt: "hello there" }, { sessionKey: "agent:main:telegram:direct:55", runId: "r2", trigger: "user" });
  observer._hookFireLedger.flush();
  const date = new Date().toISOString().slice(0, 10);
  const f = path.join(shared, BOT, "turns", `hook-fires-${date}.json`);
  const j = JSON.parse(fs.readFileSync(f, "utf8"));
  assert.equal(j.schema_version, 1);
  assert.equal(j.bot_id, BOT);
  assert.equal(j.date, date);
  const hh = new Date().toISOString().slice(11, 13);
  const h = j.hours[hh];
  assert.ok(h.count >= 2);
  assert.equal(h.keys[key].n, 1);
  assert.equal(h.keys[key].prefix, "router prompt");
  assert.equal(h.keys["agent:main:telegram:direct:55"].prefix, "hello there");
});

// ── HookFireLedger ──────────────────────────────────────────────────────────

test("ledger counts per hour, caps keys at 50, restart merges", () => {
  const dir = tmp();
  let t = Date.UTC(2026, 9, 3, 5, 30, 0);
  const mk = () => new HookFireLedger({ sharedDir: "x", botId: BOT, dir, now: () => t, autoFlush: false });
  const l = mk();
  l.record("k0", "x".repeat(200));
  l.record("k0", "ignored");
  for (let i = 1; i < 50; i++) l.record(`k${i}`, "p");
  l.record("extra1", "p");
  l.record("extra2", "p");
  t = Date.UTC(2026, 9, 3, 6, 0, 1);
  l.record("k0", "later");
  l.flush();
  const f = path.join(dir, "hook-fires-2026-10-03.json");
  const j = JSON.parse(fs.readFileSync(f, "utf8"));
  assert.deepEqual(Object.keys(j).sort(), ["bot_id", "date", "hours", "schema_version"]);
  assert.equal(j.hours["05"].count, 53);
  assert.equal(j.hours["05"].other, 2);
  assert.equal(Object.keys(j.hours["05"].keys).length, 50);
  assert.equal(j.hours["05"].keys.k0.n, 2);
  assert.equal(j.hours["05"].keys.k0.prefix.length, 80);
  assert.equal(j.hours["06"].count, 1);
  assert.deepEqual(j.hours["06"].keys.k0, { n: 1, prefix: "later" });
  assert.equal(fs.statSync(f).mode & 0o777, 0o644);

  // restart: a fresh ledger adds to what is on disk, once
  const l2 = mk();
  t = Date.UTC(2026, 9, 3, 5, 45, 0);
  l2.record("k0", "q");
  l2.flush();
  l2.flush();
  const j2 = JSON.parse(fs.readFileSync(f, "utf8"));
  assert.equal(j2.hours["05"].count, 54);
  assert.equal(j2.hours["05"].keys.k0.n, 3);
  assert.equal(j2.hours["06"].count, 1);
});

test("ledger never throws when the dir is unwritable", () => {
  const f = path.join(tmp(), "file");
  fs.writeFileSync(f, "x");
  const l = new HookFireLedger({ sharedDir: "x", botId: BOT, dir: path.join(f, "sub"), autoFlush: false });
  l.record("k", "m");
  l.flush();
});
