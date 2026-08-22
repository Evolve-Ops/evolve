/**
 * Tests for runPinnedSubagent — the OC >=2026.7 subagent model-override
 * authorization adapter (2026-07-31 fleet incident).
 *
 * Contract under test:
 *   - pinned run attempted first; result passed through on success
 *   - on OC's authorization rejection: loud log ONCE per process,
 *     unpinned retry, and subsequent calls skip the pinned attempt
 *   - non-authorization errors propagate unchanged (call sites keep
 *     their own degradation paths)
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/subagentRun.test.mjs
 */
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

import {
  runPinnedSubagent,
  isSubagentOverrideAuthError,
  subagentPinDenied,
  classifyEvolveSubagentKey,
  _resetSubagentPinDenialForTest,
} from "../dist/observer/subagentRun.js";

const AUTH_ERR = new Error(
  "provider/model override is not authorized for this plugin subagent run.",
);

function makeLogger() {
  const calls = { error: [], warn: [], info: [] };
  return {
    calls,
    info: (m) => calls.info.push(m),
    warn: (m) => calls.warn.push(m),
    error: (m) => calls.error.push(m),
  };
}

/** Fake api that rejects pinned runs with the 2026.7 auth error. */
function makeAuthRejectingApi() {
  const runs = [];
  return {
    runs,
    runtime: {
      subagent: {
        run: async (params) => {
          runs.push(params);
          if (params.model) throw AUTH_ERR;
          return { runId: `run-${runs.length}` };
        },
      },
    },
  };
}

beforeEach(() => _resetSubagentPinDenialForTest());

test("pinned run passes through on success", async () => {
  const runs = [];
  const api = {
    runtime: { subagent: { run: async (p) => { runs.push(p); return { runId: "ok-1" }; } } },
  };
  const res = await runPinnedSubagent(api, makeLogger(), {
    idempotencyKey: "k1", message: "m", model: "anthropic/claude-haiku-4-5", maxTurns: 1,
  });
  assert.equal(res.runId, "ok-1");
  assert.equal(runs.length, 1);
  assert.equal(runs[0].model, "anthropic/claude-haiku-4-5");
  assert.equal(subagentPinDenied(), false);
});

test("auth rejection → loud log once, unpinned retry, subsequent calls skip the pin", async () => {
  const api = makeAuthRejectingApi();
  const logger = makeLogger();

  const first = await runPinnedSubagent(api, logger, {
    idempotencyKey: "k1", message: "m1", model: "anthropic/claude-haiku-4-5", maxTurns: 1,
  });
  assert.ok(first.runId);
  // Attempt 1 pinned (rejected), attempt 2 unpinned.
  assert.equal(api.runs.length, 2);
  assert.equal(api.runs[0].model, "anthropic/claude-haiku-4-5");
  assert.equal(api.runs[1].model, undefined);
  assert.equal(subagentPinDenied(), true);
  assert.equal(logger.calls.error.length, 1);
  assert.match(logger.calls.error[0], /rejected the plugin's subagent model pin/);

  // Second call: no doomed pinned attempt, no second loud log.
  const second = await runPinnedSubagent(api, logger, {
    idempotencyKey: "k2", message: "m2", model: "anthropic/claude-haiku-4-5", maxTurns: 1,
  });
  assert.ok(second.runId);
  assert.equal(api.runs.length, 3);
  assert.equal(api.runs[2].model, undefined);
  assert.equal(logger.calls.error.length, 1);
});

test("loud log falls back to warn when the logger has no error()", async () => {
  const api = makeAuthRejectingApi();
  const calls = { warn: [], info: [] };
  const logger = { info: (m) => calls.info.push(m), warn: (m) => calls.warn.push(m) };
  await runPinnedSubagent(api, logger, {
    // A mapped key — an unmapped one would add its own attribution warn
    // and hide what this test is pinning (the denial-log fallback).
    idempotencyKey: "evolve:tier-classifier:1", message: "m", model: "anthropic/claude-haiku-4-5",
  });
  assert.equal(calls.warn.length, 1);
  assert.match(calls.warn[0], /rejected the plugin's subagent model pin/);
});

test("non-auth errors propagate unchanged", async () => {
  const boom = new Error("socket hang up");
  const api = { runtime: { subagent: { run: async () => { throw boom; } } } };
  await assert.rejects(
    () => runPinnedSubagent(api, makeLogger(), {
      idempotencyKey: "k1", message: "m", model: "anthropic/claude-haiku-4-5",
    }),
    /socket hang up/,
  );
  assert.equal(subagentPinDenied(), false);
});

test("no model param → straight unpinned run, no denial state", async () => {
  const api = makeAuthRejectingApi();
  const res = await runPinnedSubagent(api, makeLogger(), {
    idempotencyKey: "k1", message: "m",
  });
  assert.ok(res.runId);
  assert.equal(api.runs.length, 1);
  assert.equal(api.runs[0].model, undefined);
  assert.equal(subagentPinDenied(), false);
});

// ── Cost-attribution tagging (spec-evolve-overhead-budget Phase A2) ─────────

test("classifyEvolveSubagentKey maps every live call site's idempotencyKey", () => {
  // These literal shapes mirror the four runPinnedSubagent call sites.
  assert.equal(classifyEvolveSubagentKey("evolve:session-summary:1785326120738"), "summarizer");
  assert.equal(classifyEvolveSubagentKey("evolve:tier-classifier:1785313193500"), "classifier");
  assert.equal(classifyEvolveSubagentKey("evolve:session-judge:team-bot-a:1785313193500"), "classifier");
  assert.equal(classifyEvolveSubagentKey("evolve:preflight:team-bot-a:1785304503016"), "classifier");
});

test("classifyEvolveSubagentKey maps the OC-derived session key form", () => {
  // OC 2026.7 derives the subagent session key as
  // "agent:<agent>:explicit:<idempotencyKey>" — the llm_output ctx carries
  // this form, and it must classify identically to the raw key.
  assert.equal(
    classifyEvolveSubagentKey("agent:main:explicit:evolve:session-summary:1785326120738"),
    "summarizer",
  );
  assert.equal(
    classifyEvolveSubagentKey("agent:main:explicit:evolve:preflight:team-bot-a:1785304503016"),
    "classifier",
  );
});

test("classifyEvolveSubagentKey returns null for non-Evolve keys", () => {
  assert.equal(classifyEvolveSubagentKey("agent:main:telegram:direct:12345"), null);
  assert.equal(classifyEvolveSubagentKey("agent:main:explicit:b242944a-c9d4"), null);
  assert.equal(classifyEvolveSubagentKey("evolve:brand-new-site:123"), null);
  assert.equal(classifyEvolveSubagentKey(""), null);
  assert.equal(classifyEvolveSubagentKey(undefined), null);
  assert.equal(classifyEvolveSubagentKey(42), null);
});

test("unpinned retry keeps the idempotencyKey tag (attribution survives pin denial)", async () => {
  // Post-#3531 the pin is denied and the run proceeds UNPINNED on the
  // bot's default model — model-based attribution is impossible, so the
  // idempotencyKey tag must ride through to the retry unchanged.
  const api = makeAuthRejectingApi();
  await runPinnedSubagent(api, makeLogger(), {
    idempotencyKey: "evolve:session-summary:123",
    message: "m",
    model: "anthropic/claude-haiku-4-5",
    maxTurns: 1,
  });
  assert.equal(api.runs.length, 2);
  assert.equal(api.runs[1].model, undefined);
  assert.equal(api.runs[1].idempotencyKey, "evolve:session-summary:123");
  assert.equal(classifyEvolveSubagentKey(api.runs[1].idempotencyKey), "summarizer");
});

test("runPinnedSubagent warns on an unmapped idempotencyKey tag", async () => {
  const api = { runtime: { subagent: { run: async () => ({ runId: "r" }) } } };
  const logger = makeLogger();
  await runPinnedSubagent(api, logger, {
    idempotencyKey: "evolve:new-helper:123", message: "m",
  });
  assert.equal(logger.calls.warn.length, 1);
  assert.match(logger.calls.warn[0], /no trigger-kind mapping/);

  // Mapped keys stay quiet.
  const quiet = makeLogger();
  await runPinnedSubagent(api, quiet, {
    idempotencyKey: "evolve:tier-classifier:123", message: "m",
  });
  assert.equal(quiet.calls.warn.length, 0);
});

test("isSubagentOverrideAuthError matches the 2026.7 contract's reason strings", () => {
  for (const msg of [
    "provider/model override is not authorized for this plugin subagent run.",
    'plugin "evolve" is not trusted for fallback provider/model override requests. See https://…',
    'model override "anthropic/claude-haiku-4-5" is not allowlisted for plugin "evolve".',
    'plugin "evolve" configured subagent.allowedModels, but none of the entries normalized to a valid provider/model target.',
    "fallback provider/model overrides that use an allowlist must resolve to a canonical provider/model target.",
  ]) {
    assert.equal(isSubagentOverrideAuthError(new Error(msg)), true, msg);
  }
  assert.equal(isSubagentOverrideAuthError(new Error("socket hang up")), false);
  assert.equal(isSubagentOverrideAuthError(new Error("Gateway agent method returned an invalid runId.")), false);
});
