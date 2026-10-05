/**
 * Tests for SessionCostMonitor — in-flight per-session cost sentinel.
 *
 * PR C (2026-05-31) — runaway-session breaker that catches single-session
 * spend bursts mid-conversation, complementing the per-bot daily cap.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/sessionCostMonitor.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  SessionCostMonitor,
  readSessionBudgetBreaker,
  readSessionBudgetCap,
} from "../dist/observer/SessionCostMonitor.js";


function tmpShared() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "scm-test-"));
}


function tmpBotHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "scm-bot-"));
}


function writeOpenclawCap(botHome, capUsd) {
  const dir = path.join(botHome, ".openclaw");
  fs.mkdirSync(dir, { recursive: true });
  const cfg = {
    agents: { defaults: { sessionBudgetCapUsd: capUsd } },
  };
  fs.writeFileSync(path.join(dir, "openclaw.json"), JSON.stringify(cfg));
}


// ── readSessionBudgetCap ────────────────────────────────────────────────────


test("readSessionBudgetCap: missing openclaw.json → null", () => {
  const home = tmpBotHome();
  try {
    assert.equal(readSessionBudgetCap({ botHome: home }), null);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});


test("readSessionBudgetCap: missing key → null", () => {
  const home = tmpBotHome();
  try {
    fs.mkdirSync(path.join(home, ".openclaw"), { recursive: true });
    fs.writeFileSync(
      path.join(home, ".openclaw", "openclaw.json"),
      JSON.stringify({ agents: { defaults: {} } }),
    );
    assert.equal(readSessionBudgetCap({ botHome: home }), null);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});


test("readSessionBudgetCap: positive number → value", () => {
  const home = tmpBotHome();
  try {
    writeOpenclawCap(home, 2.50);
    assert.equal(readSessionBudgetCap({ botHome: home }), 2.50);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});


test("readSessionBudgetCap: zero / negative → null (treated as no cap)", () => {
  const home = tmpBotHome();
  try {
    writeOpenclawCap(home, 0);
    assert.equal(readSessionBudgetCap({ botHome: home }), null);
    writeOpenclawCap(home, -1);
    assert.equal(readSessionBudgetCap({ botHome: home }), null);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});


test("readSessionBudgetCap: malformed JSON → null (fail-open)", () => {
  const home = tmpBotHome();
  try {
    fs.mkdirSync(path.join(home, ".openclaw"), { recursive: true });
    fs.writeFileSync(
      path.join(home, ".openclaw", "openclaw.json"),
      "{ not json",
    );
    assert.equal(readSessionBudgetCap({ botHome: home }), null);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});


// ── SessionCostMonitor.recordCost ──────────────────────────────────────────


test("recordCost: cap null → never trips, no breaker file", () => {
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => null,
    });
    const r = m.recordCost({ sessionId: "s1", callCostUsd: 100 });
    assert.equal(r.trippedThisCall, false);
    assert.equal(r.alreadyTripped, false);
    assert.equal(r.capUsd, null);
    const breakerDir = path.join(shared, "breakers", "team-bot-a");
    assert.equal(fs.existsSync(breakerDir), false,
      "no breaker file should be created when cap is null");
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("recordCost: accumulates per session; trip on crossing cap", () => {
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => 1.00,
    });
    let r = m.recordCost({ sessionId: "s1", callCostUsd: 0.40 });
    assert.equal(r.trippedThisCall, false);
    assert.equal(r.accumulatedUsd, 0.40);
    r = m.recordCost({ sessionId: "s1", callCostUsd: 0.40 });
    assert.equal(r.trippedThisCall, false);
    r = m.recordCost({
      sessionId: "s1", callCostUsd: 0.30, model: "anthropic/claude-sonnet-4-6",
    });
    assert.equal(r.trippedThisCall, true);
    assert.equal(r.capUsd, 1.00);
    // Sum 0.40 + 0.40 + 0.30 = 1.10; round to 6dp.
    assert.equal(r.accumulatedUsd, 1.10);
    // Breaker file landed with the right fields.
    const breakerPath = path.join(
      shared, "breakers", "team-bot-a", "session-s1.json",
    );
    assert.ok(fs.existsSync(breakerPath), "breaker file written");
    const rec = JSON.parse(fs.readFileSync(breakerPath, "utf-8"));
    assert.equal(rec.bot_id, "team-bot-a");
    assert.equal(rec.session_id, "s1");
    assert.equal(rec.type, "session_budget");
    assert.equal(rec.state, "tripped");
    assert.equal(rec.cap_usd, 1.00);
    assert.equal(rec.cost_usd, 1.10);
    assert.equal(rec.last_model, "anthropic/claude-sonnet-4-6");
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("recordCost: idempotent — second crossing doesn't rewrite", (t, done) => {
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => 1.00,
    });
    m.recordCost({ sessionId: "s1", callCostUsd: 1.20 });
    const breakerPath = path.join(
      shared, "breakers", "team-bot-a", "session-s1.json",
    );
    const mtime1 = fs.statSync(breakerPath).mtimeMs;
    // Wait a tick so any rewrite would change mtime.
    setTimeout(() => {
      try {
        const r = m.recordCost({ sessionId: "s1", callCostUsd: 0.50 });
        assert.equal(r.trippedThisCall, false,
          "subsequent crossings don't re-trip");
        assert.equal(r.alreadyTripped, true);
        const mtime2 = fs.statSync(breakerPath).mtimeMs;
        assert.equal(mtime1, mtime2,
          "breaker file mtime must be unchanged on idempotent re-record");
        done();
      } finally {
        fs.rmSync(shared, { recursive: true, force: true });
      }
    }, 20);
  } catch (err) {
    fs.rmSync(shared, { recursive: true, force: true });
    throw err;
  }
});


test("recordCost: per-session isolation — bot's other sessions unaffected", () => {
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => 1.00,
    });
    m.recordCost({ sessionId: "runaway", callCostUsd: 2.00 });
    const r2 = m.recordCost({ sessionId: "healthy", callCostUsd: 0.40 });
    assert.equal(r2.trippedThisCall, false,
      "second session must not inherit the first's trip");
    const runawayBreaker = path.join(
      shared, "breakers", "team-bot-a", "session-runaway.json",
    );
    const healthyBreaker = path.join(
      shared, "breakers", "team-bot-a", "session-healthy.json",
    );
    assert.ok(fs.existsSync(runawayBreaker));
    assert.equal(fs.existsSync(healthyBreaker), false);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("recordCost: emitSignal fires once on trip, never on re-record", () => {
  const shared = tmpShared();
  try {
    let calls = 0;
    let captured = null;
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => 0.50,
      emitSignal: (rec) => { calls++; captured = rec; },
    });
    m.recordCost({ sessionId: "s1", callCostUsd: 0.30 });
    assert.equal(calls, 0);
    m.recordCost({ sessionId: "s1", callCostUsd: 0.30 });  // trips
    assert.equal(calls, 1);
    m.recordCost({ sessionId: "s1", callCostUsd: 0.30 });  // already tripped
    assert.equal(calls, 1, "emit only on first crossing");
    assert.equal(captured.session_id, "s1");
    assert.equal(captured.cap_usd, 0.50);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("recordCost: resolveCap throws → no-op, never crashes", () => {
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => { throw new Error("disk on fire"); },
    });
    const r = m.recordCost({ sessionId: "s1", callCostUsd: 999 });
    assert.equal(r.trippedThisCall, false);
    assert.equal(r.capUsd, null);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("recordCost: emitSignal throws → breaker file still landed", () => {
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => 0.50,
      emitSignal: () => { throw new Error("signal store down"); },
    });
    const r = m.recordCost({ sessionId: "s1", callCostUsd: 1.00 });
    assert.equal(r.trippedThisCall, true);
    const breakerPath = path.join(
      shared, "breakers", "team-bot-a", "session-s1.json",
    );
    assert.ok(fs.existsSync(breakerPath));
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("recordCost: negative / non-finite call cost is dropped", () => {
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => 1.00,
    });
    let r = m.recordCost({ sessionId: "s1", callCostUsd: -5 });
    assert.equal(r.accumulatedUsd, 0);
    r = m.recordCost({ sessionId: "s1", callCostUsd: NaN });
    assert.equal(r.accumulatedUsd, 0);
    r = m.recordCost({ sessionId: "s1", callCostUsd: Infinity });
    assert.equal(r.accumulatedUsd, 0);
    // Now a real value lands; cap is 1.00 so we shouldn't trip yet.
    r = m.recordCost({ sessionId: "s1", callCostUsd: 0.10 });
    assert.equal(r.accumulatedUsd, 0.10);
    assert.equal(r.trippedThisCall, false);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


// ── readSessionBudgetBreaker ─────────────────────────────────────────────────


test("readSessionBudgetBreaker: missing file → null", () => {
  const shared = tmpShared();
  try {
    const r = readSessionBudgetBreaker({
      sharedDir: shared, botId: "team-bot-a", sessionId: "absent",
    });
    assert.equal(r, null);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("readSessionBudgetBreaker: round-trip via SessionCostMonitor.recordCost", () => {
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => 0.50,
    });
    m.recordCost({
      sessionId: "s1",
      callCostUsd: 1.00,
      model: "anthropic/claude-sonnet-4-6",
      provider: "anthropic",
      userId: "U-USER",
      channelId: "C-CHAN",
      channelKind: "slack_dm",
    });
    const rec = readSessionBudgetBreaker({
      sharedDir: shared, botId: "team-bot-a", sessionId: "s1",
    });
    assert.ok(rec, "breaker record returned");
    assert.equal(rec.cost_usd, 1.00);
    assert.equal(rec.cap_usd, 0.50);
    assert.equal(rec.user_id, "U-USER");
    assert.equal(rec.channel_id, "C-CHAN");
    assert.equal(rec.channel_kind, "slack_dm");
    assert.equal(rec.last_model, "anthropic/claude-sonnet-4-6");
    assert.match(rec.reason, /exceeded per-session cap/);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("readSessionBudgetBreaker: malformed file → null (fail-open)", () => {
  const shared = tmpShared();
  try {
    const dir = path.join(shared, "breakers", "team-bot-a");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "session-bad.json"), "{ not json");
    const r = readSessionBudgetBreaker({
      sharedDir: shared, botId: "team-bot-a", sessionId: "bad",
    });
    assert.equal(r, null);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("readSessionBudgetBreaker: session id with funky chars is sanitized to match writer", () => {
  // The writer sanitizes session_id → file name (alphanum + _-). The
  // reader must apply the same transform so it finds what the writer
  // wrote. Round-trip a session id with a slash to confirm.
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => 0.10,
    });
    const sid = "agent:main:slack/dm:xyz";
    m.recordCost({ sessionId: sid, callCostUsd: 0.50 });
    const rec = readSessionBudgetBreaker({
      sharedDir: shared, botId: "team-bot-a", sessionId: sid,
    });
    assert.ok(rec, "round-trip with sanitized filename");
    assert.equal(rec.session_id, sid, "in-file session_id is the raw value");
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


// ── pruneIdle ───────────────────────────────────────────────────────────────


test("pruneIdle: removes buckets older than maxAge", () => {
  const shared = tmpShared();
  try {
    const m = new SessionCostMonitor({
      sharedDir: shared,
      botId: "team-bot-a",
      resolveCap: () => null,
    });
    const t0 = new Date("2026-05-31T00:00:00Z");
    m.recordCost({ sessionId: "old", callCostUsd: 0.10, now: t0 });
    m.recordCost({
      sessionId: "fresh",
      callCostUsd: 0.10,
      now: new Date(t0.getTime() + 10 * 60_000),
    });
    // Prune at t0 + 15 min with a 5 min max-age: only "old" goes.
    const removed = m.pruneIdle(
      5 * 60_000,
      new Date(t0.getTime() + 15 * 60_000),
    );
    assert.equal(removed, 1);
    assert.equal(m.peek("old").accumulatedUsd, 0);
    assert.equal(m.peek("fresh").accumulatedUsd, 0.10);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});
