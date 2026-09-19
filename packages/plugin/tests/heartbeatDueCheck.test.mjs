/**
 * Tests for the model-free heartbeat/cron due-check (D-OH3,
 * internal/decision-evolve-overhead-2026-09-07.md).
 *
 * The contract under test is asymmetric on purpose and the tests are written
 * that way: a SKIP is the only outcome that costs something if it is wrong
 * (a silently dead heartbeat), so every ambiguous input here must land on
 * "run the model". Only a file that parses cleanly AND evaluates to nothing
 * due may suppress a turn.
 *
 * Imports from dist/ — the plugin's tests run after `tsc`, as the rest of
 * the suite does.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  HeartbeatDueCheck,
  parseDueConditions,
  parseInterval,
  parseClock,
  evaluateConditions,
  buildWakeContext,
  unsafeRelativePath,
  cronStateKeyPrefix,
  formatSilenceWindow,
  readMaxSilenceMs,
  DEFAULT_MAX_SILENCE_MS,
  DUE_CHECK_TRIGGERS,
} from "../dist/observer/HeartbeatDueCheck.js";
import {
  buildDecisionRecord,
  appendDecision,
  decisionsFilePath,
} from "../dist/observer/HeartbeatSkipLedger.js";

function tmpdir(prefix = "evolve-hb-") {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function writeConditions(ws, doc) {
  fs.writeFileSync(path.join(ws, "HEARTBEAT.json"), JSON.stringify(doc, null, 2));
}

// ── parsing ────────────────────────────────────────────────────────────────

test("parseInterval accepts m/h/d and bare minutes, rejects junk", () => {
  assert.equal(parseInterval("30m"), 30 * 60_000);
  assert.equal(parseInterval("6h"), 6 * 3_600_000);
  assert.equal(parseInterval("2d"), 2 * 86_400_000);
  assert.equal(parseInterval(45), 45 * 60_000);
  assert.equal(parseInterval("6 hours"), null);
  assert.equal(parseInterval("0h"), null);
  assert.equal(parseInterval(""), null);
  assert.equal(parseInterval(undefined), null);
});

test("parseClock accepts HH:MM only", () => {
  assert.equal(parseClock("06:30"), 390);
  assert.equal(parseClock("6:30"), 390);
  assert.equal(parseClock("23:59"), 1439);
  assert.equal(parseClock("24:00"), null);
  assert.equal(parseClock("06:60"), null);
  assert.equal(parseClock("0630"), null);
});

test("unsafeRelativePath refuses absolute paths and '..'", () => {
  assert.equal(unsafeRelativePath("inbox"), null);
  assert.equal(unsafeRelativePath("memory/notes.md"), null);
  assert.match(String(unsafeRelativePath("/etc/passwd")), /absolute/);
  assert.match(String(unsafeRelativePath("../../other-bot/.openclaw")), /\.\./);
  assert.match(String(unsafeRelativePath("")), /empty/);
});

test("parseDueConditions accepts a well-formed file", () => {
  const r = parseDueConditions(JSON.stringify({
    version: 1,
    conditions: [
      { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process inbox." },
      { id: "brief", when: "time_window", after: "06:30", before: "07:30", wake: "Brief." },
    ],
  }));
  assert.ok("ok" in r, JSON.stringify(r));
  assert.equal(r.ok.enabled, true);
  assert.equal(r.ok.conditions.length, 2);
});

test("parseDueConditions rejects an unknown 'when' rather than treating it as not-due", () => {
  const r = parseDueConditions(JSON.stringify({
    conditions: [{ id: "x", when: "file_chnaged", path: "a", wake: "w" }],
  }));
  assert.ok("error" in r);
  assert.match(r.error, /unknown 'when'/);
});

test("parseDueConditions rejects malformed JSON, bad version, dupe ids, missing wake", () => {
  assert.match(parseDueConditions("{not json").error, /not valid JSON/);
  assert.match(parseDueConditions(JSON.stringify({ version: 2 })).error, /unsupported version/);
  assert.match(
    parseDueConditions(JSON.stringify({
      conditions: [
        { id: "a", when: "path_exists", path: "p", wake: "w" },
        { id: "a", when: "path_exists", path: "q", wake: "w" },
      ],
    })).error,
    /duplicate condition id/,
  );
  assert.match(
    parseDueConditions(JSON.stringify({
      conditions: [{ id: "a", when: "path_exists", path: "p" }],
    })).error,
    /needs a 'wake'/,
  );
  assert.match(
    parseDueConditions(JSON.stringify({
      conditions: [{ id: "a", when: "time_window", after: "09:00", before: "08:00", wake: "w" }],
    })).error,
    /must be earlier/,
  );
});

test("parseDueConditions refuses a path that escapes the workspace", () => {
  const r = parseDueConditions(JSON.stringify({
    conditions: [{ id: "a", when: "path_exists", path: "../../etc/passwd", wake: "w" }],
  }));
  assert.ok("error" in r);
  assert.match(r.error, /'\.\.'/);
});

// ── evaluation ─────────────────────────────────────────────────────────────

const EMPTY_STATE = { version: 1, conditions: {} };

test("path_exists / path_missing / dir_non_empty read the real filesystem", () => {
  const ws = tmpdir();
  fs.mkdirSync(path.join(ws, "inbox"));
  fs.writeFileSync(path.join(ws, "flag"), "x");

  const conds = [
    { id: "flag", when: "path_exists", path: "flag", wake: "flag" },
    { id: "gone", when: "path_missing", path: "nope", wake: "gone" },
    { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "inbox" },
  ];
  let { due } = evaluateConditions(conds, ws, EMPTY_STATE, new Date());
  assert.deepEqual(due.map((d) => d.id).sort(), ["flag", "gone"]);

  fs.writeFileSync(path.join(ws, "inbox", "item.md"), "x");
  ({ due } = evaluateConditions(conds, ws, EMPTY_STATE, new Date()));
  assert.deepEqual(due.map((d) => d.id).sort(), ["flag", "gone", "inbox"]);
});

test("dir_non_empty ignores dotfiles", () => {
  const ws = tmpdir();
  fs.mkdirSync(path.join(ws, "q"));
  fs.writeFileSync(path.join(ws, "q", ".DS_Store"), "x");
  const conds = [{ id: "q", when: "dir_non_empty", path: "q", wake: "w" }];
  assert.equal(evaluateConditions(conds, ws, EMPTY_STATE, new Date()).due.length, 0);
});

test("file_changed is due on first sight, then only when mtime advances", () => {
  const ws = tmpdir();
  const f = path.join(ws, "notes.md");
  fs.writeFileSync(f, "one");
  const conds = [{ id: "notes", when: "file_changed", path: "notes.md", wake: "w" }];

  const first = evaluateConditions(conds, ws, EMPTY_STATE, new Date());
  assert.equal(first.due.length, 1, "never-seen file must wake the model");
  assert.ok(first.nextState.conditions.notes.lastMtimeMs > 0);

  const second = evaluateConditions(conds, ws, first.nextState, new Date());
  assert.equal(second.due.length, 0, "unchanged file is not due");

  const later = fs.statSync(f).mtimeMs + 5000;
  fs.utimesSync(f, new Date(later), new Date(later));
  const third = evaluateConditions(conds, ws, first.nextState, new Date());
  assert.equal(third.due.length, 1);
});

test("file_changed on an absent file is not due (nothing can have changed)", () => {
  const ws = tmpdir();
  const conds = [{ id: "x", when: "file_changed", path: "absent.md", wake: "w" }];
  assert.equal(evaluateConditions(conds, ws, EMPTY_STATE, new Date()).due.length, 0);
});

test("time_window fires once inside the window and not again that day", () => {
  const ws = tmpdir();
  const conds = [
    { id: "brief", when: "time_window", after: "06:30", before: "07:30", wake: "w" },
  ];
  const inside = new Date(2026, 8, 7, 6, 45, 0);
  const before = new Date(2026, 8, 7, 5, 59, 0);
  const after = new Date(2026, 8, 7, 8, 0, 0);

  assert.equal(evaluateConditions(conds, ws, EMPTY_STATE, before).due.length, 0);
  assert.equal(evaluateConditions(conds, ws, EMPTY_STATE, after).due.length, 0);

  const fired = evaluateConditions(conds, ws, EMPTY_STATE, inside);
  assert.equal(fired.due.length, 1);

  const again = evaluateConditions(
    conds, ws, fired.nextState, new Date(2026, 8, 7, 7, 10, 0),
  );
  assert.equal(again.due.length, 0, "one wake per window, not one per tick");

  const tomorrow = evaluateConditions(
    conds, ws, fired.nextState, new Date(2026, 8, 8, 6, 45, 0),
  );
  assert.equal(tomorrow.due.length, 1, "next day's window fires again");
});

test("every fires when the interval has elapsed, and on a first run", () => {
  const ws = tmpdir();
  const conds = [{ id: "sweep", when: "every", interval: "6h", wake: "w" }];
  const now = new Date(2026, 8, 7, 12, 0, 0);

  const first = evaluateConditions(conds, ws, EMPTY_STATE, now);
  assert.equal(first.due.length, 1, "never-run condition wakes the model");

  const soon = new Date(now.getTime() + 3 * 3_600_000);
  assert.equal(evaluateConditions(conds, ws, first.nextState, soon).due.length, 0);

  const later = new Date(now.getTime() + 6 * 3_600_000 + 1000);
  assert.equal(evaluateConditions(conds, ws, first.nextState, later).due.length, 1);
});

test("only the conditions that fired have their state advanced", () => {
  const ws = tmpdir();
  fs.writeFileSync(path.join(ws, "a.md"), "a");
  fs.writeFileSync(path.join(ws, "b.md"), "b");
  const conds = [
    { id: "a", when: "file_changed", path: "a.md", wake: "a" },
    { id: "b", when: "file_changed", path: "b.md", wake: "b" },
  ];
  const seeded = evaluateConditions(conds, ws, EMPTY_STATE, new Date()).nextState;
  const bump = fs.statSync(path.join(ws, "a.md")).mtimeMs + 5000;
  fs.utimesSync(path.join(ws, "a.md"), new Date(bump), new Date(bump));

  const run = evaluateConditions(conds, ws, seeded, new Date());
  assert.deepEqual(run.due.map((d) => d.id), ["a"]);
  assert.equal(run.nextState.conditions.b.lastMtimeMs, seeded.conditions.b.lastMtimeMs);
  assert.ok(run.nextState.conditions.a.lastMtimeMs > seeded.conditions.a.lastMtimeMs);
});

test("buildWakeContext names the due items and nothing else", () => {
  const text = buildWakeContext([
    { id: "inbox", wake: "Process the inbox.", evidence: "inbox holds 2 entries" },
  ]);
  assert.match(text, /Process the inbox\./);
  assert.match(text, /inbox holds 2 entries/);
  assert.match(text, /NO_REPLY/);
  assert.ok(!text.includes("HEARTBEAT.md checklist item"));
});

// ── the checker end-to-end ────────────────────────────────────────────────

/**
 * A checker over a bot that woke recently, so the max-silence floor is not
 * what these tests are measuring. The floor has its own tests below; seeding
 * ``lastWokeAt`` here is the difference between "a bot that has been running"
 * and "a bot nobody has heard from in a day", and only the second is the
 * floor's business.
 */
function checker(ws, shared, now, { seedLastWoke = true, maxSilenceMs } = {}) {
  const warnings = [];
  const check = new HeartbeatDueCheck({
    botId: "test_bot",
    sharedDir: shared,
    logger: { info: () => {}, warn: (m) => warnings.push(m), debug: () => {} },
    ...(now ? { now: () => now } : {}),
    ...(maxSilenceMs === undefined ? {} : { maxSilenceMs: () => maxSilenceMs }),
  });
  if (seedLastWoke) {
    fs.mkdirSync(path.dirname(check.statePath()), { recursive: true });
    fs.writeFileSync(
      check.statePath(),
      JSON.stringify({
        version: 1,
        conditions: {},
        lastWokeAt: (now ?? new Date()).toISOString(),
      }),
    );
  }
  return { check, warnings };
}

test("nothing due → skip", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  fs.mkdirSync(path.join(ws, "inbox"));
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process." }],
  });
  const { check } = checker(ws, shared);
  const v = check.evaluate(ws);
  assert.equal(v.decision, "skip");
  assert.equal(v.conditionsEvaluated, 1);
  assert.match(v.reason, /nothing due/);
});

test("one condition due → wake, carrying only that item", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  fs.mkdirSync(path.join(ws, "inbox"));
  fs.writeFileSync(path.join(ws, "inbox", "todo.md"), "x");
  fs.mkdirSync(path.join(ws, "other"));
  writeConditions(ws, {
    version: 1,
    conditions: [
      { id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process the inbox." },
      { id: "other", when: "dir_non_empty", path: "other", wake: "Do the other thing." },
    ],
  });
  const { check } = checker(ws, shared);
  const v = check.evaluate(ws);
  assert.equal(v.decision, "wake");
  assert.deepEqual(v.due.map((d) => d.id), ["inbox"]);
  const ctxText = buildWakeContext(v.due);
  assert.match(ctxText, /Process the inbox\./);
  assert.ok(!ctxText.includes("Do the other thing."), "the not-due item stays out of the prompt");
});

test("missing conditions file → unavailable with NO warning (that is the default)", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  const { check, warnings } = checker(ws, shared);
  const v = check.evaluate(ws);
  assert.equal(v.decision, "unavailable");
  assert.equal(v.warn, null);
  check.warnOnce(v.warn, ws);
  assert.deepEqual(warnings, []);
});

test("invalid conditions file → unavailable + exactly one warning", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  fs.writeFileSync(path.join(ws, "HEARTBEAT.json"), "{ oops");
  const { check, warnings } = checker(ws, shared);
  for (let i = 0; i < 3; i++) {
    const v = check.evaluate(ws);
    assert.equal(v.decision, "unavailable");
    check.warnOnce(v.warn, ws);
  }
  assert.equal(warnings.length, 1, "one warning per process, not one per heartbeat");
  assert.match(warnings[0], /invalid/);
  assert.match(warnings[0], /Running the model as usual/);
});

test("enabled:false and an empty condition list both keep today's behaviour", () => {
  const shared = tmpdir();
  const off = tmpdir();
  writeConditions(off, { version: 1, enabled: false, conditions: [] });
  assert.equal(checker(off, shared).check.evaluate(off).decision, "unavailable");

  const empty = tmpdir();
  writeConditions(empty, { version: 1, conditions: [] });
  assert.equal(checker(empty, shared).check.evaluate(empty).decision, "unavailable");
});

test("no workspace dir on the hook context → unavailable, never a skip", () => {
  const shared = tmpdir();
  const { check } = checker(tmpdir(), shared);
  assert.equal(check.evaluate(null).decision, "unavailable");
  assert.equal(check.evaluate("").decision, "unavailable");
  assert.equal(check.evaluate(undefined).decision, "unavailable");
});

test("state round-trips through the shared dir so a wake is not re-served", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "sweep", when: "every", interval: "6h", wake: "Sweep." }],
  });
  const t0 = new Date(2026, 8, 7, 12, 0, 0);
  const { check } = checker(ws, shared, t0);
  const first = check.evaluate(ws);
  assert.equal(first.decision, "wake");
  check.commitState(first.nextState);
  assert.ok(fs.existsSync(check.statePath()));

  // A fresh checker (process restart) reads the same state back — including
  // the lastWokeAt the wake above committed, so no re-seeding here.
  const { check: check2 } = checker(ws, shared, new Date(2026, 8, 7, 14, 0, 0), {
    seedLastWoke: false,
  });
  assert.equal(check2.evaluate(ws).decision, "skip");

  const { check: check3 } = checker(ws, shared, new Date(2026, 8, 7, 18, 30, 0), {
    seedLastWoke: false,
  });
  assert.equal(check3.evaluate(ws).decision, "wake");
});

test("a corrupt state file means everything is due, never a skip", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "sweep", when: "every", interval: "6h", wake: "Sweep." }],
  });
  const { check } = checker(ws, shared);
  fs.mkdirSync(path.dirname(check.statePath()), { recursive: true });
  fs.writeFileSync(check.statePath(), "not json at all");
  assert.equal(check.evaluate(ws).decision, "wake");
});

// ── the decisions ledger ──────────────────────────────────────────────────

test("a skip record is a KNOWN zero, not an unpriced null", () => {
  const rec = buildDecisionRecord({
    botId: "test_bot",
    trigger: "heartbeat",
    outcome: "skipped_nothing_due",
    conditionsEvaluated: 3,
    reason: "nothing due (3 conditions checked)",
    now: new Date("2026-09-07T14:00:00.000Z"),
  });
  assert.equal(rec.cost, 0);
  assert.equal(rec.cost_source, "no_model_call");
  assert.equal(rec.model, null);
  assert.equal(rec.source, "heartbeat");
  assert.equal(rec.outcome, "skipped_nothing_due");
  assert.deepEqual(rec.due_ids, []);
  assert.equal(rec.input_tokens + rec.output_tokens, 0);
});

test("decisions land beside the turns file, never inside its glob", () => {
  const shared = tmpdir();
  const rec = buildDecisionRecord({
    botId: "test_bot",
    trigger: "cron",
    outcome: "skipped_nothing_due",
    conditionsEvaluated: 1,
    reason: "nothing due (1 condition checked)",
    now: new Date("2026-09-07T14:00:00.000Z"),
  });
  assert.equal(appendDecision(shared, rec), true);
  const p = decisionsFilePath(shared, "test_bot", "2026-09-07");
  assert.ok(fs.existsSync(p));
  assert.ok(!path.basename(p).startsWith("turns-"), "must not shadow turns-<date>.jsonl");
  const parsed = JSON.parse(fs.readFileSync(p, "utf8").trim());
  assert.equal(parsed.source, "cron");
  assert.equal(parsed.schema_version, 1);
});

test("the trigger allowlist is exactly heartbeat + cron", () => {
  assert.deepEqual([...DUE_CHECK_TRIGGERS].sort(), ["cron", "heartbeat"]);
  assert.ok(!DUE_CHECK_TRIGGERS.includes("user"));
});

// ── the shipped starter ───────────────────────────────────────────────────

test("the starter HEARTBEAT.json Evolve ships parses with the parser that reads it", () => {
  // A starter an operator copies must be valid against the SHIPPED validator,
  // not against its author's memory of the schema. Cross-package on purpose:
  // the template lives with the other bot-workspace templates, the parser
  // lives here, and only a test can hold them together.
  const starter = new URL(
    "../../admin/evolve_admin/templates/bot_workspace/HEARTBEAT.json",
    import.meta.url,
  );
  const raw = fs.readFileSync(starter, "utf8");
  const parsed = parseDueConditions(raw);
  assert.ok("ok" in parsed, JSON.stringify(parsed));
  assert.equal(parsed.ok.enabled, false, "the starter must ship inert");
  assert.ok(parsed.ok.conditions.length >= 1);
  // Every documented condition kind should be demonstrated at least once, so
  // the starter doubles as the worked example the help doc points at.
  const kinds = new Set(parsed.ok.conditions.map((c) => c.when));
  for (const k of ["dir_non_empty", "file_changed", "time_window", "every"]) {
    assert.ok(kinds.has(k), `starter should demonstrate ${k}`);
  }
});

// ── cron scoping (a cron job is not a heartbeat) ───────────────────────────

const CRON_JOB = "9506f538-340e-4487-ae07-5675cb58b48c";

test("parseDueConditions namespaces cron state keys per job", () => {
  const parsed = parseDueConditions(
    JSON.stringify({
      version: 1,
      conditions: [{ id: "sweep", when: "every", interval: "6h", wake: "Sweep." }],
      cron: {
        [CRON_JOB]: [
          { id: "sweep", when: "every", interval: "6h", wake: "Back up." },
        ],
      },
    }),
  );
  assert.ok(parsed.ok, JSON.stringify(parsed));
  assert.equal(parsed.ok.conditions[0].stateKey, "sweep");
  assert.equal(
    parsed.ok.cron[CRON_JOB][0].stateKey,
    `${cronStateKeyPrefix(CRON_JOB)}sweep`,
  );
});

test("a non-object 'cron' is a parse error, so the file fails open", () => {
  const parsed = parseDueConditions(
    JSON.stringify({ version: 1, conditions: [], cron: ["job-a"] }),
  );
  assert.ok(parsed.error, "an array 'cron' must be rejected");
  assert.match(parsed.error, /'cron' must be an object/);
});

test("an unconditioned cron job is never claimed — the model runs", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  fs.mkdirSync(path.join(ws, "inbox"));
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process." }],
  });
  const { check } = checker(ws, shared);
  // Nothing is due on the heartbeat scope...
  assert.equal(check.evaluate(ws, { trigger: "heartbeat" }).decision, "skip");
  // ...and that says nothing whatever about a cron job.
  const v = check.evaluate(ws, { trigger: "cron", jobId: CRON_JOB });
  assert.equal(v.decision, "unavailable");
  assert.match(v.reason, /no conditions for cron job/);
  assert.equal(v.warn, null);
});

test("a cron trigger with no job id on the ctx is never claimed", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "x", when: "path_exists", path: "nope", wake: "w" }],
    cron: { [CRON_JOB]: [{ id: "y", when: "path_exists", path: "nope", wake: "w" }] },
  });
  const { check } = checker(ws, shared);
  const v = check.evaluate(ws, { trigger: "cron", jobId: null });
  assert.equal(v.decision, "unavailable");
  assert.match(v.reason, /no job id/);
});

test("a scoped cron job skips and wakes on its OWN conditions", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  fs.mkdirSync(path.join(ws, "outbox"));
  fs.mkdirSync(path.join(ws, "inbox"));
  fs.writeFileSync(path.join(ws, "inbox", "todo.md"), "x");
  writeConditions(ws, {
    version: 1,
    // The heartbeat has work to do. The cron job does not. They must not
    // borrow each other's answer in either direction.
    conditions: [{ id: "inbox", when: "dir_non_empty", path: "inbox", wake: "Process." }],
    cron: {
      [CRON_JOB]: [
        { id: "outbox", when: "dir_non_empty", path: "outbox", wake: "Send the outbox." },
      ],
    },
  });
  const { check } = checker(ws, shared);
  const skipped = check.evaluate(ws, { trigger: "cron", jobId: CRON_JOB });
  assert.equal(skipped.decision, "skip");
  assert.equal(skipped.cronJobId, CRON_JOB);
  assert.equal(skipped.conditionsEvaluated, 1, "only the cron scope is evaluated");

  fs.writeFileSync(path.join(ws, "outbox", "msg.md"), "x");
  const woke = check.evaluate(ws, { trigger: "cron", jobId: CRON_JOB });
  assert.equal(woke.decision, "wake");
  assert.deepEqual(woke.due.map((d) => d.id), ["outbox"]);
  assert.equal(woke.cronJobId, CRON_JOB);
});

test("a cron wake never marks a heartbeat condition served", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  const t0 = new Date(2026, 8, 7, 12, 0, 0);
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "sweep", when: "every", interval: "6h", wake: "Sweep." }],
    cron: { [CRON_JOB]: [{ id: "sweep", when: "every", interval: "6h", wake: "Back up." }] },
  });
  const { check } = checker(ws, shared, t0);
  const cronWake = check.evaluate(ws, { trigger: "cron", jobId: CRON_JOB });
  assert.equal(cronWake.decision, "wake");
  check.commitState(cronWake.nextState);

  // Same id, same interval — but the heartbeat's own "sweep" has not run, so
  // the heartbeat still wakes.
  const { check: hb } = checker(ws, shared, t0, { seedLastWoke: false });
  const hbVerdict = hb.evaluate(ws, { trigger: "heartbeat" });
  assert.equal(hbVerdict.decision, "wake");
  assert.deepEqual(hbVerdict.due.map((d) => d.id), ["sweep"]);
});

// ── the floor the bot cannot lower ─────────────────────────────────────────

test("formatSilenceWindow renders whole hours as hours", () => {
  assert.equal(formatSilenceWindow(DEFAULT_MAX_SILENCE_MS), "24h");
  assert.equal(formatSilenceWindow(6 * 3_600_000), "6h");
  assert.equal(formatSilenceWindow(90 * 60_000), "90m");
});

test("readMaxSilenceMs: default, pod value, per-bot override, off", () => {
  const shared = tmpdir();
  const write = (doc) =>
    fs.writeFileSync(path.join(shared, "network.json"), JSON.stringify(doc));
  assert.equal(readMaxSilenceMs(shared, "test_bot"), DEFAULT_MAX_SILENCE_MS);
  write({ heartbeat: { max_silence: "6h" } });
  assert.equal(readMaxSilenceMs(shared, "test_bot"), 6 * 3_600_000);
  write({
    heartbeat: { max_silence: "6h" },
    bots: { test_bot: { heartbeat: { max_silence: "90m" } } },
  });
  assert.equal(readMaxSilenceMs(shared, "test_bot"), 90 * 60_000);
  write({ heartbeat: { max_silence: 0 } });
  assert.equal(readMaxSilenceMs(shared, "test_bot"), 0, "0 turns the floor off");
  fs.writeFileSync(path.join(shared, "network.json"), "{ not json");
  assert.equal(
    readMaxSilenceMs(shared, "test_bot"),
    DEFAULT_MAX_SILENCE_MS,
    "an unreadable network.json keeps the default floor",
  );
});

test("the floor forces a wake once the bot has been silent past it", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  fs.mkdirSync(path.join(ws, "inbox"));
  writeConditions(ws, {
    version: 1,
    // Nothing here can ever fire — the shape a bot quieting itself would write.
    conditions: [{ id: "never", when: "path_exists", path: "never/there", wake: "w" }],
  });
  const t0 = new Date(2026, 8, 7, 12, 0, 0);
  const { check } = checker(ws, shared, t0, { maxSilenceMs: 24 * 3_600_000 });
  assert.equal(check.evaluate(ws, { trigger: "heartbeat" }).decision, "skip");

  const later = new Date(2026, 8, 8, 12, 0, 1); // 24h + 1s after the seeded wake
  const { check: past } = checker(ws, shared, later, {
    seedLastWoke: false,
    maxSilenceMs: 24 * 3_600_000,
  });
  const v = past.evaluate(ws, { trigger: "heartbeat" });
  assert.equal(v.decision, "wake");
  assert.deepEqual(v.due, [], "a floor wake names no due item — nothing was due");
  assert.equal(v.reason, "floor: no wake in 24h");
  assert.equal(v.nextState.lastWokeAt, later.toISOString());

  // Committing the floor wake resets the window: the next tick skips again.
  past.commitState(v.nextState);
  const { check: after } = checker(ws, shared, new Date(2026, 8, 8, 13, 0, 0), {
    seedLastWoke: false,
    maxSilenceMs: 24 * 3_600_000,
  });
  assert.equal(after.evaluate(ws, { trigger: "heartbeat" }).decision, "skip");
});

test("a bot that has never woken is past the floor", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "never", when: "path_exists", path: "never/there", wake: "w" }],
  });
  const { check } = checker(ws, shared, undefined, { seedLastWoke: false });
  const v = check.evaluate(ws, { trigger: "heartbeat" });
  assert.equal(v.decision, "wake");
  assert.match(v.reason, /^floor: no wake in /);
});

test("max_silence 0 turns the floor off — conditions have the last word", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "never", when: "path_exists", path: "never/there", wake: "w" }],
  });
  const { check } = checker(ws, shared, undefined, {
    seedLastWoke: false,
    maxSilenceMs: 0,
  });
  assert.equal(check.evaluate(ws, { trigger: "heartbeat" }).decision, "skip");
});

// ── tamper visibility ──────────────────────────────────────────────────────

test("every decision carries the conditions digest", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  const doc = {
    version: 1,
    conditions: [{ id: "never", when: "path_exists", path: "never/there", wake: "w" }],
  };
  writeConditions(ws, doc);
  const { check } = checker(ws, shared);
  const v = check.evaluate(ws, { trigger: "heartbeat" });
  assert.equal(v.decision, "skip");
  assert.match(v.conditionsSha256, /^[0-9a-f]{64}$/);
  const expected = crypto
    .createHash("sha256")
    .update(JSON.stringify(doc, null, 2))
    .digest("hex");
  assert.equal(v.conditionsSha256, expected);
});

test("a conditions file that changes under a running gateway warns once", () => {
  const ws = tmpdir();
  const shared = tmpdir();
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "inbox", when: "dir_non_empty", path: "inbox", wake: "w" }],
  });
  const { check, warnings } = checker(ws, shared);
  const first = check.evaluate(ws, { trigger: "heartbeat" });
  assert.deepEqual(warnings, [], "the first sight of a file is not a change");

  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "never", when: "path_exists", path: "never/there", wake: "w" }],
  });
  const second = check.evaluate(ws, { trigger: "heartbeat" });
  assert.notEqual(second.conditionsSha256, first.conditionsSha256);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /changed while the gateway was running/);

  // A third change does not re-warn: one line per process, per file.
  writeConditions(ws, {
    version: 1,
    conditions: [{ id: "other", when: "path_exists", path: "still/not/there", wake: "w" }],
  });
  check.evaluate(ws, { trigger: "heartbeat" });
  assert.equal(warnings.length, 1);
});
