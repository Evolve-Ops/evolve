/**
 * How many model calls of its OWN does Evolve make around a user turn?
 *
 * The number the decision is about (D-OH5 —
 * internal/decision-evolve-overhead-2026-09-07.md: "≤ 1 Evolve model
 * call per user turn"). This file measures it rather than asserting it
 * in prose, by driving the real ``PreflightIntentRouter`` over the
 * replay fixture's user turns with a subagent stub that counts every
 * invocation, in both postures:
 *
 *   gate OPEN  — what a default pod did before this change, because the
 *                old haiku gate was default-on AND fail-open;
 *   gate CLOSED — what a default pod does now.
 *
 * The preflight router is the only one of the four Evolve call sites
 * that fires ON the hot path (in front of the user, inside
 * before_model_resolve). The tier classifier and the judge fire at
 * agent_end and the summariser at session end; all three read the same
 * gate, so closing it takes them to zero too — but this test measures
 * the hot-path one, which is the one a person waits for.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/evolveCallBudget.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import { PreflightIntentRouter } from "../dist/observer/PreflightIntentRouter.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(
  fs.readFileSync(path.join(HERE, "fixtures", "routing-replay.json"), "utf8"),
);
const USER_TURNS = FIXTURE.turns.filter((t) => t.source === "user");

/**
 * Prompts that reach the haiku layer: no regex hit either way. The
 * router's free layers short-circuit obvious cases, so counting on
 * "thanks!" would flatter the before-number.
 */
const AMBIGUOUS_PROMPT = "can you take another look at the thing we discussed";

function countingApi() {
  const state = { runs: 0 };
  return {
    state,
    runtime: {
      subagent: {
        run: async () => {
          state.runs += 1;
          return { runId: `r-${state.runs}` };
        },
        waitForRun: async () => ({ lastMessage: "TIER2" }),
      },
    },
  };
}

function pod(network) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "call-budget-"));
  fs.writeFileSync(path.join(dir, "network.json"), JSON.stringify(network));
  return { botId: FIXTURE.botId, sharedDir: dir };
}

async function measure(network) {
  const api = countingApi();
  const router = new PreflightIntentRouter(pod(network), { debug() {} }, api);
  for (const turn of USER_TURNS) {
    await router.classify({
      userMessage: AMBIGUOUS_PROMPT,
      botId: FIXTURE.botId,
      surface: turn.channel,
    });
  }
  return api.state.runs;
}

test("hot path: a default pod makes ZERO Evolve model calls per user turn", async () => {
  // No classifier config at all — the shape of every pod that has not
  // opted in.
  const calls = await measure({});
  assert.equal(
    calls, 0,
    "the routing answer for a user turn must cost no model call of Evolve's own",
  );
});

test("hot path: an unreadable config still makes ZERO calls (fails closed)", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "call-budget-"));
  fs.writeFileSync(path.join(dir, "network.json"), "{ broken");
  const api = countingApi();
  const router = new PreflightIntentRouter(
    { botId: FIXTURE.botId, sharedDir: dir }, { debug() {} }, api,
  );
  await router.classify({ userMessage: AMBIGUOUS_PROMPT, botId: FIXTURE.botId });
  assert.equal(api.state.runs, 0);
});

test("hot path: a bot with a confident prior makes ZERO calls even gate-open", async () => {
  // The learned prior answers before the LLM layer is reached, which is
  // the point of moving it offline: an opted-in pod still pays nothing
  // per turn on the bots the analyzer has characterised.
  const calls = await measure({
    cascade: { classifiers: { enabled: true } },
    bots: {
      [FIXTURE.botId]: {
        preflight: {
          bot_prior: "standard",
          prior_evidence: { turns: 400, confidence: 0.95, surfaces: {} },
        },
      },
    },
  });
  assert.equal(calls, 0);
});

test("call budget: prints the before/after table for the fixture", async () => {
  const before = await measure({ cascade: { classifiers: { enabled: true } } });
  const after = await measure({});
  const n = USER_TURNS.length;

  console.log(
    [
      "",
      `Evolve-internal model calls on the hot path, over ${n} user turns ` +
      `from the replay fixture:`,
      "",
      "| posture                                   | calls | per user turn |",
      "|-------------------------------------------|-------|---------------|",
      `| gate open (the old default: on, fail-open)| ${String(before).padStart(5)} | ` +
      `${(before / n).toFixed(2)}          |`,
      `| gate closed (the new default)             | ${String(after).padStart(5)} | ` +
      `${(after / n).toFixed(2)}          |`,
      "",
    ].join("\n"),
  );

  assert.equal(before, n, "gate open: one preflight call per ambiguous user turn");
  assert.equal(after, 0, "gate closed: none");
});
