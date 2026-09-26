/**
 * Housekeeping on the cheap rung — OC's pre-compaction memory flush.
 *
 * Finding: internal/finding-cost-forensics-power-bot-2026-09-04.md §2 — a
 * flush turn nobody asked for (26 tokens of new input, 194k cache write, 12k
 * output) ran on the session's POWER model with every tool loaded: ~$1.90.
 *
 * OC 2026.9.2 runs the flush as `runEmbeddedAgent({ trigger: "memory" })` on
 * the conversation's own session key and fires before_model_resolve for it.
 * These tests drive the REGISTERED hooks (not the private methods) so the
 * ordering guarantees — decided before any per-session state is touched —
 * are what is under test.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/housekeepingCheapRung.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import fs from "node:fs";
import path from "node:path";

import { TurnObserver } from "../dist/observer/TurnObserver.js";
import {
  HOUSEKEEPING_LEVER,
  HOUSEKEEPING_TOOLS_ALLOW,
  HousekeepingLeverReader,
  housekeepingKindForTrigger,
  leverEnabled,
  resolveHousekeepingModel,
} from "../dist/observer/housekeeping.js";

const CONVERSATION_KEY = "agent:main:telegram:direct:5550100";
const CHEAP = "anthropic/claude-haiku-4-5";
const POWER = "anthropic/claude-opus-5";
const FLUSH_PROMPT =
  "Pre-compaction memory flush. Store durable memories now (use memory/2026-09-04.md).";

function makeHarness({ network = null, fastRoleModel = CHEAP } = {}) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-housekeeping-"));
  if (network) fs.writeFileSync(path.join(shared, "network.json"), JSON.stringify(network));
  const logs = [];
  const logger = {
    info: (m) => logs.push(String(m)),
    warn: (m) => logs.push(String(m)),
    error: (m) => logs.push(String(m)),
    debug: () => {},
  };
  const observer = new TurnObserver({
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
    classifierModel: "anthropic/claude-haiku-4-5",
  }, logger, undefined);

  // The conversation is pinned to POWER: if the flush inherited the session's
  // routing, this is the model it would get.
  const routerCalls = { resolveModelOverride: 0, setUserTier: 0, setSessionType: 0 };
  const router = observer.modelRouter;
  router.resolveFastRoleOverride = () => fastRoleModel;
  const origResolve = router.resolveModelOverride.bind(router);
  router.resolveModelOverride = (k) => { routerCalls.resolveModelOverride++; return POWER ?? origResolve(k); };
  const origSetUserTier = router.setUserTier.bind(router);
  router.setUserTier = (...a) => { routerCalls.setUserTier++; return origSetUserTier(...a); };
  const origSetSessionType = router.setSessionType.bind(router);
  router.setSessionType = (...a) => { routerCalls.setSessionType++; return origSetSessionType(...a); };
  observer.preflightRouter.classify = async () => {
    throw new Error("the preflight router must never see a housekeeping run");
  };

  const handlers = new Map();
  observer.register({
    on: (name, fn) => { handlers.set(name, fn); },
    registerTool: () => {},
    runtime: {},
  });
  return { observer, shared, logs, routerCalls, handlers };
}

function turnsRows(shared) {
  const dir = path.join(shared, "team_bot_a", "turns");
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir).flatMap((f) =>
    fs.readFileSync(path.join(dir, f), "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)));
}

// ── 1. Routing: cheap rung regardless of the session model ─────────────────

test("memory_flush trigger routes to the cheap rung, not the conversation's power model", async () => {
  const h = makeHarness();
  const out = await h.handlers.get("before_model_resolve")(
    { prompt: FLUSH_PROMPT },
    { sessionKey: CONVERSATION_KEY, runId: "flush-run-1", trigger: "memory" },
  );
  assert.deepEqual(out, { providerOverride: "anthropic", modelOverride: "claude-haiku-4-5" });
  assert.equal(h.routerCalls.resolveModelOverride, 0,
    "the conversation's routing (which would pick POWER) must not run for the flush");
});

test("the flush never writes the conversation's routing state", async () => {
  const h = makeHarness();
  await h.handlers.get("before_model_resolve")(
    { prompt: FLUSH_PROMPT },
    { sessionKey: CONVERSATION_KEY, runId: "flush-run-2", trigger: "memory" },
  );
  assert.equal(h.routerCalls.setUserTier, 0, "tier preference untouched");
  assert.equal(h.routerCalls.setSessionType, 0, "session class untouched");
  assert.equal(h.observer.modelRouter.getSessionType(CONVERSATION_KEY) ?? null, null);
});

test("a user turn on the same session still takes the conversation's routing (control)", async () => {
  const h = makeHarness();
  const out = await h.handlers.get("before_model_resolve")(
    { prompt: "what's on my calendar tomorrow?" },
    { sessionKey: CONVERSATION_KEY, runId: "user-run-1", trigger: "user" },
  );
  assert.equal(out.modelOverride, "claude-opus-5",
    "nothing changes which model answers the user");
});

test("failover re-fire for the same flush run stands down (lets OC's fallback walk)", async () => {
  const h = makeHarness();
  const fire = () => h.handlers.get("before_model_resolve")(
    { prompt: FLUSH_PROMPT },
    { sessionKey: CONVERSATION_KEY, runId: "flush-run-3", trigger: "memory" },
  );
  assert.equal((await fire()).modelOverride, "claude-haiku-4-5");
  assert.deepEqual(await fire(), {});
});

test("falls back to the classifier model when the fast role is unconfigured", async () => {
  const h = makeHarness({ fastRoleModel: null });
  const out = await h.handlers.get("before_model_resolve")(
    { prompt: FLUSH_PROMPT },
    { sessionKey: CONVERSATION_KEY, runId: "flush-run-4", trigger: "memory" },
  );
  assert.equal(out.modelOverride, "claude-haiku-4-5");
});

// ── 2. Opt-out (D-CS5: on by default, per-bot opt-out) ─────────────────────

test("per-bot opt-out leaves the flush on the session model", async () => {
  const h = makeHarness({
    network: { bots: { team_bot_a: { cost: { levers: { [HOUSEKEEPING_LEVER]: false } } } } },
  });
  const out = await h.handlers.get("before_model_resolve")(
    { prompt: FLUSH_PROMPT },
    { sessionKey: CONVERSATION_KEY, runId: "flush-run-5", trigger: "memory" },
  );
  assert.deepEqual(out, {}, "no override: OC's own choice stands");
  assert.equal(h.routerCalls.resolveModelOverride, 0,
    "opting out does not re-route the flush through the conversation's routing either");
});

test("leverEnabled: default on, bot beats pod, only explicit false disables", () => {
  assert.equal(leverEnabled({}, "b", HOUSEKEEPING_LEVER), true);
  assert.equal(leverEnabled(null, "b", HOUSEKEEPING_LEVER), true);
  assert.equal(leverEnabled({ cost: { levers: { [HOUSEKEEPING_LEVER]: false } } }, "b", HOUSEKEEPING_LEVER), false);
  assert.equal(leverEnabled({
    cost: { levers: { [HOUSEKEEPING_LEVER]: false } },
    bots: { b: { cost: { levers: { [HOUSEKEEPING_LEVER]: true } } } },
  }, "b", HOUSEKEEPING_LEVER), true);
  assert.equal(leverEnabled({ bots: { b: { cost: { levers: { [HOUSEKEEPING_LEVER]: "no" } } } } }, "b", HOUSEKEEPING_LEVER), true,
    "a non-boolean is not an opt-out");
});

test("HousekeepingLeverReader caches for the TTL and fails open on a missing file", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-lever-"));
  let now = 1_000;
  const r = new HousekeepingLeverReader(dir, "b", () => now);
  assert.equal(r.enabled(), true, "no network.json → on");
  fs.writeFileSync(path.join(dir, "network.json"),
    JSON.stringify({ bots: { b: { cost: { levers: { [HOUSEKEEPING_LEVER]: false } } } } }));
  assert.equal(r.enabled(), true, "still cached");
  now += 61_000;
  assert.equal(r.enabled(), false, "re-read after TTL");
});

// ── 3. Tool set: the allow-list, and no Evolve injections ──────────────────

test("before_prompt_build returns only the flush allow-list for a housekeeping run", async () => {
  const h = makeHarness();
  await h.handlers.get("before_model_resolve")(
    { prompt: FLUSH_PROMPT },
    { sessionKey: CONVERSATION_KEY, runId: "flush-run-6", trigger: "memory" },
  );
  const out = await h.handlers.get("before_prompt_build")(
    {}, { sessionKey: CONVERSATION_KEY, runId: "flush-run-6", sessionId: "s-1", trigger: "memory" },
  );
  assert.deepEqual(out, { toolsAllow: ["read", "write"] });
  assert.deepEqual([...HOUSEKEEPING_TOOLS_ALLOW], ["read", "write"]);
});

test("the allow-list also applies when routing did not run (trigger alone)", async () => {
  const h = makeHarness();
  const out = await h.handlers.get("before_prompt_build")(
    {}, { sessionKey: CONVERSATION_KEY, runId: "unrouted", sessionId: "s-1", trigger: "memory" },
  );
  assert.deepEqual(out, { toolsAllow: ["read", "write"] });
});

// ── 4. Visibility: one row tagged memory_flush, nothing else ───────────────

test("the flush turn is written as source=memory_flush and does not leak into the next turn", async () => {
  const h = makeHarness();
  const sessionId = "sess-flush-1";
  await h.handlers.get("before_model_resolve")(
    { prompt: FLUSH_PROMPT },
    { sessionKey: CONVERSATION_KEY, runId: "flush-run-7", trigger: "memory" },
  );
  await h.handlers.get("llm_output")(
    {
      sessionId, model: "claude-haiku-4-5", provider: "anthropic",
      usage: { input: 26, output: 900, cacheRead: 0, cacheWrite: 41_000 },
    },
    { sessionId, sessionKey: CONVERSATION_KEY, trigger: "memory", runId: "flush-run-7" },
  );
  await h.handlers.get("agent_end")(
    { messages: [{ role: "user", content: FLUSH_PROMPT }, { role: "assistant", content: "NO_REPLY" }], success: true },
    { sessionId, sessionKey: CONVERSATION_KEY, runId: "flush-run-7", trigger: "memory" },
  );
  const rows = turnsRows(h.shared);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].source, "memory_flush");
  assert.equal(rows[0].output_tokens, 900);
  assert.equal(rows[0].cache_write_tokens, 41_000);
  assert.equal(h.observer.sessionLlmData.has(sessionId), false,
    "the next conversation turn must not inherit the flush's tokens");
  assert.equal(h.observer.sessionTurnCounts.get(sessionId) ?? 0, 0,
    "the flush is not a conversation turn");
});

// ── 5. Pure helpers ─────────────────────────────────────────────────────────

test("housekeepingKindForTrigger maps OC's trigger and the normalised tags", () => {
  assert.equal(housekeepingKindForTrigger("memory"), "memory_flush");
  assert.equal(housekeepingKindForTrigger("MEMORY"), "memory_flush");
  assert.equal(housekeepingKindForTrigger("memory_flush"), "memory_flush");
  assert.equal(housekeepingKindForTrigger("compaction"), "compaction");
  for (const t of ["user", "heartbeat", "cron", "", null, undefined, 7]) {
    assert.equal(housekeepingKindForTrigger(t), null);
  }
});

test("resolveHousekeepingModel prefers the fast role, then the classifier model", () => {
  assert.deepEqual(resolveHousekeepingModel("p/fast", "p/cls"), { model: "p/fast", source: "fast_role" });
  assert.deepEqual(resolveHousekeepingModel(null, "p/cls"), { model: "p/cls", source: "classifier_model" });
  assert.equal(resolveHousekeepingModel("  ", ""), null);
});
