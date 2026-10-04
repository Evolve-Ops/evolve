/**
 * No-progress gate — the per-run repetition ledger, its two predicates and
 * the D-CS11 ceiling, driven through the ONE before_tool_call handler, plus
 * the 2026-09-20 poll-loop replay.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/noProgressGate.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  NoProgressGate,
  NO_PROGRESS_VETO_REPEAT,
  noProgressGate,
  renderCeilingVeto,
  renderFailingVeto,
  renderIdenticalCallVeto,
  resolveMaxModelCallsPerRun,
} from "../dist/breakers/NoProgressGate.js";
import { makeBeforeToolCallHandler } from "../dist/integrity/ToolCallGate.js";
import { makeAppIntegrityHandler } from "../dist/integrity/AppIntegrityMiddleware.js";
import { resolveConfig } from "../dist/config.js";

const BOT = "team_bot_a";
const quiet = { info() {}, warn() {}, error() {}, debug() {} };
let seq = 0;
const newRun = () => `run-${process.pid}-${++seq}`;

function setup(maxModelCallsPerRun = null) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "no-progress-"));
  noProgressGate.configure({ sharedDir: shared, botId: BOT, registered: true, maxModelCallsPerRun, logger: quiet });
  const gate = makeBeforeToolCallHandler(
    { botId: BOT, sharedDir: shared, layer2Enforce: false, layer2EnforceWarning: null },
    quiet,
  );
  const results = makeAppIntegrityHandler({ lookup: () => null }, quiet);
  const call = (runId, toolName, params) => gate({ toolName, params, runId }, { toolName, runId });
  const result = (runId, toolName, args, text, isError = false) =>
    results({ toolCallId: "t", toolName, args, isError, result: { content: [{ type: "text", text }] } }, { runtime: "openclaw", runId });
  return { shared, call, result };
}

function rows(shared) {
  const fp = path.join(shared, "breakers", BOT, "no-progress.jsonl");
  return fs.existsSync(fp) ? fs.readFileSync(fp, "utf8").trim().split("\n").map((l) => JSON.parse(l)) : [];
}

test("3 identical view_image calls → the third is vetoed with the exact message; the event is ledgered", () => {
  const { shared, call, result } = setup();
  const run = newRun();
  const img = { path: "/tmp/board.jpg", prompt: "read the board" };
  assert.equal(call(run, "view_image", img), undefined);
  result(run, "view_image", img, "Loaded 1 image");
  assert.equal(call(run, "view_image", { prompt: "read the board", path: "/tmp/board.jpg" }), undefined); // key order is not identity
  result(run, "view_image", img, "Loaded 1 image");
  assert.deepEqual(call(run, "view_image", img), {
    block: true,
    blockReason:
      "Evolve stopped this call: `view_image` with these arguments has already run 2 times this turn " +
      "and returned the same thing. Reply to the user now with what you have, and say what you could not do.",
  });
  const [row] = rows(shared);
  assert.equal(row.event, "no_progress");
  assert.equal(row.predicate, "identical_call");
  assert.equal(row.tool, "view_image");
  assert.equal(row.count, 2);
  assert.equal(row.run_id, run);
});

test("identical arguments are enough — a changing result (subagents runtime) does not exempt a read tool", () => {
  const { call, result } = setup();
  const run = newRun();
  const a = { action: "list", recentMinutes: 30 };
  call(run, "subagents", a); result(run, "subagents", a, '{"runtime":"3s"}');
  call(run, "subagents", a); result(run, "subagents", a, '{"runtime":"6s"}');
  assert.equal(call(run, "subagents", a).blockReason, renderIdenticalCallVeto("subagents", 2, false));
});

test("2 identical then a different call → allowed", () => {
  const { call } = setup();
  const run = newRun();
  assert.equal(call(run, "view_image", { path: "a" }), undefined);
  assert.equal(call(run, "view_image", { path: "a" }), undefined);
  assert.equal(call(run, "view_image", { path: "b" }), undefined);
  assert.equal(call(run, "exec", { command: "ls" }), undefined);
});

test("a failing exec ×3 (exit status observable) → the next call is vetoed", () => {
  const { shared, call, result } = setup();
  const run = newRun();
  const err = "board_words conflict at F10: G vs K";
  for (const flag of ["", " --strict", " --verbose"]) {
    const a = { command: `python3 audit.py request.json${flag}` };
    assert.equal(call(run, "exec", a), undefined);
    result(run, "exec", a, err, true);
  }
  assert.equal(call(run, "edit", { path: "request.json" }).blockReason, renderFailingVeto("exec", 3));
  assert.equal(rows(shared)[0].predicate, "failing_validation");
});

test("three different calls failing with empty text do not fire (b)", () => {
  const { call, result } = setup();
  const run = newRun();
  for (const cmd of ["grep -q needle a.txt", "test -f missing.json", "grep -q needle b.txt"]) {
    const a = { command: cmd };
    assert.equal(call(run, "exec", a), undefined);
    result(run, "exec", a, "", true); // failed, but silently — nothing repeated
  }
  assert.equal(call(run, "exec", { command: "ls" }), undefined);
});

test("different failures, or failures without a host signal, do not fire (b)", () => {
  const { call, result } = setup();
  const run = newRun();
  for (let i = 0; i < 4; i++) {
    const a = { command: `check ${i}` };
    call(run, "exec", a);
    result(run, "exec", a, `error ${i}`, true); // failed, but never the same way
  }
  for (let i = 4; i < 8; i++) {
    const a = { command: `check ${i}` };
    call(run, "exec", a);
    result(run, "exec", a, "error: same text", false); // no host signal → unknown, not failed
  }
  assert.equal(call(run, "exec", { command: "next" }), undefined);
});

test("missing runId → allowed and unknown logged", () => {
  const { call } = setup();
  for (let i = 0; i < 5; i++) assert.equal(call(undefined, "view_image", { path: "a" }), undefined);
  const logs = [];
  const g = new NoProgressGate();
  g.configure({ sharedDir: "", botId: BOT, registered: true, maxModelCallsPerRun: null, logger: { info: (m) => logs.push(m), warn() {} } });
  assert.deepEqual(g.evaluate(null, null, "view_image", {}), { kind: "unknown" });
  assert.match(logs[0], /without a runId — unknown, allowed/);
});

test("after a veto every later call in the run is vetoed with the one-liner; a new runId starts clean", () => {
  const { shared, call } = setup();
  const run = newRun();
  for (let i = 0; i < 2; i++) call(run, "process", { action: "poll" });
  assert.equal(call(run, "process", { action: "poll" }).block, true);
  assert.deepEqual(call(run, "message", { text: "different" }), { block: true, blockReason: NO_PROGRESS_VETO_REPEAT });
  assert.deepEqual(call(run, "read", { path: "x" }), { block: true, blockReason: NO_PROGRESS_VETO_REPEAT });
  assert.equal(rows(shared).length, 1, "one row per stopped run");
  const next = newRun();
  assert.equal(call(next, "process", { action: "poll" }), undefined);
  assert.equal(call(next, "process", { action: "poll" }), undefined);
  noProgressGate.endRun(next); // agent_end discards the ledger
  assert.equal(call(next, "process", { action: "poll" }), undefined);
});

test("ceiling unset → never fires", () => {
  const { call } = setup(null);
  const run = newRun();
  for (let i = 0; i < 60; i++) {
    noProgressGate.noteModelCall(run);
    assert.equal(call(run, "exec", { command: `step ${i}` }), undefined);
  }
});

test("ceiling set to 5 → the sixth call is vetoed; no model calls observed → not evaluated", () => {
  const { shared, call } = setup(5);
  const run = newRun();
  for (let i = 1; i <= 5; i++) {
    noProgressGate.noteModelCall(run);
    assert.equal(call(run, "exec", { command: `step ${i}` }), undefined, `call ${i}`);
  }
  noProgressGate.noteModelCall(run);
  assert.equal(call(run, "exec", { command: "step 6" }).blockReason, renderCeilingVeto(5));
  assert.equal(rows(shared)[0].predicate, "call_ceiling");
  const quietRun = newRun(); // tool calls with no llm_output evidence
  for (let i = 0; i < 8; i++) assert.equal(call(quietRun, "exec", { command: `q ${i}` }), undefined);
});

test("maxModelCallsPerRun: unset or 0 = off; bad values refused with a warning", () => {
  assert.deepEqual(resolveMaxModelCallsPerRun({}), { max: null, warning: null });
  assert.deepEqual(resolveMaxModelCallsPerRun({ maxModelCallsPerRun: 0 }), { max: null, warning: null });
  assert.deepEqual(resolveMaxModelCallsPerRun({ maxModelCallsPerRun: 40 }), { max: 40, warning: null });
  for (const bad of [2.5, "40", -1]) {
    const r = resolveMaxModelCallsPerRun({ maxModelCallsPerRun: bad });
    assert.equal(r.max, null);
    assert.match(r.warning, /refused/);
  }
  assert.equal(resolveConfig({ botId: BOT }, {}).maxModelCallsPerRun, null);
});

test("status file: armed, predicates, ceiling and liveness counters for the health control", () => {
  const { shared } = setup(40);
  const st = JSON.parse(fs.readFileSync(path.join(shared, "breakers", BOT, "no-progress-status.json"), "utf8"));
  assert.equal(st.armed, true);
  assert.equal(st.identical_calls, 3);
  assert.equal(st.failures, 3);
  assert.equal(st.max_model_calls_per_run, 40);
});

test("replay 2026-09-20: predicate (a) stops the poll loop at call 14, seq 638", () => {
  const fx = JSON.parse(fs.readFileSync(new URL("./fixtures/no-progress-replay-2026-09-20.json", import.meta.url)));
  assert.equal(fx.calls.length, 301);
  const { shared, call, result } = setup();
  const run = newRun();
  let stop = null;
  let vetoed = 0;
  fx.calls.forEach(([s, tool, argsId, failed, resultId], i) => {
    const args = { id: argsId };
    const r = call(run, tool, args);
    if (r?.block) {
      vetoed += 1;
      if (!stop) stop = { call: i + 1, seq: s, tool, reason: r.blockReason };
      return;
    }
    result(run, tool, args, `result ${resultId}`, failed);
  });
  assert.deepEqual(
    { call: stop.call, seq: stop.seq, tool: stop.tool },
    { call: 14, seq: 638, tool: "subagents" },
  );
  assert.equal(stop.reason, renderIdenticalCallVeto("subagents", 2, false));
  assert.equal(vetoed, 301 - 13, "every later call in the run is vetoed");
  assert.equal(rows(shared).length, 1);
});
