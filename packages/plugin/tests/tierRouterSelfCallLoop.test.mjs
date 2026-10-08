/**
 * Regression tests for the tier-router self-call loop.
 *
 * Incident: internal/finding-tier-router-self-call-loop-2026-09-07.md
 * Chip:     internal/dispatch/done/tier-router-self-call-loop.md
 *
 * The preflight router classifies a turn by asking a cheap model, and it asks
 * by running a plugin subagent. OpenClaw 2026.9.2 began firing
 * `before_model_resolve` for that subagent's own session, so the router saw
 * its own prompt as a turn to route and classified it again — and the router
 * prompt is ambiguous to the regex layers by design, so every level reached
 * the model layer and spawned the next.
 *
 * Reproduced from the pod's own log (atlas, 2026-09-08), the two lines the
 * chip asks for side by side — a scheduled reminder seeds the tree, and
 * 264 ms later the hook is firing on the router's own prompt:
 *
 *   07:35:00.680 … before_model_resolve fired sessionKey=agent:main:m
 *                  userMessage="A scheduled reminder has been triggered…"
 *   07:35:00.944 … before_model_resolve fired sessionKey=agent:main:e
 *                  userMessage="You are routing an AI request to the right
 *                  model tier for response quality.\n\nTIE"
 *
 * Ten more followed inside the first 500 ms, each under its own fresh runId,
 * on a bot whose steady state was 12 hook fires a DAY.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/tierRouterSelfCallLoop.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { TurnObserver } from "../dist/observer/TurnObserver.js";
import { PreflightCallCap } from "../dist/observer/preflightCallCap.js";
import {
  EVOLVE_INTERNAL_PROMPT_SENTINEL,
  isEvolveInternalPrompt,
  runPinnedSubagent,
  _resetSubagentPinDenialForTest,
  _resetSubagentBreakerGateForTest,
} from "../dist/observer/subagentRun.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** The first 80 chars of the router prompt, exactly as the log recorded it. */
const ROUTER_PROMPT =
  "You are routing an AI request to the right model tier for response quality.\n\n"
  + "TIER1 = needs deep reasoning, multi-step thinking, careful analysis:";

/** The session-key family OC derives from `evolve:preflight:<bot>:<ts>`. */
const PREFLIGHT_SESSION_KEY =
  "agent:main:explicit:evolve:preflight:team_bot_a:1757345700944";

// ── Harness ─────────────────────────────────────────────────────────────────

/**
 * A TurnObserver with its hooks registered against a fake OC api, and the
 * preflight router replaced by a counter. `fire()` invokes the REGISTERED
 * before_model_resolve handler — the guard lives in that wrapper, not in
 * handleBeforeModelResolve, so a test that called the method directly would
 * walk straight past the thing under test.
 */
function makeHookHarness() {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-selfcall-"));
  const logs = [];
  const logger = {
    info: (m) => logs.push(["info", String(m)]),
    warn: (m) => logs.push(["warn", String(m)]),
    error: (m) => logs.push(["error", String(m)]),
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
  }, logger, undefined);

  // Count classifications without making one. The loop's cost was entirely in
  // this call, so "was it invoked" is the assertion that matters.
  const calls = { classify: 0, prompts: [] };
  observer.preflightRouter.classify = async ({ userMessage }) => {
    calls.classify++;
    calls.prompts.push(userMessage);
    return { tier: null, reason: "stub", layer: "stub", confidence: 0, latency_ms: 0 };
  };
  // The gate reads network.json and fails CLOSED on a pod without one (D-OH2),
  // which would make every case below vacuously pass. Force it open so the
  // control case can prove the router still runs for a real turn.
  observer._isPreflightEnabled = () => true;

  const handlers = new Map();
  observer.register({
    on: (name, fn) => { handlers.set(name, fn); },
    registerTool: () => {},
    runtime: {},
  });
  const handler = handlers.get("before_model_resolve");
  assert.ok(handler, "before_model_resolve must be registered");

  return {
    observer, logs, calls,
    fire: (prompt, ctx = {}) => handler(
      { prompt },
      { sessionKey: "agent:main:telegram:direct:55", runId: "run-1", trigger: "user", ...ctx },
    ),
  };
}

// ── 1. The guard: the router prompt never reaches the router ────────────────

test("self-call by SESSION KEY: the router's own session is not routed", async () => {
  const h = makeHookHarness();
  await h.fire(ROUTER_PROMPT, { sessionKey: PREFLIGHT_SESSION_KEY, runId: "run-nested-1" });
  assert.equal(h.calls.classify, 0,
    "the preflight router must not classify its own subagent session");
});

test("self-call by SENTINEL ALONE: an unrecognisable key still does not route", async () => {
  // The half that survives OC changing how it derives session keys — the
  // failure mode that produced this incident in the first place.
  const h = makeHookHarness();
  await h.fire(`${EVOLVE_INTERNAL_PROMPT_SENTINEL}\n${ROUTER_PROMPT}`, {
    sessionKey: "agent:main:some:future:key:shape:OC:invents",
    runId: "run-nested-2",
  });
  assert.equal(h.calls.classify, 0,
    "the prompt marker must stop the loop even when the session key is unrecognisable");
});

test("each guard half works with the other absent", async () => {
  // Key recognised, no marker in the prompt.
  const a = makeHookHarness();
  await a.fire("some ordinary text", { sessionKey: PREFLIGHT_SESSION_KEY, runId: "r1" });
  assert.equal(a.calls.classify, 0, "key check alone must hold");

  // Marker present, key is an ordinary user session.
  const b = makeHookHarness();
  await b.fire(`${EVOLVE_INTERNAL_PROMPT_SENTINEL}\nanything`, {
    sessionKey: "agent:main:telegram:direct:55", runId: "r2",
  });
  assert.equal(b.calls.classify, 0, "sentinel check alone must hold");
});

test("CONTROL: a real user turn is still routed", async () => {
  // The guard must stop Evolve's own calls and nothing else. Without this the
  // three tests above would pass just as well on a hook that always returns {}.
  const h = makeHookHarness();
  await h.fire("should I use postgres or sqlite for this?", {
    sessionKey: "agent:main:telegram:direct:55", runId: "run-user-1",
  });
  assert.equal(h.calls.classify, 1, "an ordinary user turn must still reach the router");
});

test("the guard returns a no-op, never an override", async () => {
  // Returning {} means no systemAppend and no model override: the run proceeds
  // on the model OC already resolved. Nothing about which model answers.
  const h = makeHookHarness();
  const res = await h.fire(ROUTER_PROMPT, { sessionKey: PREFLIGHT_SESSION_KEY });
  assert.deepEqual(res, {}, "a guarded self-call must return an empty result");
});

test("the guard runs BEFORE the diag line it would otherwise flood", async () => {
  // 461 diag lines for one scheduled reminder is how this hid in the log.
  const h = makeHookHarness();
  await h.fire(ROUTER_PROMPT, { sessionKey: PREFLIGHT_SESSION_KEY });
  assert.equal(
    h.logs.filter(([, m]) => m.includes("before_model_resolve fired")).length, 0,
    "a guarded self-call must not emit the diag line",
  );
});

// ── 2. The sentinel is stamped on every plugin-internal model call ──────────

test("runPinnedSubagent stamps the marker on the prompt", async () => {
  _resetSubagentPinDenialForTest();
  _resetSubagentBreakerGateForTest();
  const seen = [];
  const api = { runtime: { subagent: { run: async (p) => { seen.push(p); return { runId: "r" }; } } } };
  const logger = { info() {}, warn() {}, error() {} };

  await runPinnedSubagent(api, logger, {
    idempotencyKey: "evolve:preflight:team_bot_a:1",
    message: ROUTER_PROMPT,
  });

  assert.equal(seen.length, 1);
  assert.ok(isEvolveInternalPrompt(seen[0].message),
    "the message OC receives must carry the marker");
  assert.ok(seen[0].message.endsWith(ROUTER_PROMPT),
    "the original prompt must survive intact after the marker");
});

test("the marker is not doubled when a prompt already carries it", async () => {
  _resetSubagentPinDenialForTest();
  _resetSubagentBreakerGateForTest();
  const seen = [];
  const api = { runtime: { subagent: { run: async (p) => { seen.push(p); return { runId: "r" }; } } } };
  const logger = { info() {}, warn() {}, error() {} };

  const already = `${EVOLVE_INTERNAL_PROMPT_SENTINEL}\n${ROUTER_PROMPT}`;
  await runPinnedSubagent(api, logger, {
    idempotencyKey: "evolve:session-judge:team_bot_a:1",
    message: already,
  });
  assert.equal(seen[0].message, already);
  assert.equal(
    seen[0].message.split(EVOLVE_INTERNAL_PROMPT_SENTINEL).length - 1, 1,
    "the marker must appear exactly once",
  );
});

test("every plugin-internal call site funnels through runPinnedSubagent", () => {
  // The marker is stamped in ONE place, so this is the property that makes
  // "all four call sites carry it" true — and the one a new call site breaks.
  const callSites = [
    "PreflightIntentRouter.ts",
    "LLMTierClassifier.ts",
    "SessionStruggleJudge.ts",
    "SessionSummarizer.ts",
  ];
  for (const f of callSites) {
    const src = fs.readFileSync(
      path.join(__dirname, "..", "src", "observer", f), "utf8",
    );
    assert.ok(/\brunPinnedSubagent\(/.test(src),
      `${f} must route its model call through runPinnedSubagent (it is where the marker is stamped)`);
    assert.ok(!/api\.runtime\.subagent\.run\(/.test(src),
      `${f} must not call api.runtime.subagent.run directly — that path skips the marker`);
  }
});

// ── 3. The cap ──────────────────────────────────────────────────────────────

test("cap: one classification per runId", () => {
  const cap = new PreflightCallCap();
  const s = "agent:main:telegram:direct:55";
  assert.equal(cap.admit("run-1", s, 1_000).admitted, true);
  assert.equal(cap.admit("run-1", s, 1_100).admitted, false,
    "a second classification under the same runId must be refused");
  assert.equal(cap.admit("run-2", s, 1_200).admitted, true,
    "a new turn must be admitted");
});

test("cap: ten per session per rolling minute, then pass-through", () => {
  // The runId ceiling alone would not have caught 2026-09-08: every nested
  // call arrived under its own fresh runId. This is the ceiling that bites.
  const cap = new PreflightCallCap();
  const s = "agent:main:explicit:evolve:preflight:team_bot_a:1";
  for (let i = 0; i < PreflightCallCap.PER_MINUTE_PER_SESSION; i++) {
    assert.equal(cap.admit(`run-${i}`, s, 1_000 + i).admitted, true, `call ${i} admitted`);
  }
  assert.equal(cap.admit("run-over", s, 1_050).admitted, false,
    "the 11th call in the window must be refused");
});

test("cap: the window rolls", () => {
  const cap = new PreflightCallCap();
  const s = "sess";
  for (let i = 0; i < 10; i++) cap.admit(`run-${i}`, s, 1_000 + i);
  assert.equal(cap.admit("run-x", s, 1_500).admitted, false);
  assert.equal(cap.admit("run-y", s, 1_000 + PreflightCallCap.WINDOW_MS + 1).admitted, true,
    "once the minute has passed the session is admitted again");
});

test("cap: one log line per session per window, not one per refusal", () => {
  // The mistake this hook already made once: 461 lines for one reminder.
  const cap = new PreflightCallCap();
  const s = "sess";
  for (let i = 0; i < 10; i++) cap.admit(`run-${i}`, s, 1_000 + i);

  const lines = [];
  for (let i = 0; i < 500; i++) {
    const v = cap.admit(`over-${i}`, s, 1_100 + i);
    assert.equal(v.admitted, false);
    if (v.logLine) lines.push(v.logLine);
  }
  assert.equal(lines.length, 1, "a loop must produce exactly one operator line");
  assert.match(lines[0], /UNROUTED/);
  assert.match(lines[0], /tier-router-self-call-loop/,
    "the line must point at the finding, so the next operator does not re-derive it");
});

test("cap: sessions are independent", () => {
  const cap = new PreflightCallCap();
  for (let i = 0; i < 10; i++) cap.admit(`loud-${i}`, "loud", 1_000 + i);
  // Distinct runIds, as OC issues them: the per-turn ceiling is global
  // because a runId identifies one turn pod-wide, so reusing one here would
  // test that ceiling rather than the per-session one.
  assert.equal(cap.admit("loud-over", "loud", 1_050).admitted, false);
  assert.equal(cap.admit("quiet-1", "quiet", 1_050).admitted, true,
    "one looping session must not cap an unrelated one");
});

test("cap: memory is bounded", () => {
  const cap = new PreflightCallCap();
  for (let i = 0; i < PreflightCallCap.MAX_TRACKED * 3; i++) {
    cap.admit(`run-${i}`, `sess-${i}`, 1_000 + i);
  }
  // Reaching here without exhausting memory is the assertion; the internals
  // are private, so check the observable consequence instead.
  assert.ok(cap.admit("fresh", "fresh-session").admitted);
});

// ── 4. The diag line prints the whole key ───────────────────────────────────

test("the diag line prints the FULL session key", async () => {
  // It printed 12 chars — "agent:main:e" — for every looping fire, cutting off
  // "explicit:evolve:preflight:…", which is the part that says whose call it is.
  const h = makeHookHarness();
  const key = "agent:main:telegram:direct:55";
  await h.fire("hello", { sessionKey: key, runId: "run-diag" });
  const diag = h.logs.find(([, m]) => m.includes("before_model_resolve fired"));
  assert.ok(diag, "the diag line must still be emitted for a real turn");
  assert.ok(diag[1].includes(`sessionKey=${key}`),
    `diag line must carry the whole key, got: ${diag[1].slice(0, 160)}`);
});

test("the diag line is not truncated by a source-level slice", () => {
  const src = fs.readFileSync(
    path.join(__dirname, "..", "src", "observer", "TurnObserver.ts"), "utf8",
  );
  const line = src.split("\n").find((l) => l.includes("before_model_resolve fired sessionKey="));
  assert.ok(line, "the diag line must exist");
  assert.ok(!/sessionKey=\$\{String\(sessionKey \?\? "<none>"\)\.slice\(/.test(line),
    "the session key must not be re-truncated");
});
