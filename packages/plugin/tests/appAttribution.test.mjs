/**
 * Tests for apps/AppAttribution — the AL-1.1 run/session-scoped attribution
 * registry (docs/design-app-attribution-2026-08-15.md §4–§5).
 *
 * Covers the decision table (scheduled > explicit > none; within explicit,
 * last signal wins), the session-stickiness rule (flip on a different app,
 * expiry after N signal-less turns, scheduled-never-flips), conflict-ledger
 * writes, TTL + FIFO eviction, and the never-throws / error→none contract.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/appAttribution.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  configureAppAttribution,
  recordExplicit,
  recordScheduled,
  resolveForTurn,
  STICKY_SIGNALLESS_TURN_LIMIT,
  CONFLICT_LEDGER_FILENAME,
  _resetForTests,
} from "../dist/apps/AppAttribution.js";


function fakeLogger() {
  const records = { debug: [], warn: [] };
  return {
    debug: (m) => records.debug.push(m),
    warn: (m) => records.warn.push(m),
    records,
  };
}

const NONE = {
  app_id: null,
  app_attribution: "none",
  app_confidence: null,
  app_attribution_source: null,
};

test.beforeEach(() => _resetForTests());


// ── Decision table ───────────────────────────────────────────────────────────

test("no signal at all resolves the honest none tri-state", () => {
  assert.deepEqual(resolveForTurn("run-1", "sess-1"), NONE);
  assert.deepEqual(resolveForTurn(null, null), NONE);
  assert.deepEqual(resolveForTurn(undefined, undefined), NONE);
});

test("explicit run signal resolves explicit/1.0 with its source", () => {
  recordExplicit("run-1", "sess-1", "morning-briefing", "expand_app");
  assert.deepEqual(resolveForTurn("run-1", "sess-1"), {
    app_id: "morning-briefing",
    app_attribution: "explicit",
    app_confidence: 1.0,
    app_attribution_source: "expand_app",
  });
});

test("scheduled beats explicit (design §4 ordering)", () => {
  recordScheduled("sess-1", "cron-app");
  recordExplicit("run-1", "sess-1", "other-app", "script_middleware");
  const r = resolveForTurn("run-1", "sess-1");
  assert.equal(r.app_id, "cron-app");
  assert.equal(r.app_attribution, "scheduled");
  assert.equal(r.app_confidence, 1.0);
  assert.equal(r.app_attribution_source, "scheduled");
});

test("scheduled sources stay distinct — oc_cron_map / claim_file (AL-1.2 calibration contract)", () => {
  recordScheduled("sess-cron", "cron-app", "oc_cron_map");
  recordExplicit("run-1", "sess-cron", "other-app", "expand_app");
  assert.deepEqual(resolveForTurn("run-1", "sess-cron"), {
    app_id: "cron-app",
    app_attribution: "scheduled",
    app_confidence: 1.0,
    app_attribution_source: "oc_cron_map",
  });

  recordScheduled("sess-claim", "wrapped-app", "claim_file");
  assert.equal(resolveForTurn("run-2", "sess-claim").app_attribution_source, "claim_file");
  // Direct callers with no source still resolve the AL-1.1 default.
  recordScheduled("sess-legacy", "legacy-app");
  assert.equal(resolveForTurn("run-3", "sess-legacy").app_attribution_source, "scheduled");
});

test("within explicit, the LAST signal in the run wins", () => {
  recordExplicit("run-1", "sess-1", "app-a", "expand_app");
  recordExplicit("run-1", "sess-1", "app-b", "script_middleware");
  const r = resolveForTurn("run-1", "sess-1");
  assert.equal(r.app_id, "app-b");
  assert.equal(r.app_attribution_source, "script_middleware");
});

test("a signal on one run/session never leaks to another (subagent turns stay none)", () => {
  recordExplicit("run-user", "sess-user", "app-a", "expand_app");
  // An Evolve subagent turn carries its own run/session identifiers and no
  // explicit signal — it must resolve none, never the user app.
  assert.deepEqual(resolveForTurn("run-summarizer", "sess-summarizer"), NONE);
});

test("blank/invalid app ids record nothing", () => {
  recordExplicit("run-1", "sess-1", "", "expand_app");
  recordExplicit("run-1", "sess-1", "   ", "expand_app");
  assert.deepEqual(resolveForTurn("run-1", "sess-1"), NONE);
});


// ── Stickiness (design §5) ───────────────────────────────────────────────────

test("session becomes sticky on first explicit signal; later turns resolve explicit/sticky", () => {
  recordExplicit("run-1", "sess-1", "app-a", "expand_app");
  assert.equal(resolveForTurn("run-1", "sess-1").app_attribution_source, "expand_app");
  const later = resolveForTurn("run-2", "sess-1");
  assert.deepEqual(later, {
    app_id: "app-a",
    app_attribution: "explicit",
    app_confidence: 1.0,
    app_attribution_source: "sticky",
  });
});

test("an explicit signal for a DIFFERENT app flips the session", () => {
  recordExplicit("run-1", "sess-1", "app-a", "expand_app");
  resolveForTurn("run-1", "sess-1");
  recordExplicit("run-2", "sess-1", "app-b", "script_middleware");
  assert.equal(resolveForTurn("run-2", "sess-1").app_id, "app-b");
  // Subsequent sticky turns follow the flip.
  assert.equal(resolveForTurn("run-3", "sess-1").app_id, "app-b");
  assert.equal(resolveForTurn("run-3", "sess-1").app_attribution_source, "sticky");
});

test(`stickiness expires after ${STICKY_SIGNALLESS_TURN_LIMIT} signal-less turns`, () => {
  recordExplicit("run-0", "sess-1", "app-a", "expand_app");
  resolveForTurn("run-0", "sess-1"); // the signal turn itself
  for (let i = 1; i <= STICKY_SIGNALLESS_TURN_LIMIT; i++) {
    const r = resolveForTurn(`run-${i}`, "sess-1");
    assert.equal(r.app_id, "app-a", `turn ${i} still sticky`);
    assert.equal(r.app_attribution_source, "sticky");
  }
  // Turn N+1 without a signal: stickiness has lapsed.
  assert.deepEqual(resolveForTurn("run-expired", "sess-1"), NONE);
  // …and it stays lapsed (no zombie re-stick).
  assert.deepEqual(resolveForTurn("run-expired-2", "sess-1"), NONE);
});

test("a fresh explicit signal resets the signal-less counter", () => {
  recordExplicit("run-0", "sess-1", "app-a", "expand_app");
  resolveForTurn("run-0", "sess-1");
  for (let i = 1; i <= STICKY_SIGNALLESS_TURN_LIMIT - 1; i++) {
    resolveForTurn(`run-${i}`, "sess-1");
  }
  recordExplicit("run-again", "sess-1", "app-a", "expand_app");
  resolveForTurn("run-again", "sess-1");
  // A full fresh window of sticky turns is available again.
  for (let i = 1; i <= STICKY_SIGNALLESS_TURN_LIMIT; i++) {
    assert.equal(resolveForTurn(`run-b${i}`, "sess-1").app_id, "app-a");
  }
});

test("scheduled sessions never flip and never expire on signal-less turns", () => {
  recordScheduled("sess-cron", "cron-app");
  recordExplicit("run-1", "sess-cron", "other-app", "expand_app");
  assert.equal(resolveForTurn("run-1", "sess-cron").app_id, "cron-app");
  for (let i = 0; i < STICKY_SIGNALLESS_TURN_LIMIT + 5; i++) {
    const r = resolveForTurn(`run-quiet-${i}`, "sess-cron");
    assert.equal(r.app_id, "cron-app");
    assert.equal(r.app_attribution, "scheduled");
  }
});

test("stickiness is established at resolve time when the record site had no sessionId (Layer C)", () => {
  // Layer C's before_model_resolve ctx may not carry a sessionId.
  recordExplicit("run-1", null, "app-a", "layer_c");
  const r = resolveForTurn("run-1", "sess-1");
  assert.equal(r.app_attribution_source, "layer_c");
  assert.equal(resolveForTurn("run-2", "sess-1").app_attribution_source, "sticky");
  assert.equal(resolveForTurn("run-2", "sess-1").app_id, "app-a");
});


// ── Conflict ledger ──────────────────────────────────────────────────────────

test("conflicting explicit signals within one run append to the per-bot conflict ledger", () => {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-attr-"));
  configureAppAttribution({ sharedDir: shared, botId: "team_bot_a" }, fakeLogger());

  recordExplicit("run-1", "sess-1", "app-a", "expand_app");
  recordExplicit("run-1", "sess-1", "app-b", "script_middleware");
  // Same-app re-record is NOT a conflict.
  recordExplicit("run-1", "sess-1", "app-b", "layer_c");

  const file = path.join(shared, "team_bot_a", CONFLICT_LEDGER_FILENAME);
  const lines = fs.readFileSync(file, "utf-8").trim().split("\n");
  assert.equal(lines.length, 1);
  const rec = JSON.parse(lines[0]);
  assert.equal(rec.bot_id, "team_bot_a");
  assert.equal(rec.run_id, "run-1");
  assert.equal(rec.prior_app_id, "app-a");
  assert.equal(rec.prior_source, "expand_app");
  assert.equal(rec.app_id, "app-b");
  assert.equal(rec.source, "script_middleware");
  fs.rmSync(shared, { recursive: true, force: true });
});

test("conflict logging is best-effort: unconfigured or unwritable never throws, last signal still wins", () => {
  // Unconfigured (no configureAppAttribution call).
  recordExplicit("run-1", "sess-1", "app-a", "expand_app");
  recordExplicit("run-1", "sess-1", "app-b", "expand_app");
  assert.equal(resolveForTurn("run-1", "sess-1").app_id, "app-b");

  // Configured with an unwritable destination (sharedDir is a FILE).
  _resetForTests();
  const bogus = path.join(os.tmpdir(), `evolve-attr-file-${process.pid}`);
  fs.writeFileSync(bogus, "not a dir");
  configureAppAttribution({ sharedDir: bogus, botId: "team_bot_a" }, fakeLogger());
  recordExplicit("run-2", "sess-2", "app-a", "expand_app");
  recordExplicit("run-2", "sess-2", "app-b", "expand_app");
  assert.equal(resolveForTurn("run-2", "sess-2").app_id, "app-b");
  fs.rmSync(bogus, { force: true });
});


// ── TTL + FIFO eviction ──────────────────────────────────────────────────────

test("run signals TTL out (stale runId never resolves)", (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: 1_000_000 });
  recordExplicit("run-1", null, "app-a", "expand_app");
  t.mock.timers.setTime(1_000_000 + 31 * 60_000); // past the 30-min run TTL
  assert.deepEqual(resolveForTurn("run-1", null), NONE);
});

test("session stickiness TTLs out", (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: 1_000_000 });
  recordExplicit("run-1", "sess-1", "app-a", "expand_app");
  resolveForTurn("run-1", "sess-1");
  t.mock.timers.setTime(1_000_000 + 13 * 60 * 60_000); // past the 12-h session TTL
  assert.deepEqual(resolveForTurn("run-2", "sess-1"), NONE);
});

test("run map is FIFO-bounded — oldest entry evicts at capacity", () => {
  recordExplicit("run-first", null, "app-first", "expand_app");
  for (let i = 0; i < 1024; i++) {
    recordExplicit(`run-fill-${i}`, null, `app-${i}`, "expand_app");
  }
  assert.deepEqual(resolveForTurn("run-first", null), NONE);
  // Late entries survive.
  assert.equal(resolveForTurn("run-fill-1023", null).app_id, "app-1023");
});


// ── Never-throws / error→none contract ───────────────────────────────────────

test("hostile inputs never throw and resolve none", () => {
  assert.deepEqual(resolveForTurn(123, { weird: true }), NONE);
  recordExplicit({}, [], 42, null); // garbage in, no crash
  recordScheduled(null, undefined);
  assert.deepEqual(resolveForTurn({}, []), NONE);
});
