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

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  runPinnedSubagent,
  isSubagentOverrideAuthError,
  subagentPinDenied,
  classifyEvolveSubagentKey,
  SubagentBreakerRefusal,
  configureSubagentBreakerGate,
  isInteractiveTrigger,
  _resetSubagentPinDenialForTest,
  _resetSubagentBreakerGateForTest,
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

beforeEach(() => {
  _resetSubagentPinDenialForTest();
  _resetSubagentBreakerGateForTest();
});

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


// ── Cost-breaker gate: a paused bot's Evolve machinery pauses too ────────────
//
// On 2026-09-07 the preflight router's run count went 10,407 -> 13,282 and
// the pod's 7-day spend $36.95 -> $42.61 in an hour on a bot the operator
// had "paused": the breaker gated the bot's replies, not Evolve's own model
// calls. All four cheap-LLM helpers funnel through runPinnedSubagent, so
// this is the one gate that covers them.

const GATE_BOT = "team_bot_a";

function makeGateDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "evolve-subagent-gate-"));
}

function tripGateBreaker(shared, { expiresInMs = 24 * 3600 * 1000 } = {}) {
  const bdir = path.join(shared, "breakers", GATE_BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), JSON.stringify({
    bot_id: GATE_BOT, type: "cost", state: "tripped",
    tripped_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + expiresInMs).toISOString(),
    initiated_by: "auto:spend_alert",
    reason: "per-bot daily cap exceeded: $27.49 >= $5.00",
    trip_id: "deadbeef",
  }));
}

/** The operator's Reactivate. */
function resetGateBreaker(shared) {
  fs.rmSync(path.join(shared, "breakers", GATE_BOT, "cost.json"), { force: true });
}

function makeCountingApi() {
  const runs = [];
  return {
    runs,
    runtime: {
      subagent: {
        run: async (params) => { runs.push(params); return { runId: "r1" }; },
      },
    },
  };
}

test("an unconfigured gate is inert — the run proceeds", async () => {
  const api = makeCountingApi();
  await runPinnedSubagent(api, makeLogger(), {
    idempotencyKey: "evolve:preflight:x:1", message: "m",
  });
  assert.equal(api.runs.length, 1);
});

test("a tripped breaker refuses the run before any api call", async () => {
  const shared = makeGateDir();
  tripGateBreaker(shared);
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  await assert.rejects(
    () => runPinnedSubagent(api, makeLogger(), {
      idempotencyKey: "evolve:preflight:x:1", message: "m", model: "haiku",
    }),
    (err) => err instanceof SubagentBreakerRefusal,
  );
  assert.equal(api.runs.length, 0, "a refused run must not reach the subagent runtime");
});

test("every call site is covered — one gate, four key tags", async () => {
  const shared = makeGateDir();
  tripGateBreaker(shared);
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  for (const tag of ["preflight", "tier-classifier", "session-judge", "session-summary"]) {
    await assert.rejects(
      () => runPinnedSubagent(api, makeLogger(), {
        idempotencyKey: `evolve:${tag}:x:1`, message: "m",
      }),
      (err) => err instanceof SubagentBreakerRefusal,
    );
  }
  assert.equal(api.runs.length, 0);
});

test("reactivation lifts the refusal with the bot", async () => {
  const shared = makeGateDir();
  tripGateBreaker(shared);
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  await assert.rejects(
    () => runPinnedSubagent(api, makeLogger(), {
      idempotencyKey: "evolve:preflight:x:1", message: "m",
    }),
    (err) => err instanceof SubagentBreakerRefusal,
  );
  // No second piece of state to clear: the gate reads the same breaker
  // file the turn veto reads, so clearing the breaker is the whole fix.
  resetGateBreaker(shared);
  await runPinnedSubagent(api, makeLogger(), {
    idempotencyKey: "evolve:preflight:x:2", message: "m",
  });
  assert.equal(api.runs.length, 1);
});

test("an EXPIRED trip does not refuse", async () => {
  const shared = makeGateDir();
  tripGateBreaker(shared, { expiresInMs: -1000 });
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  await runPinnedSubagent(api, makeLogger(), {
    idempotencyKey: "evolve:preflight:x:1", message: "m",
  });
  assert.equal(api.runs.length, 1);
});

test("a pod-wide trip refuses too", async () => {
  const shared = makeGateDir();
  const pdir = path.join(shared, "breakers", "pod");
  fs.mkdirSync(pdir, { recursive: true });
  fs.writeFileSync(path.join(pdir, "cost.json"), JSON.stringify({
    bot_id: "pod", type: "cost", state: "tripped",
    tripped_at: new Date().toISOString(), expires_at: null,
    initiated_by: "web", reason: "pod-wide", trip_id: "podtrip",
  }));
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  await assert.rejects(
    () => runPinnedSubagent(api, makeLogger(), {
      idempotencyKey: "evolve:preflight:x:1", message: "m",
    }),
    (err) => err instanceof SubagentBreakerRefusal,
  );
});

test("the refusal is logged once a minute, not once per call", async () => {
  const shared = makeGateDir();
  tripGateBreaker(shared);
  const logger = makeLogger();
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, logger);
  const api = makeCountingApi();
  for (let i = 0; i < 30; i += 1) {
    await runPinnedSubagent(api, makeLogger(), {
      idempotencyKey: `evolve:preflight:x:${i}`, message: "m",
    }).catch(() => {});
  }
  const lines = logger.calls.info.filter((m) => m.includes("subagent runs refused"));
  assert.equal(lines.length, 1, `expected 1 log line, got ${lines.length}`);
});

// ── Unknown breaker state fails CLOSED ──────────────────────────────────────
//
// "Every Evolve switch fails closed; an unreadable config disables the
// feature" (D-OH5). A truncated write during a trip is exactly when
// cost.json is unreadable, and that is the moment the breaker must hold —
// otherwise the four helpers resume on a bot the operator paused, which is
// the 10,407 -> 13,282 bleed this gate exists to stop. An ABSENT file is
// different: it legitimately means "not tripped", and keeps proceeding.

test("a corrupt breaker file fails CLOSED — the classifiers stop", async () => {
  const shared = makeGateDir();
  const bdir = path.join(shared, "breakers", GATE_BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), "{ not json");
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  await assert.rejects(
    () => runPinnedSubagent(api, makeLogger(), {
      idempotencyKey: "evolve:session-summary:x:1", message: "m",
    }),
    (err) => err instanceof SubagentBreakerRefusal
      && /unreadable/i.test(err.message),
  );
  assert.equal(api.runs.length, 0, "unknown breaker state must not spend");
});

test("a truncated breaker file fails CLOSED too", async () => {
  // The realistic shape: valid JSON, half a record. Same verdict.
  const shared = makeGateDir();
  const bdir = path.join(shared, "breakers", GATE_BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), JSON.stringify({ bot_id: GATE_BOT }));
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  await assert.rejects(
    () => runPinnedSubagent(api, makeLogger(), {
      idempotencyKey: "evolve:session-judge:x:1", message: "m",
    }),
    (err) => err instanceof SubagentBreakerRefusal,
  );
  assert.equal(api.runs.length, 0);
});

test("an ABSENT breaker file still proceeds — absent is not unknown", async () => {
  // The other half of the distinction. No file at all is the normal state
  // of a healthy bot; it must not be read as "paused".
  const shared = makeGateDir();
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  await runPinnedSubagent(api, makeLogger(), {
    idempotencyKey: "evolve:session-summary:x:1", message: "m",
  });
  assert.equal(api.runs.length, 1);
});

test("an unreadable POD breaker file fails closed as well", async () => {
  const shared = makeGateDir();
  const pdir = path.join(shared, "breakers", "pod");
  fs.mkdirSync(pdir, { recursive: true });
  fs.writeFileSync(path.join(pdir, "cost.json"), "}{");
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  await assert.rejects(
    () => runPinnedSubagent(api, makeLogger(), {
      idempotencyKey: "evolve:session-summary:x:1", message: "m",
    }),
    (err) => err instanceof SubagentBreakerRefusal,
  );
  assert.equal(api.runs.length, 0);
});

// ── The interactive carve-out (a breaker stops work, it does not re-route) ──
//
// The tier classifier and the preflight router DECIDE which model answers a
// turn. Refusing them on an interactive turn does not stop the turn — it
// moves it to each call site's heuristic fallback, i.e. to a different rung
// than the one it would otherwise have had. "Nothing changes which model
// answers a user turn" is a standing rule, so those two tags are excused on
// an interactive trigger only. Everything else stays gated.

test("the two routing helpers are NOT refused on an interactive turn", async () => {
  const shared = makeGateDir();
  tripGateBreaker(shared);
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  for (const tag of ["tier-classifier", "preflight"]) {
    await runPinnedSubagent(api, makeLogger(), {
      idempotencyKey: `evolve:${tag}:x:1`, message: "m", interactiveTurn: true,
    });
  }
  assert.equal(api.runs.length, 2);
});

test("the carve-out is opt-IN — silence from a call site is still gated", async () => {
  // A call site that says nothing about its turn gets the gate. Silence
  // must not be a way to switch a spend gate off, which is why the carve-out
  // reads an explicit flag rather than inferring one from an absent field.
  const shared = makeGateDir();
  tripGateBreaker(shared);
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  for (const tag of ["tier-classifier", "preflight"]) {
    await assert.rejects(
      () => runPinnedSubagent(api, makeLogger(), {
        idempotencyKey: `evolve:${tag}:x:1`, message: "m",
      }),
      (err) => err instanceof SubagentBreakerRefusal,
      `${tag} with no interactiveTurn flag must stay refused`,
    );
  }
  assert.equal(api.runs.length, 0);
});

test("the carve-out does NOT extend to background triggers", async () => {
  // The two routing call sites fill the flag from isInteractiveTrigger, so
  // a heartbeat/cron turn arrives here as `false` and stays refused.
  const shared = makeGateDir();
  tripGateBreaker(shared);
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  for (const trigger of ["heartbeat", "cron", "scheduled", "subagent"]) {
    for (const tag of ["tier-classifier", "preflight"]) {
      await assert.rejects(
        () => runPinnedSubagent(api, makeLogger(), {
          idempotencyKey: `evolve:${tag}:x:1`,
          message: "m",
          interactiveTurn: isInteractiveTrigger(trigger),
        }),
        (err) => err instanceof SubagentBreakerRefusal,
        `${tag} on ${trigger} should stay refused`,
      );
    }
  }
  assert.equal(api.runs.length, 0);
});

test("the carve-out does NOT extend to the judge or the summariser", async () => {
  // Neither decides the answering model, so a paused bot's machinery stops
  // for them on every trigger — including a person's turn.
  const shared = makeGateDir();
  tripGateBreaker(shared);
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  for (const tag of ["session-judge", "session-summary"]) {
    await assert.rejects(
      () => runPinnedSubagent(api, makeLogger(), {
        idempotencyKey: `evolve:${tag}:x:1`, message: "m", interactiveTurn: true,
      }),
      (err) => err instanceof SubagentBreakerRefusal,
      `${tag} must stay gated on a user turn`,
    );
  }
  assert.equal(api.runs.length, 0);
});

test("the carve-out covers unknown breaker state too, on the same two tags", async () => {
  // The re-routing hazard does not care WHY the gate would refuse.
  const shared = makeGateDir();
  const bdir = path.join(shared, "breakers", GATE_BOT);
  fs.mkdirSync(bdir, { recursive: true });
  fs.writeFileSync(path.join(bdir, "cost.json"), "{ not json");
  configureSubagentBreakerGate({ sharedDir: shared, botId: GATE_BOT }, makeLogger());
  const api = makeCountingApi();
  await runPinnedSubagent(api, makeLogger(), {
    idempotencyKey: "evolve:tier-classifier:x:1", message: "m", interactiveTurn: true,
  });
  assert.equal(api.runs.length, 1);
});

test("`interactiveTurn` is Evolve's own input — it never reaches OC's runtime", async () => {
  const api = makeCountingApi();
  await runPinnedSubagent(api, makeLogger(), {
    idempotencyKey: "evolve:preflight:x:1", message: "m", interactiveTurn: true,
  });
  assert.equal(api.runs.length, 1);
  assert.equal("interactiveTurn" in api.runs[0], false);
});

test("isInteractiveTrigger names the background set exactly", () => {
  for (const t of ["heartbeat", "cron", "CRON", " Heartbeat ", "cron_app",
                   "scheduled", "subagent"]) {
    assert.equal(isInteractiveTrigger(t), false, t);
  }
  // An absent trigger reads as interactive: OC does not always populate
  // ctx.trigger on before_model_resolve for a user turn, and TurnObserver's
  // own convention on that hook is `=== "user" || == null`.
  for (const t of ["user", "", null, undefined, "webhook"]) {
    assert.equal(isInteractiveTrigger(t), true, String(t));
  }
});
