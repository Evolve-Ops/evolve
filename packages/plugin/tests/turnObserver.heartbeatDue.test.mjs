/**
 * TurnObserver × the heartbeat due-check (D-OH3).
 *
 * These tests assert the SEAM, not the arithmetic (heartbeatDueCheck.test.mjs
 * covers the conditions): does `before_agent_reply` claim the turn — which is
 * what makes OC short-circuit to NO_REPLY without dispatching a model — and
 * does it refuse to claim in every case where claiming would be wrong.
 *
 * Verified hook contract (OC 2026.9.2 bundle, `runEmbeddedAgent`):
 * `runBeforeAgentReplyForTurn` is awaited BEFORE `executePreparedEmbeddedRun`
 * and a truthy `handled` returns `buildHandledBeforeAgentReplyPayloads` —
 * i.e. NO_REPLY — so a claim here is a model call that never happens.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { TurnObserver } from "../dist/observer/TurnObserver.js";
import { decisionsFilePath } from "../dist/observer/HeartbeatSkipLedger.js";

/**
 * Seed the due-state so the max-silence floor is not what a test is
 * measuring. Without it every fresh shared dir reads as "this bot has never
 * woken", which is past the floor by construction — correct in production
 * (see the floor tests below), noise in a test about conditions.
 */
function seedLastWoke(shared, botId = "test_bot", at = new Date()) {
  const dir = path.join(shared, botId, "turns");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "heartbeat-due-state.json"),
    JSON.stringify({ version: 1, conditions: {}, lastWokeAt: at.toISOString() }),
  );
}

function makeObserver({ seedWake = true, api = undefined, network = null } = {}) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-hb-obs-"));
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-hb-ws-"));
  if (network) {
    fs.writeFileSync(path.join(shared, "network.json"), JSON.stringify(network, null, 2));
  }
  if (seedWake) seedLastWoke(shared);
  const logs = [];
  const logger = {
    info: (m) => logs.push(["info", String(m)]),
    warn: (m) => logs.push(["warn", String(m)]),
    error: (m) => logs.push(["error", String(m)]),
    debug: () => {},
  };
  const config = {
    botId: "test_bot",
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
  };
  return { observer: new TurnObserver(config, logger, api), shared, workspace, logs, config, logger };
}

const CRON_JOB = "9506f538-340e-4487-ae07-5675cb58b48c";

function writeConditions(ws, conditions, extra = {}) {
  fs.writeFileSync(
    path.join(ws, "HEARTBEAT.json"),
    JSON.stringify({ version: 1, conditions, ...extra }, null, 2),
  );
}

function decisionsFor(shared, botId = "test_bot") {
  const day = new Date().toISOString().slice(0, 10);
  const p = decisionsFilePath(shared, botId, day);
  if (!fs.existsSync(p)) return [];
  return fs.readFileSync(p, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

test("nothing due → handled:true (no model call) + one zero-cost record", () => {
  const { observer, shared, workspace } = makeObserver();
  fs.mkdirSync(path.join(workspace, "inbox"));
  writeConditions(workspace, [
    { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process the inbox." },
  ]);

  const result = observer.handleBeforeAgentReply("run-1", {
    trigger: "heartbeat",
    workspaceDir: workspace,
    sessionId: "sess-1",
  });

  assert.equal(result?.handled, true);
  assert.match(result.reason, /nothing due/);

  const records = decisionsFor(shared);
  assert.equal(records.length, 1);
  assert.equal(records[0].outcome, "skipped_nothing_due");
  assert.equal(records[0].cost, 0);
  assert.equal(records[0].cost_source, "no_model_call");
  assert.equal(records[0].source, "heartbeat");
  assert.equal(records[0].run_id, "run-1");
  assert.equal(records[0].session_id, "sess-1");
});

test("one condition due → the model runs and the turn carries only that item", () => {
  const { observer, shared, workspace } = makeObserver();
  fs.mkdirSync(path.join(workspace, "inbox"));
  fs.writeFileSync(path.join(workspace, "inbox", "todo.md"), "x");
  fs.mkdirSync(path.join(workspace, "reports"));
  writeConditions(workspace, [
    { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process the inbox." },
    { id: "reports", when: "dir_non_empty", path: "reports", wake: "File the reports." },
  ]);

  const result = observer.handleBeforeAgentReply("run-2", {
    trigger: "heartbeat",
    workspaceDir: workspace,
    sessionId: "sess-2",
  });
  assert.equal(result, undefined, "a due heartbeat must NOT be claimed");

  const records = decisionsFor(shared);
  assert.equal(records.length, 1);
  assert.equal(records[0].outcome, "woke_due");
  assert.deepEqual(records[0].due_ids, ["inbox"]);

  // The woken run is registered for the prompt-build narrowing.
  const due = observer._dueWakeByRun.get("run-2");
  assert.ok(due, "the run must be registered for before_prompt_build");
  assert.deepEqual(due.map((d) => d.id), ["inbox"]);
});

test("a user turn that talks about heartbeats is never claimed", () => {
  const { observer, shared, workspace } = makeObserver();
  fs.mkdirSync(path.join(workspace, "inbox"));
  writeConditions(workspace, [
    { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process the inbox." },
  ]);

  for (const trigger of ["user", "human", "", undefined]) {
    const result = observer.handleBeforeAgentReply("run-user", {
      trigger,
      workspaceDir: workspace,
      sessionId: "sess-u",
      // The message body is irrelevant — the gate reads ctx.trigger only.
      cleanedBody: "what does the heartbeat do when nothing is due?",
    });
    assert.equal(result, undefined, `trigger ${JSON.stringify(trigger)} must not be claimed`);
  }
  assert.deepEqual(decisionsFor(shared), [], "a user turn writes no heartbeat decision");
});

test("missing conditions → model runs, no warning, no record", () => {
  const { observer, shared, workspace, logs } = makeObserver();
  const result = observer.handleBeforeAgentReply("run-3", {
    trigger: "heartbeat",
    workspaceDir: workspace,
    sessionId: "sess-3",
  });
  assert.equal(result, undefined);
  assert.deepEqual(decisionsFor(shared), []);
  assert.deepEqual(logs.filter(([lvl]) => lvl === "warn"), []);
});

test("invalid conditions → model runs, exactly one warning across many ticks", () => {
  const { observer, shared, workspace, logs } = makeObserver();
  fs.writeFileSync(path.join(workspace, "HEARTBEAT.json"), "{ broken");

  for (let i = 0; i < 5; i++) {
    const result = observer.handleBeforeAgentReply(`run-bad-${i}`, {
      trigger: "heartbeat",
      workspaceDir: workspace,
      sessionId: "sess-bad",
    });
    assert.equal(result, undefined, "an unparseable file must never suppress a turn");
  }
  const warns = logs.filter(([lvl]) => lvl === "warn");
  assert.equal(warns.length, 1);
  assert.match(warns[0][1], /invalid/);
  assert.deepEqual(decisionsFor(shared), []);
});

test("an unconditioned cron job is NEVER claimed, whatever the heartbeat says", () => {
  // A cron job carries its own instruction ("post the digest") and merely
  // shares the workspace. Gating it on the heartbeat's conditions is how a
  // cron silently stops firing — so a job the file does not name is never
  // claimed, and no decision record pretends otherwise.
  const { observer, shared, workspace } = makeObserver();
  fs.mkdirSync(path.join(workspace, "inbox"));
  writeConditions(workspace, [
    { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process the inbox." },
  ]);

  // The heartbeat itself has nothing due — the state the old behaviour used
  // to claim a cron with.
  const heartbeat = observer.handleBeforeAgentReply("run-hb", {
    trigger: "heartbeat",
    workspaceDir: workspace,
    sessionId: "sess-hb",
  });
  assert.equal(heartbeat?.handled, true);

  for (const ctx of [
    { trigger: "cron", workspaceDir: workspace, sessionId: "sess-c1", jobId: CRON_JOB },
    { trigger: "cron", workspaceDir: workspace, sessionId: "sess-c2" },
  ]) {
    assert.equal(
      observer.handleBeforeAgentReply("run-cron", ctx),
      undefined,
      `cron job ${ctx.jobId ?? "<no id>"} must run: the file does not scope it`,
    );
  }
  assert.deepEqual(
    decisionsFor(shared).map((r) => r.source),
    ["heartbeat"],
    "an unclaimed cron writes no decision record",
  );
});

test("a cron job the file scopes skips and wakes on its own conditions", () => {
  const { observer, shared, workspace } = makeObserver();
  fs.mkdirSync(path.join(workspace, "outbox"));
  fs.mkdirSync(path.join(workspace, "inbox"));
  fs.writeFileSync(path.join(workspace, "inbox", "todo.md"), "x");
  writeConditions(
    workspace,
    // The heartbeat has work; the cron job does not. Neither answers for the
    // other.
    [{ id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process the inbox." }],
    {
      cron: {
        [CRON_JOB]: [
          { id: "outbox", when: "dir_non_empty", path: "outbox", wake: "Send the outbox." },
        ],
      },
    },
  );

  const skipped = observer.handleBeforeAgentReply("run-cron-1", {
    trigger: "cron",
    workspaceDir: workspace,
    sessionId: "sess-c1",
    jobId: CRON_JOB,
  });
  assert.equal(skipped?.handled, true);
  assert.match(skipped.reason, /^cron: nothing due$/);

  fs.writeFileSync(path.join(workspace, "outbox", "msg.md"), "x");
  const woke = observer.handleBeforeAgentReply("run-cron-2", {
    trigger: "cron",
    workspaceDir: workspace,
    sessionId: "sess-c2",
    jobId: CRON_JOB,
  });
  assert.equal(woke, undefined);

  const records = decisionsFor(shared);
  assert.deepEqual(records.map((r) => r.outcome), ["skipped_nothing_due", "woke_due"]);
  assert.deepEqual(records.map((r) => r.source), ["cron", "cron"]);
  assert.deepEqual(records.map((r) => r.cron_job_id), [CRON_JOB, CRON_JOB]);
  assert.deepEqual(records[1].due_ids, ["outbox"]);
});

test("a cron wake does not mark a heartbeat condition served", () => {
  const { observer, workspace } = makeObserver();
  writeConditions(
    workspace,
    [{ id: "sweep", when: "every", interval: "6h", wake: "Run the sweep." }],
    {
      cron: { [CRON_JOB]: [{ id: "sweep", when: "every", interval: "6h", wake: "Back up." }] },
    },
  );

  const cronWake = observer.handleBeforeAgentReply("run-cron", {
    trigger: "cron",
    workspaceDir: workspace,
    jobId: CRON_JOB,
  });
  assert.equal(cronWake, undefined, "the cron job's own 'sweep' has never run");

  const heartbeat = observer.handleBeforeAgentReply("run-hb", {
    trigger: "heartbeat",
    workspaceDir: workspace,
  });
  assert.equal(
    heartbeat,
    undefined,
    "the heartbeat's 'sweep' is still due — the cron wake was a different scope",
  );
});

test("a direct-sent run is still claimed for its own reason, not the heartbeat's", () => {
  const { observer, workspace } = makeObserver();
  writeConditions(workspace, [
    { id: "inbox", when: "path_exists", path: "inbox", wake: "Process." },
  ]);
  observer._directSentRuns.set("run-direct", "brief");
  const result = observer.handleBeforeAgentReply("run-direct", {
    trigger: "user",
    workspaceDir: workspace,
  });
  assert.equal(result?.handled, true);
  assert.match(result.reason, /direct-sent/);
});

test("a wake is not re-served: state is committed before the model runs", () => {
  const { observer, workspace } = makeObserver();
  writeConditions(workspace, [
    { id: "sweep", when: "every", interval: "6h", wake: "Sweep." },
  ]);
  const first = observer.handleBeforeAgentReply("run-a", {
    trigger: "heartbeat",
    workspaceDir: workspace,
  });
  assert.equal(first, undefined, "first tick wakes the model");

  const second = observer.handleBeforeAgentReply("run-b", {
    trigger: "heartbeat",
    workspaceDir: workspace,
  });
  assert.equal(second?.handled, true, "the next tick inside the interval is skipped");
});

// ── the floor, at the seam ─────────────────────────────────────────────────

test("past the max-silence floor the model runs, whatever the file says", () => {
  // The threat this closes: HEARTBEAT.json is in the bot's own workspace, so
  // the bot's model can write the file that decides whether it ever wakes.
  // This file is valid, enabled, and can never fire.
  const { observer, shared, workspace } = makeObserver({
    seedWake: false,
    network: { heartbeat: { max_silence: "24h" } },
  });
  writeConditions(workspace, [
    { id: "never", when: "path_exists", path: "never/there", wake: "Never." },
  ]);

  const first = observer.handleBeforeAgentReply("run-floor", {
    trigger: "heartbeat",
    workspaceDir: workspace,
    sessionId: "sess-floor",
  });
  assert.equal(first, undefined, "a bot with no recorded wake is past the floor");

  const records = decisionsFor(shared);
  assert.equal(records.length, 1);
  assert.equal(records[0].outcome, "woke_due");
  assert.equal(records[0].reason, "floor: no wake in 24h");
  assert.deepEqual(records[0].due_ids, []);
  assert.match(records[0].conditions_sha256, /^[0-9a-f]{64}$/);

  // The floor wake committed lastWokeAt, so the window has restarted and the
  // conditions have the last word again.
  const second = observer.handleBeforeAgentReply("run-floor-2", {
    trigger: "heartbeat",
    workspaceDir: workspace,
    sessionId: "sess-floor",
  });
  assert.equal(second?.handled, true);
  assert.equal(
    observer._dueWakeByRun.get("run-floor"),
    undefined,
    "a floor wake names no due items, so before_prompt_build prepends nothing",
  );
});

test("a conditions file that changes mid-process warns once and is on the record", () => {
  const { observer, shared, workspace, logs } = makeObserver();
  fs.mkdirSync(path.join(workspace, "inbox"));
  writeConditions(workspace, [
    { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process the inbox." },
  ]);
  observer.handleBeforeAgentReply("run-h1", { trigger: "heartbeat", workspaceDir: workspace });
  assert.deepEqual(logs.filter(([lvl]) => lvl === "warn"), []);

  writeConditions(workspace, [
    { id: "never", when: "path_exists", path: "never/there", wake: "Never." },
  ]);
  observer.handleBeforeAgentReply("run-h2", { trigger: "heartbeat", workspaceDir: workspace });
  observer.handleBeforeAgentReply("run-h3", { trigger: "heartbeat", workspaceDir: workspace });

  const warns = logs.filter(([lvl]) => lvl === "warn");
  assert.equal(warns.length, 1, "one line per process, not one per tick");
  assert.match(warns[0][1], /changed while the gateway was running/);

  const shas = decisionsFor(shared).map((r) => r.conditions_sha256);
  assert.equal(shas.length, 3);
  assert.notEqual(shas[0], shas[1], "the digest is what makes the rewrite visible");
  assert.equal(shas[1], shas[2]);
});

test("a heartbeat with no workspaceDir on the ctx warns once, then stays quiet", () => {
  // A NEW dependency on OC's hook-context shape. If a point release renames
  // the field, every bot silently returns to full idle burn and the receipt
  // still reads "0 skipped" — so it says so out loud, once.
  const { observer, shared, logs } = makeObserver();
  for (let i = 0; i < 4; i++) {
    const result = observer.handleBeforeAgentReply(`run-nows-${i}`, {
      trigger: i % 2 === 0 ? "heartbeat" : "cron",
      sessionId: "sess-nows",
      jobId: CRON_JOB,
    });
    assert.equal(result, undefined, "no workspaceDir must never suppress a turn");
  }
  const warns = logs.filter(([lvl]) => lvl === "warn");
  assert.equal(warns.length, 1);
  assert.match(warns[0][1], /no workspaceDir on the hook context/);
  assert.deepEqual(decisionsFor(shared), []);
});

// ── zero model calls, through the hooks OC actually fires ──────────────────

/**
 * OC 2026.9.2's default heartbeat prompt, read out of the live bundle
 * (`dist/heartbeat-*.js`: `HEARTBEAT_CONTEXT_PROMPT` + the silent-reply
 * sentence). This is the text that reaches `event.prompt` on a heartbeat
 * turn — the input the preflight router would classify if the trigger gate
 * were not there.
 */
const OC_HEARTBEAT_PROMPT =
  "Follow the heartbeat monitor scratch context when provided. Recurring tasks " +
  "are automations; create or change their schedules with the automations tool, " +
  "not heartbeat scratch. Do not infer or repeat old tasks from prior chats. " +
  "If nothing needs attention, reply HEARTBEAT_OK.";

/** A fake OC plugin api that captures hooks and counts subagent runs. */
function fakeApi() {
  const handlers = new Map();
  const state = { subagentRuns: 0 };
  return {
    api: {
      on(name, handler) {
        const list = handlers.get(name) ?? [];
        list.push(handler);
        handlers.set(name, list);
      },
      runtime: {
        subagent: {
          async run() {
            state.subagentRuns += 1;
            return { runId: `sub-${state.subagentRuns}` };
          },
          // BOTH halves, deliberately: the preflight router's haiku layer
          // skips itself when `waitForRun` is missing, so a fake without it
          // would satisfy "zero model calls" by accident. With it, an
          // ungated heartbeat prompt reaches the layer and the counter moves.
          async waitForRun() {
            return { lastMessage: "tier3" };
          },
        },
      },
    },
    state,
    async fire(name, event, ctx) {
      let last;
      for (const handler of handlers.get(name) ?? []) last = await handler(event, ctx);
      return last;
    },
    registered: (name) => (handlers.get(name) ?? []).length,
  };
}

/**
 * Drive the four hooks OC fires around a turn, in OC's order, with a
 * heartbeat context. Returns what each returned plus the subagent count.
 */
async function runHeartbeatTurn(hooks, observer, { runId, workspaceDir, jobId, trigger = "heartbeat" }) {
  const ctx = {
    runId,
    jobId,
    trigger,
    sessionId: `sess-${runId}`,
    sessionKey: `agent:main:heartbeat:${runId}`,
    workspaceDir,
    channel: "heartbeat",
    modelId: "claude-haiku-4-5-20251001",
    modelProviderId: "anthropic",
  };
  const event = { prompt: OC_HEARTBEAT_PROMPT, runId };
  const beforeModelResolve = await hooks.fire("before_model_resolve", event, ctx);
  const beforeAgentRun = await hooks.fire(
    "before_agent_run",
    { ...event, cleanedBody: OC_HEARTBEAT_PROMPT },
    ctx,
  );
  const beforePromptBuild = await hooks.fire("before_prompt_build", event, ctx);
  const beforeAgentReply = await hooks.fire(
    "before_agent_reply",
    { cleanedBody: OC_HEARTBEAT_PROMPT },
    ctx,
  );
  return { beforeModelResolve, beforeAgentRun, beforePromptBuild, beforeAgentReply };
}

/**
 * network.json with BOTH per-turn model-call gates open: the preflight
 * router enabled AND the classifier gate (the switch that lets its haiku
 * layer spend money) on at a sample rate of 1. Without this the "zero model
 * calls" assertion would be satisfied by the fail-closed default rather than
 * by the heartbeat path itself, which is the thing under test.
 */
const EVERY_GATE_OPEN = {
  cascade: {
    preflight: { enabled: true },
    classifiers: { enabled: true, sample_rate: 1, sample_rate_with_prior: 1 },
  },
};

test("nothing due: the registered hooks make ZERO model calls and claim the turn", async () => {
  const hooks = fakeApi();
  const { observer, shared, workspace } = makeObserver({
    api: hooks.api,
    network: EVERY_GATE_OPEN,
  });
  observer.register(hooks.api);
  assert.ok(hooks.registered("before_agent_reply") > 0, "the gate must be registered");
  assert.ok(hooks.registered("before_model_resolve") > 0);

  fs.mkdirSync(path.join(workspace, "inbox"));
  writeConditions(workspace, [
    { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process the inbox." },
  ]);

  const out = await runHeartbeatTurn(hooks, observer, {
    runId: "run-hooks-1",
    workspaceDir: workspace,
  });

  assert.equal(
    hooks.state.subagentRuns,
    0,
    "an idle heartbeat must not spend a model call on Evolve's own helpers",
  );
  assert.equal(out.beforeAgentReply?.handled, true);
  assert.match(out.beforeAgentReply.reason, /nothing due/);
  const records = decisionsFor(shared);
  assert.equal(records.length, 1);
  assert.equal(records[0].outcome, "skipped_nothing_due");
  assert.equal(records[0].cost, 0);
});

test("something due: before_prompt_build prepends only the due item, still zero model calls", async () => {
  const hooks = fakeApi();
  const { observer, workspace } = makeObserver({
    api: hooks.api,
    network: EVERY_GATE_OPEN,
  });
  observer.register(hooks.api);

  fs.mkdirSync(path.join(workspace, "inbox"));
  fs.writeFileSync(path.join(workspace, "inbox", "todo.md"), "x");
  fs.mkdirSync(path.join(workspace, "reports"));
  writeConditions(workspace, [
    { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process the inbox." },
    { id: "reports", when: "dir_non_empty", path: "reports", wake: "File the reports." },
  ]);

  // before_agent_reply runs BEFORE the model dispatch and is what registers
  // the run for the narrowing, so fire it first here and then the prompt
  // build, mirroring the wake path rather than the claim path.
  const claim = observer.handleBeforeAgentReply("run-hooks-2", {
    trigger: "heartbeat",
    workspaceDir: workspace,
    sessionId: "sess-hooks-2",
  });
  assert.equal(claim, undefined, "a due heartbeat is not claimed");

  const built = await hooks.fire(
    "before_prompt_build",
    { prompt: OC_HEARTBEAT_PROMPT },
    { runId: "run-hooks-2", trigger: "heartbeat", sessionId: "sess-hooks-2", workspaceDir: workspace },
  );
  assert.ok(built?.prependContext, "the woken turn must carry the due items");
  assert.match(built.prependContext, /Process the inbox\./);
  assert.ok(
    !built.prependContext.includes("File the reports."),
    "only what is due — the point of the narrowing",
  );
  assert.equal(hooks.state.subagentRuns, 0);
});

test("the harness itself would see a model call — the fake is not the reason the count is 0", async () => {
  // Guards the two tests above from passing for the wrong reason. Same
  // observer, same open gates, same prompt — but a `user` trigger, which the
  // preflight router is still allowed to classify. If this stops counting,
  // the zero-call assertions above have stopped meaning anything.
  const hooks = fakeApi();
  const { observer, workspace } = makeObserver({ api: hooks.api, network: EVERY_GATE_OPEN });
  observer.register(hooks.api);
  await runHeartbeatTurn(hooks, observer, {
    runId: "run-user-turn",
    workspaceDir: workspace,
    trigger: "user",
  });
  assert.equal(
    hooks.state.subagentRuns,
    1,
    "OC's heartbeat prompt IS classifiable — the trigger gate is what stops it",
  );
});
