/**
 * Tests for SessionStruggleJudge — the LLM-as-judge layer that fires
 * when SessionStruggleAggregator's pre-thresholds suspect struggle.
 *
 * Architecture (2026-06-07 design conversation):
 *   "Cast a wide net with cheap regex/pattern features, then use LLM
 *    to look for actual fish."
 *
 * Tests pin:
 *   1. _parseJudgeResponse handles every reasonable + adversarial input
 *   2. _buildConversationSnippet produces snippet within budget
 *   3. shouldRunJudge pre-threshold gating (looser than aggregator's
 *      elevation thresholds)
 *   4. SessionStruggleJudge.judge() integration with mock API:
 *      - happy path: STRUGGLING / OK / AMBIGUOUS verdicts
 *      - fault tolerance: API throws / times out / null api → AMBIGUOUS
 *      - latency_ms is observed
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/sessionStruggleJudge.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  SessionStruggleJudge,
  _parseJudgeResponse,
  _buildConversationSnippet,
  shouldRunJudge,
} from "../dist/observer/SessionStruggleJudge.js";
import {
  _resetSubagentPinDenialForTest,
  subagentPinDenied,
} from "../dist/observer/subagentRun.js";

function fakeLogger() {
  return {
    debug: () => {}, info: () => {}, warn: () => {}, error: () => {},
  };
}

const FAKE_CONFIG = {
  botId: "team_bot_a",
  sharedDir: "/tmp",
  classifierModel: "anthropic/claude-haiku-4-5",
};

function makeJudgeApi(lastMessage) {
  const calls = [];
  return {
    calls,
    runtime: {
      subagent: {
        run: async (input) => {
          calls.push({ kind: "run", input });
          return { runId: "fake-run-id" };
        },
        waitForRun: async () => {
          calls.push({ kind: "wait" });
          return { lastMessage };
        },
      },
    },
  };
}

function makeFailingApi() {
  return {
    runtime: {
      subagent: {
        run: async () => { throw new Error("rate-limited"); },
        waitForRun: async () => ({ lastMessage: "STRUGGLING" }),
      },
    },
  };
}

// ── _parseJudgeResponse ─────────────────────────────────────────────────────

test("parseJudgeResponse: STRUGGLING with rationale", () => {
  const r = _parseJudgeResponse("STRUGGLING: user is pasting shell errors repeatedly");
  assert.equal(r.verdict, "STRUGGLING");
  assert.equal(r.reason, "user is pasting shell errors repeatedly");
});

test("parseJudgeResponse: OK with rationale", () => {
  const r = _parseJudgeResponse("OK: productive Q&A");
  assert.equal(r.verdict, "OK");
  assert.equal(r.reason, "productive Q&A");
});

test("parseJudgeResponse: AMBIGUOUS with rationale", () => {
  const r = _parseJudgeResponse("AMBIGUOUS: not enough context to tell");
  assert.equal(r.verdict, "AMBIGUOUS");
});

test("parseJudgeResponse: bare verdict (no rationale)", () => {
  const r = _parseJudgeResponse("STRUGGLING");
  assert.equal(r.verdict, "STRUGGLING");
  assert.equal(r.reason, "no_reason_given");
});

test("parseJudgeResponse: case-insensitive + extra whitespace", () => {
  const r = _parseJudgeResponse("  struggling : user frustrated  ");
  assert.equal(r.verdict, "STRUGGLING");
});

test("parseJudgeResponse: multi-verdict response → AMBIGUOUS (safe default)", () => {
  // Guard against the model giving "STRUGGLING or OK — depends" output.
  // Can't safely pick one; return AMBIGUOUS.
  const r = _parseJudgeResponse("STRUGGLING or OK — depends on whether you count tool errors");
  assert.equal(r.verdict, "AMBIGUOUS");
  assert.equal(r.reason, "multiple_verdicts");
});

test("parseJudgeResponse: garbage / unrecognized verdict → AMBIGUOUS", () => {
  assert.equal(_parseJudgeResponse("Hmm, I'm not sure").verdict, "AMBIGUOUS");
  assert.equal(_parseJudgeResponse("yes").verdict, "AMBIGUOUS");
  assert.equal(_parseJudgeResponse("12345").verdict, "AMBIGUOUS");
});

test("parseJudgeResponse: empty / null / undefined → AMBIGUOUS no_response", () => {
  assert.equal(_parseJudgeResponse("").verdict, "AMBIGUOUS");
  assert.equal(_parseJudgeResponse("").reason, "no_response");
  assert.equal(_parseJudgeResponse(null).verdict, "AMBIGUOUS");
  assert.equal(_parseJudgeResponse(undefined).verdict, "AMBIGUOUS");
});

// ── _buildConversationSnippet ───────────────────────────────────────────────

test("buildConversationSnippet: includes last N turns labeled USER/BOT", () => {
  const turns = [
    { userMessage: "first question", assistantMessage: "first answer" },
    { userMessage: "second question", assistantMessage: "second answer" },
  ];
  const s = _buildConversationSnippet(turns);
  assert.ok(s.includes("USER: first question"));
  assert.ok(s.includes("BOT: first answer"));
  assert.ok(s.includes("USER: second question"));
  assert.ok(s.includes("BOT: second answer"));
});

test("buildConversationSnippet: caps at last 5 turns (most recent)", () => {
  const turns = [];
  for (let i = 0; i < 8; i++) {
    turns.push({ userMessage: `q${i}`, assistantMessage: `a${i}` });
  }
  const s = _buildConversationSnippet(turns);
  // First 3 turns dropped
  assert.equal(s.includes("USER: q0"), false);
  assert.equal(s.includes("USER: q1"), false);
  assert.equal(s.includes("USER: q2"), false);
  // Last 5 included
  assert.equal(s.includes("USER: q3"), true);
  assert.equal(s.includes("USER: q7"), true);
});

test("buildConversationSnippet: caps total length at 2000 chars", () => {
  const big = "x".repeat(5000);
  const turns = [];
  for (let i = 0; i < 5; i++) {
    turns.push({ userMessage: big, assistantMessage: big });
  }
  const s = _buildConversationSnippet(turns);
  assert.ok(s.length <= 2000, `snippet should be ≤2000 chars, got ${s.length}`);
});

test("buildConversationSnippet: handles empty messages gracefully", () => {
  const turns = [
    { userMessage: "", assistantMessage: "" },
    { userMessage: "real question", assistantMessage: "" },
  ];
  const s = _buildConversationSnippet(turns);
  // Empty turn dropped; non-empty user msg included
  assert.ok(s.includes("USER: real question"));
});

// ── shouldRunJudge (pre-threshold gating) ──────────────────────────────────

test("shouldRunJudge: returns null when no signal trips", () => {
  const r = shouldRunJudge({
    shell_error_paste_count: 0,
    bot_self_correction_count: 0,
    turn_velocity_per_min: 0.1,
    turn_count: 2,
  });
  assert.equal(r, null);
});

test("shouldRunJudge: single shell paste triggers", () => {
  const r = shouldRunJudge({
    shell_error_paste_count: 1,
    bot_self_correction_count: 0,
    turn_velocity_per_min: null,
    turn_count: 2,
  });
  assert.equal(r, "shell_paste");
});

test("shouldRunJudge: single self-correction triggers", () => {
  const r = shouldRunJudge({
    shell_error_paste_count: 0,
    bot_self_correction_count: 1,
    turn_velocity_per_min: null,
    turn_count: 2,
  });
  assert.equal(r, "self_correction");
});

test("shouldRunJudge: velocity triggers ONLY with sufficient turn count", () => {
  // 5 turns at 1.0/min — should trigger
  const r1 = shouldRunJudge({
    shell_error_paste_count: 0,
    bot_self_correction_count: 0,
    turn_velocity_per_min: 1.0,
    turn_count: 5,
  });
  assert.equal(r1, "velocity");
  // 2 turns at 10/min — too few turns to judge cadence
  const r2 = shouldRunJudge({
    shell_error_paste_count: 0,
    bot_self_correction_count: 0,
    turn_velocity_per_min: 10.0,
    turn_count: 2,
  });
  assert.equal(r2, null);
});

test("shouldRunJudge: multiple triggers → 'multiple'", () => {
  const r = shouldRunJudge({
    shell_error_paste_count: 2,
    bot_self_correction_count: 1,
    turn_velocity_per_min: null,
    turn_count: 3,
  });
  assert.equal(r, "multiple");
});

// ── SessionStruggleJudge.judge() integration ─────────────────────────────────

test("judge: returns STRUGGLING when API responds STRUGGLING", async () => {
  const api = makeJudgeApi("STRUGGLING: user pasted three shell errors");
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), api);
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "USER: copy these files\nBOT: try scp...\nUSER: permission denied",
    triggeredBy: "shell_paste",
  });
  assert.equal(decision.verdict, "STRUGGLING");
  assert.equal(decision.triggered_by, "shell_paste");
  assert.ok(decision.latency_ms >= 0);
  assert.ok(api.calls.length === 2);  // run + wait
});

test("judge: returns OK when API responds OK", async () => {
  const api = makeJudgeApi("OK: productive exchange");
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), api);
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "USER: hi\nBOT: hello, how can I help?",
    triggeredBy: "shell_paste",
  });
  assert.equal(decision.verdict, "OK");
});

test("judge: returns AMBIGUOUS when API throws", async () => {
  const api = makeFailingApi();
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), api);
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "snippet",
    triggeredBy: "self_correction",
  });
  // Failure must NOT throw into the hot path — verdict = AMBIGUOUS
  assert.equal(decision.verdict, "AMBIGUOUS");
  assert.equal(decision.reason, "judge_failed");
});

test("judge: returns AMBIGUOUS when api is null", async () => {
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), null);
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "snippet",
    triggeredBy: "velocity",
  });
  assert.equal(decision.verdict, "AMBIGUOUS");
  assert.equal(decision.reason, "no_api");
});

test("judge: returns AMBIGUOUS when api is partial stub (no subagent)", async () => {
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), {});
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "snippet",
    triggeredBy: "shell_paste",
  });
  assert.equal(decision.verdict, "AMBIGUOUS");
  assert.equal(decision.reason, "no_api");
});

test("judge: empty snippet returns AMBIGUOUS without API call", async () => {
  const api = makeJudgeApi("STRUGGLING: should not see this");
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), api);
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "",
    triggeredBy: "shell_paste",
  });
  assert.equal(decision.verdict, "AMBIGUOUS");
  assert.equal(decision.reason, "empty_snippet");
  // No API call attempted
  assert.equal(api.calls.length, 0);
});

test("judge: multi-verdict API response → AMBIGUOUS", async () => {
  const api = makeJudgeApi("STRUGGLING or OK depending on context");
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), api);
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "real snippet",
    triggeredBy: "shell_paste",
  });
  assert.equal(decision.verdict, "AMBIGUOUS");
});

test("judge: passes through triggered_by to decision", async () => {
  const api = makeJudgeApi("STRUGGLING: yes");
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), api);
  for (const triggeredBy of ["shell_paste", "self_correction", "velocity", "multiple"]) {
    const decision = await judge.judge({
      botId: "team_bot_a",
      conversationSnippet: "snippet",
      triggeredBy,
    });
    assert.equal(decision.triggered_by, triggeredBy);
  }
});

// ── Cross-vendor judge derivation (design judge-role-collapse §5.3, J2) ─────
//
// The judge tries the injected cross-vendor resolver first (against
// "standard"); on null/absent/throw it falls back to classifierModel exactly
// as before, and stamps cross_vendor on the decision either way so the span's
// judge-accuracy grading can segment self-judged from cross-judged verdicts.

test("judge: cross-vendor resolver model is used and stamped cross_vendor:true", async () => {
  const api = makeJudgeApi("OK: fine");
  const resolverCalls = [];
  const judge = new SessionStruggleJudge(
    FAKE_CONFIG,
    fakeLogger(),
    api,
    (againstRole) => { resolverCalls.push(againstRole); return "openai/gpt-4.1"; },
  );
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "snippet",
    triggeredBy: "shell_paste",
  });
  assert.equal(decision.cross_vendor, true);
  // The derivation runs against "standard" (every current caller's default).
  assert.deepEqual(resolverCalls, ["standard"]);
  // The subagent run was pinned to the derived model, not classifierModel.
  const run = api.calls.find((c) => c.kind === "run");
  assert.equal(run.input.model, "openai/gpt-4.1");
});

test("judge: resolver null → falls back to classifierModel, cross_vendor:false", async () => {
  const api = makeJudgeApi("OK: fine");
  const judge = new SessionStruggleJudge(
    FAKE_CONFIG,
    fakeLogger(),
    api,
    () => null,  // single-provider pod — None is meaningful (design §5.2)
  );
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "snippet",
    triggeredBy: "shell_paste",
  });
  assert.equal(decision.cross_vendor, false);
  const run = api.calls.find((c) => c.kind === "run");
  assert.equal(run.input.model, FAKE_CONFIG.classifierModel);
});

test("judge: no resolver wired → classifierModel, cross_vendor:false (today's behavior)", async () => {
  const api = makeJudgeApi("STRUGGLING: yes");
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), api);
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "snippet",
    triggeredBy: "velocity",
  });
  assert.equal(decision.cross_vendor, false);
  const run = api.calls.find((c) => c.kind === "run");
  assert.equal(run.input.model, FAKE_CONFIG.classifierModel);
});

test("judge: resolver throwing falls back cleanly, cross_vendor:false", async () => {
  const api = makeJudgeApi("OK: fine");
  const judge = new SessionStruggleJudge(
    FAKE_CONFIG,
    fakeLogger(),
    api,
    () => { throw new Error("catalog unreadable"); },
  );
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "snippet",
    triggeredBy: "shell_paste",
  });
  assert.equal(decision.verdict, "OK");
  assert.equal(decision.cross_vendor, false);
  const run = api.calls.find((c) => c.kind === "run");
  assert.equal(run.input.model, FAKE_CONFIG.classifierModel);
});

test("judge: early exits still carry the cross_vendor field", async () => {
  const judge = new SessionStruggleJudge(FAKE_CONFIG, fakeLogger(), null, () => "openai/gpt-4.1");
  const decision = await judge.judge({
    botId: "team_bot_a",
    conversationSnippet: "snippet",
    triggeredBy: "velocity",
  });
  assert.equal(decision.reason, "no_api");
  // No judge ran — but the field is present so the span shape is stable.
  assert.equal(typeof decision.cross_vendor, "boolean");
});

// ── cross_vendor vs the sticky subagent pin denial (post-#3911) ─────────────
//
// runPinnedSubagent swallows OC's pin-authorization rejection, retries
// UNPINNED, and remembers the denial per process — so once a pin is denied,
// every judge run executes on the bot's session-default model no matter what
// _pickJudgeModel derived. The span flag must never claim a cross-vendor run
// that cannot happen: the judge consults subagentPinDenied() at pick time
// (skip the derivation) AND at stamp time (the run that DISCOVERS the denial
// also stamps false).

const PIN_DENIAL_ERROR = new Error(
  "provider/model override is not authorized for this plugin subagent run.",
);

/** api whose subagent.run rejects any pinned attempt with OC's auth error. */
function makePinDenyingApi(lastMessage) {
  const calls = [];
  return {
    calls,
    runtime: {
      subagent: {
        run: async (input) => {
          calls.push({ kind: "run", input });
          if (input.model) throw PIN_DENIAL_ERROR;
          return { runId: "fake-run-id" };
        },
        waitForRun: async () => ({ lastMessage }),
      },
    },
  };
}

test("judge: pin denied DURING the run → cross_vendor:false (stamp-time recheck)", async () => {
  _resetSubagentPinDenialForTest();
  try {
    const api = makePinDenyingApi("OK: fine");
    const judge = new SessionStruggleJudge(
      FAKE_CONFIG,
      fakeLogger(),
      api,
      () => "openai/gpt-4.1",
    );
    const decision = await judge.judge({
      botId: "team_bot_a",
      conversationSnippet: "snippet",
      triggeredBy: "shell_paste",
    });
    // The pinned attempt was made and denied; the unpinned retry succeeded.
    const runs = api.calls.filter((c) => c.kind === "run");
    assert.equal(runs.length, 2);
    assert.equal(runs[0].input.model, "openai/gpt-4.1");
    assert.equal(runs[1].input.model, undefined);
    assert.equal(decision.verdict, "OK");
    // The judge actually ran on the session-default model — the span must
    // not claim cross-vendor even though the derivation picked one.
    assert.equal(decision.cross_vendor, false);
  } finally {
    _resetSubagentPinDenialForTest();
  }
});

test("judge: sticky pin denial → derivation skipped, unpinned run, cross_vendor:false", async () => {
  _resetSubagentPinDenialForTest();
  try {
    // First call trips the denial (as above); the state is per-process.
    const denyApi = makePinDenyingApi("OK: fine");
    const resolverCalls = [];
    const mkJudge = (api) => new SessionStruggleJudge(
      FAKE_CONFIG,
      fakeLogger(),
      api,
      (againstRole) => { resolverCalls.push(againstRole); return "openai/gpt-4.1"; },
    );
    await mkJudge(denyApi).judge({
      botId: "team_bot_a",
      conversationSnippet: "snippet",
      triggeredBy: "shell_paste",
    });
    assert.equal(subagentPinDenied(), true);
    assert.equal(resolverCalls.length, 1);

    // Second call (fresh judge instance — the denial is process-wide): the
    // derivation is not even consulted, the run goes out unpinned, and the
    // span stamps self-judged.
    const api = makeJudgeApi("STRUGGLING: yes");
    const decision = await mkJudge(api).judge({
      botId: "team_bot_a",
      conversationSnippet: "snippet",
      triggeredBy: "velocity",
    });
    assert.equal(resolverCalls.length, 1); // unchanged — derivation skipped
    const run = api.calls.find((c) => c.kind === "run");
    assert.equal(run.input.model, undefined);
    assert.equal(decision.verdict, "STRUGGLING");
    assert.equal(decision.cross_vendor, false);
  } finally {
    _resetSubagentPinDenialForTest();
  }
});

test("judge: pin honored → cross_vendor:true unchanged", async () => {
  _resetSubagentPinDenialForTest();
  try {
    const api = makeJudgeApi("OK: fine");
    const judge = new SessionStruggleJudge(
      FAKE_CONFIG,
      fakeLogger(),
      api,
      () => "openai/gpt-4.1",
    );
    const decision = await judge.judge({
      botId: "team_bot_a",
      conversationSnippet: "snippet",
      triggeredBy: "shell_paste",
    });
    assert.equal(decision.cross_vendor, true);
    const run = api.calls.find((c) => c.kind === "run");
    assert.equal(run.input.model, "openai/gpt-4.1");
  } finally {
    _resetSubagentPinDenialForTest();
  }
});
