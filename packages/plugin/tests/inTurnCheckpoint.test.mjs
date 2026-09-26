/**
 * In-turn cost checkpoint (D-CS12) — the ledger, the veto through the ONE
 * before_tool_call gate, the owner notice, the continue grant, and the
 * 2026-09-20 replay.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/inTurnCheckpoint.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  InTurnCheckpoint,
  IN_TURN_VETO_REPEAT,
  inTurnCheckpoint,
  renderInTurnVeto,
  resolveInTurnCheckpointUsd,
  withInTurnOwnerNotice,
} from "../dist/breakers/InTurnCheckpoint.js";
import { renderInTurnCheckpointMessage } from "../dist/breakers/CostCheckpoint.js";
import { makeBeforeToolCallHandler } from "../dist/integrity/ToolCallGate.js";
import { estimateCost } from "../dist/observer/ModelPricing.js";
import { captureSender, _resetForTests } from "../dist/util/senderRegistry.js";
import { resolveConfig } from "../dist/config.js";

const BOT = "team_bot_a";
const quiet = { info() {}, warn() {}, error() {}, debug() {} };

function tmpShared() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "in-turn-checkpoint-"));
}

function ledgerRows(shared) {
  const fp = path.join(shared, "breakers", BOT, "in-turn-checkpoints.jsonl");
  if (!fs.existsSync(fp)) return [];
  return fs.readFileSync(fp, "utf8").trim().split("\n").map((l) => JSON.parse(l));
}

function fresh(shared = tmpShared(), thresholdUsd = 5) {
  const c = new InTurnCheckpoint();
  c.configure({ sharedDir: shared, botId: BOT, thresholdUsd, registered: true, logger: quiet });
  return c;
}

function gateFor(shared) {
  inTurnCheckpoint.configure({ sharedDir: shared, botId: BOT, thresholdUsd: 5, registered: true, logger: quiet });
  // layer2 observe-only: the Layer-2 half never blocks, so any block is ours.
  return makeBeforeToolCallHandler(
    { botId: BOT, sharedDir: shared, layer2Enforce: false, layer2EnforceWarning: null },
    quiet,
  );
}

test("below threshold → allowed; crossing call vetoed with the exact message; later calls the one-liner", () => {
  const shared = tmpShared();
  const gate = gateFor(shared);
  const run = "run-cross-1";
  inTurnCheckpoint.recordCost(run, "s1", 2.0);
  assert.equal(gate({ toolName: "exec", params: {}, runId: run }, { toolName: "exec", sessionId: "s1" }), undefined);
  inTurnCheckpoint.recordCost(run, "s1", 2.9);
  assert.equal(gate({ toolName: "exec", params: {}, runId: run }, { toolName: "exec" }), undefined);
  inTurnCheckpoint.recordCost(run, "s1", 0.2); // 5.10 recorded
  const first = gate({ toolName: "exec", params: {}, runId: run }, { toolName: "exec" });
  assert.deepEqual(first, {
    block: true,
    blockReason:
      "Evolve checkpoint: this turn has spent $5.10 (recorded) — reply to the user now " +
      "with what you have and say what is unfinished. The owner can continue it.",
  });
  const later = gate({ toolName: "read", params: {}, runId: run }, { toolName: "read" });
  assert.deepEqual(later, { block: true, blockReason: IN_TURN_VETO_REPEAT });

  const trip = ledgerRows(shared).find((r) => r.event === "checkpoint_in_turn");
  assert.equal(trip.run_id, run);
  assert.equal(trip.spend_usd, 5.1);
  assert.equal(trip.llm_calls, 3);
  assert.equal(trip.tool_calls, 3);
  assert.equal(trip.price_source, "catalog");
  assert.equal(trip.spend_basis, "recorded");
});

test("a new run starts clean", () => {
  const c = fresh();
  c.recordCost("run-a", "s1", 6);
  assert.equal(c.evaluate("run-a", "s1").kind, "veto");
  c.recordCost("run-b", "s1", 0.5);
  assert.deepEqual(c.evaluate("run-b", "s1"), { kind: "allow", spendUsd: 0.5 });
});

test("missing runId → allowed + unknown, counted on the status file", () => {
  const shared = tmpShared();
  const gate = gateFor(shared);
  const logs = [];
  inTurnCheckpoint.configure({
    sharedDir: shared, botId: BOT, thresholdUsd: 5, registered: true,
    logger: { info: (m) => logs.push(m), warn() {} },
  });
  inTurnCheckpoint.recordCost(undefined, "s1", 50);
  assert.equal(gate({ toolName: "exec", params: {} }, { toolName: "exec" }), undefined);
  assert.ok(logs.some((m) => m.includes("unknown")));
  const c = fresh(shared);
  assert.deepEqual(c.evaluate(null, "s1"), { kind: "unknown" });
});

test("no recorded spend for a run is not evidence — allowed", () => {
  assert.deepEqual(fresh().evaluate("never-seen", null), { kind: "allow", spendUsd: 0 });
});

test("config below the $1.00 floor is refused at load; the default stands", () => {
  assert.deepEqual(resolveInTurnCheckpointUsd({}), { usd: 5, warning: null });
  assert.deepEqual(resolveInTurnCheckpointUsd({ inTurnCheckpointUsd: 12.5 }), { usd: 12.5, warning: null });
  for (const bad of [0.5, 0, -3, "8", Number.NaN]) {
    const r = resolveInTurnCheckpointUsd({ inTurnCheckpointUsd: bad });
    assert.equal(r.usd, 5, String(bad));
    assert.match(r.warning, /refused/);
  }
  const cfg = resolveConfig({ botId: BOT, inTurnCheckpointUsd: 0.25 }, {});
  assert.equal(cfg.inTurnCheckpointUsd, 5);
  assert.match(cfg.inTurnCheckpointWarning, /≥ \$1\.00/);
  // configure() clamps too — nothing below the floor reaches the ledger.
  const c = fresh(tmpShared(), 0.1);
  c.recordCost("r", null, 0.5);
  assert.equal(c.evaluate("r", null).kind, "allow");
});

test("owner text: produced once per run, rendered by CostCheckpoint, delivered through the conversation", () => {
  const shared = tmpShared();
  const c = fresh(shared);
  c.recordCost("run-n", "s1", 5.25);
  c.recordCost("run-n", "s1", 0);
  c.evaluate("run-n", "s1");
  c.recordCost("run-n", "s1", 0.3); // the reply call after the veto: not in the notice
  const payload = { text: "Here is what I have so far." };
  const out = withInTurnOwnerNotice({ runId: "run-n", payload }, undefined, c);
  const expected = renderInTurnCheckpointMessage({ spendUsd: 5.25, thresholdUsd: 5, incrementUsd: 2.5, llmCalls: 2 });
  assert.equal(out.payload.text, `Here is what I have so far.\n\n${expected}`);
  // Second payload on the same run: untouched.
  assert.equal(withInTurnOwnerNotice({ runId: "run-n", payload }, undefined, c), undefined);
  // An untripped run: untouched.
  assert.equal(withInTurnOwnerNotice({ runId: "other", payload }, undefined, c), undefined);
  const notice = ledgerRows(shared).filter((r) => r.event === "owner_notice");
  assert.equal(notice.length, 1);
  assert.equal(notice[0].delivery, "conversation");
  assert.match(expected, /\$5\.25 \(recorded, catalog prices\)/);
  assert.match(expected, /raised by \$2\.50 to \$7\.50/);
});

test("a tripped run whose reply never carried text is ledgered not_delivered when it is dropped", () => {
  const shared = tmpShared();
  const c = fresh(shared);
  const t0 = new Date("2026-09-20T20:00:00Z");
  c.recordCost("run-silent", "s1", 9, t0);
  c.evaluate("run-silent", "s1", t0);
  c.endRun("run-silent", t0);
  c.recordCost("run-later", "s1", 0.1, new Date(t0.getTime() + 10 * 60_000)); // prune runs
  const rows = ledgerRows(shared).filter((r) => r.event === "owner_notice");
  assert.deepEqual(rows.map((r) => r.delivery), ["not_delivered"]);
});

test("owner 'continue' re-opens with +50 %; a non-owner's does not; both ledgered with who", () => {
  _resetForTests();
  const shared = tmpShared();
  fs.mkdirSync(path.join(shared, "rosters"), { recursive: true });
  fs.writeFileSync(path.join(shared, "rosters", `${BOT}.json`), JSON.stringify({
    identities: { "telegram:111": { role: "primary_user" }, "telegram:222": { role: "participant" } },
  }));
  const c = fresh(shared);
  c.recordCost("run-1", "s1", 5.5);
  c.evaluate("run-1", "s1");

  captureSender("run-2", { senderId: "222", platform: "telegram" });
  c.noteTurnStart("run-2", "s1", "continue");
  c.recordCost("run-2", "s1", 6);
  assert.equal(c.evaluate("run-2", "s1").kind, "veto", "non-owner keeps the base checkpoint");

  captureSender("run-3", { senderId: "111", platform: "telegram" });
  c.noteTurnStart("run-3", "s1", "Continue.");
  c.recordCost("run-3", "s1", 6);
  assert.equal(c.evaluate("run-3", "s1").kind, "allow", "owner's run gets $7.50");
  c.recordCost("run-3", "s1", 1.6);
  assert.equal(c.evaluate("run-3", "s1").kind, "veto");

  const cont = ledgerRows(shared).filter((r) => r.event === "continue");
  assert.deepEqual(cont.map((r) => [r.who, r.role, r.granted, r.threshold_usd]), [
    ["telegram:222", "participant", false, 5],
    ["telegram:111", "primary_user", true, 7.5],
  ]);
  assert.ok(cont.every((r) => typeof r.ts === "string"));
});

test("'continue' on a session with no trip is an ordinary message", () => {
  const shared = tmpShared();
  const c = fresh(shared);
  c.noteTurnStart("run-x", "s9", "continue");
  assert.deepEqual(ledgerRows(shared).filter((r) => r.event === "continue"), []);
});

test("status file: armed, threshold, price source, liveness counters", () => {
  const shared = tmpShared();
  const c = fresh(shared);
  c.recordCost("r1", null, 1);
  c.recordCost("r1", null, 1);
  c.evaluate("r1", null);
  c.recordCost("r2", null, 99, new Date(Date.now() + 120_000)); // past the throttle
  const st = JSON.parse(fs.readFileSync(path.join(shared, "breakers", BOT, "in-turn-checkpoint-status.json"), "utf8"));
  assert.equal(st.armed, true);
  assert.equal(st.threshold_usd, 5);
  assert.equal(st.price_source, "catalog");
  assert.equal(st.spend_runs, 2);
  assert.equal(st.multi_call_runs, 1);
  assert.equal(st.evaluated_runs, 1);
});

test("replay 2026-09-20: the run stops at call 59, seq 728, $5.00 recorded", () => {
  const fx = JSON.parse(fs.readFileSync(new URL("./fixtures/in-turn-replay-2026-09-20.json", import.meta.url)));
  // The pod priced this turn from its mirrored catalog (cost_source: catalog;
  // the post-mortem's §1 rates). The offline table has no claude-sonnet-5, so
  // without a catalog the plugin records $0 — which the status file counts.
  const shared = tmpShared();
  fs.writeFileSync(path.join(shared, "model-pricing.json"), JSON.stringify({ models: [{
    provider: "anthropic", model_id: fx.model,
    input_cost_per_token: 2e-6, output_cost_per_token: 10e-6,
    cache_write_cost_per_token: 2.5e-6, cache_read_cost_per_token: 0.2e-6,
  }] }));
  const c = fresh(shared);
  let stop = null;
  let total = 0;
  fx.calls.forEach(([seq, time, input, output, cacheRead, cacheWrite, tools], i) => {
    const cost = estimateCost(fx.model, input, output, cacheWrite, cacheRead, fx.provider, shared);
    total += cost;
    c.recordCost("d74dfe83", "sess", cost);
    if (!tools || stop) return;
    const d = c.evaluate("d74dfe83", "sess");
    if (d.kind === "veto") stop = { call: i + 1, seq, time, spend: d.spendUsd, reason: d.reason };
  });
  assert.equal(Math.round(total * 100) / 100, 65.98, "catalog replay matches the recorded cost");
  assert.equal(stop.call, 59);
  assert.equal(stop.seq, 728);
  assert.equal(stop.time, "20:09:10");
  assert.ok(stop.spend >= 5 && stop.spend < 5.1, String(stop.spend));
  assert.equal(stop.reason, renderInTurnVeto(stop.spend));
  console.log(`replay 2026-09-20: stopped at call ${stop.call} (seq ${stop.seq}, ${stop.time}Z) ` +
    `at $${stop.spend.toFixed(4)} recorded of $${total.toFixed(2)}`);
});

test("a call priced $0 (no catalog row) is counted — the checkpoint cannot see it", () => {
  const shared = tmpShared();
  const c = fresh(shared);
  c.recordCost("r", null, 0);
  c.recordCost("r2", null, 0, new Date(Date.now() + 120_000));
  const st = JSON.parse(fs.readFileSync(path.join(shared, "breakers", BOT, "in-turn-checkpoint-status.json"), "utf8"));
  assert.equal(st.zero_cost_calls, 2);
});
