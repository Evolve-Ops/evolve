/**
 * Tests for apps/GrowthLog — the report-only app growth-log observer.
 *
 * Brief: internal/dispatch/done/growth-log-observer.md. Covers the five
 * behaviours the brief names — an owned-file edit yields a delta carrying the
 * cause text; an unowned edit in an unattributed turn yields nothing; an
 * unattributable change lands as `unattributed_change`; the log is append-only
 * and rotates by UTC day; retention prunes past the horizon — plus the pure
 * extractors, marker-grade attribution, the DNT switch, and the per-day cap.
 *
 * The sweep half (attribution: "sweep") is Python and lives in
 * packages/analyzer/tests/test_app_growth_sweep.py.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/growthLog.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  GrowthLog,
  GROWTH_LOG_SCHEMA_VERSION,
  GROWTH_LOG_ROOT,
  GROWTH_LOG_RETENTION_DAYS,
  MAX_CAUSE_CHARS,
  MAX_RECORDS_PER_DAY_FILE,
  UNATTRIBUTED_SEGMENT,
  buildOwnershipIndex,
  extractFileWrites,
  growthAppDir,
  indexKey,
  isFileWriteTool,
  isReadCommandInput,
  normalizeAppPath,
  parseEvolveMarkerIds,
  pathsFromToolInput,
} from "../dist/apps/GrowthLog.js";


function fakeLogger() {
  const records = { debug: [], warn: [] };
  return {
    debug: (m) => records.debug.push(m),
    warn: (m) => records.warn.push(m),
    records,
  };
}

/** A tmp pod: sharedDir + a bot workspace with a manifests dir. */
function makePod(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-growth-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const sharedDir = path.join(root, "shared");
  const workspaceRoot = path.join(root, "bot", ".openclaw", "workspace");
  const manifestsDir = path.join(workspaceRoot, "manifests");
  fs.mkdirSync(sharedDir, { recursive: true });
  fs.mkdirSync(manifestsDir, { recursive: true });
  return { root, sharedDir, workspaceRoot, manifestsDir };
}

function writeManifest(pod, name, manifest) {
  fs.writeFileSync(
    path.join(pod.manifestsDir, `${name}.json`),
    JSON.stringify(manifest, null, 1),
  );
}

function writeWorkspaceFile(pod, rel, body) {
  const abs = path.join(pod.workspaceRoot, rel);
  fs.mkdirSync(path.dirname(abs), { recursive: true });
  fs.writeFileSync(abs, body);
  return abs;
}

function growthLogFor(pod, overrides = {}) {
  return new GrowthLog(
    {
      sharedDir: pod.sharedDir,
      botId: "team_bot_a",
      workspaceRoot: pod.workspaceRoot,
      manifestsDir: pod.manifestsDir,
      ...overrides,
    },
    fakeLogger(),
  );
}

/** An Anthropic-shaped agent_end messages payload with one write call. */
function writeTurn(toolName, input, { isError = false, id = "tu_1" } = {}) {
  return [
    { role: "user", content: [{ type: "text", text: "hi" }] },
    { role: "assistant", content: [{ type: "tool_use", id, name: toolName, input }] },
    {
      role: "user",
      content: [{ type: "tool_result", tool_use_id: id, is_error: isError, content: "ok" }],
    },
  ];
}

const TS = "2026-08-28T12:00:00.000Z";
const EXPLICIT = {
  app_id: "task-manager",
  app_attribution: "explicit",
  app_confidence: 1.0,
  app_attribution_source: "expand_app",
};
const NO_ATTRIBUTION = {
  app_id: null,
  app_attribution: "none",
  app_confidence: null,
  app_attribution_source: null,
};

function readDay(pod, appId, day = "2026-08-28") {
  const file = path.join(growthAppDir(pod.sharedDir, "team_bot_a", appId), `${day}.jsonl`);
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, "utf8").trim().split("\n")
    .filter(Boolean).map((l) => JSON.parse(l));
}


// ── Pure: write-tool recognition ─────────────────────────────────────────────

test("isFileWriteTool recognises the write family and rejects reads", () => {
  for (const name of [
    "write", "Write", "file_write", "edit", "multi_edit", "apply_patch",
    "str_replace_based_edit_tool", "notebook_edit", "create_file",
    "mcp__fs__write_file",
  ]) {
    assert.equal(isFileWriteTool(name), true, name);
  }
  for (const name of [
    "read", "Read", "read_file", "grep", "glob", "list_dir", "bash", "",
    null, undefined, 42,
  ]) {
    assert.equal(isFileWriteTool(name), false, String(name));
  }
});

test("isFileWriteTool prefers the read signal when a name carries both", () => {
  // "read_and_write_file" is not a real tool, but the precedence must be
  // stated: a name that looks like a read is never recorded as a write.
  assert.equal(isFileWriteTool("read_and_write_file"), false);
});


test("a view command on a write-named editor tool is not a write", () => {
  // str_replace_based_edit_tool multiplexes read and write behind one name.
  assert.equal(isReadCommandInput({ command: "view", path: "a.py" }), true);
  assert.equal(isReadCommandInput({ command: "str_replace", path: "a.py" }), false);
  assert.equal(isReadCommandInput({ path: "a.py" }), false);
  assert.equal(isReadCommandInput(null), false);

  const viewing = writeTurn("str_replace_based_edit_tool", {
    command: "view", path: "scripts/tasks.py",
  });
  assert.deepEqual(extractFileWrites(viewing), []);
  const editing = writeTurn("str_replace_based_edit_tool", {
    command: "str_replace", path: "scripts/tasks.py",
  });
  assert.equal(extractFileWrites(editing).length, 1);
});


// ── Pure: path extraction ────────────────────────────────────────────────────

test("pathsFromToolInput reads the aliased single-destination keys", () => {
  assert.deepEqual(pathsFromToolInput({ file_path: "scripts/a.py" }), ["scripts/a.py"]);
  assert.deepEqual(pathsFromToolInput({ path: " scripts/b.py " }), ["scripts/b.py"]);
  assert.deepEqual(pathsFromToolInput({ target_file: "c.py" }), ["c.py"]);
  assert.deepEqual(pathsFromToolInput({}), []);
  assert.deepEqual(pathsFromToolInput(null), []);
  assert.deepEqual(pathsFromToolInput("scripts/a.py"), []);
});

test("pathsFromToolInput pulls every path out of an apply_patch envelope", () => {
  const patch = [
    "*** Begin Patch",
    "*** Update File: scripts/tasks.py",
    "@@",
    "-old",
    "+new",
    "*** Add File: scripts/new.py",
    "+hello",
    "*** End Patch",
  ].join("\n");
  assert.deepEqual(
    pathsFromToolInput({ patch }),
    ["scripts/tasks.py", "scripts/new.py"],
  );
});

test("a file BODY that quotes a patch header does not mint phantom paths", () => {
  // `content` is the file's own bytes on a plain write. Scanning it for
  // apply_patch headers would record paths the turn never touched — a doc
  // about apply_patch is the obvious way to trip it.
  const body = "Docs say:\n*** Update File: some/other.py\nand so on\n";
  assert.deepEqual(
    pathsFromToolInput({ file_path: "docs/patching.md", content: body }),
    ["docs/patching.md"],
  );
});

test("pathsFromToolInput is re-entrant across calls (no sticky regex state)", () => {
  const patch = "*** Update File: a.py\n*** Add File: b.py\n";
  assert.deepEqual(pathsFromToolInput({ patch }), ["a.py", "b.py"]);
  assert.deepEqual(pathsFromToolInput({ patch }), ["a.py", "b.py"]);
});


// ── Pure: extraction from an agent_end payload ───────────────────────────────

test("extractFileWrites handles both provider message shapes", () => {
  assert.deepEqual(
    extractFileWrites(writeTurn("write", { file_path: "scripts/a.py" })),
    [{ tool: "write", path: "scripts/a.py" }],
  );
  const openai = [{
    role: "assistant",
    tool_calls: [{
      id: "c1",
      function: { name: "edit", arguments: JSON.stringify({ path: "scripts/b.py" }) },
    }],
  }];
  assert.deepEqual(
    extractFileWrites(openai),
    [{ tool: "edit", path: "scripts/b.py" }],
  );
});

test("extractFileWrites drops a write whose tool_result errored", () => {
  const msgs = writeTurn("write", { file_path: "scripts/a.py" }, { isError: true });
  assert.deepEqual(extractFileWrites(msgs), []);
});

test("extractFileWrites tolerates junk instead of throwing", () => {
  for (const junk of [null, undefined, 5, "messages", {}, [null, 3, { content: 7 }]]) {
    assert.deepEqual(extractFileWrites(junk), []);
  }
});


// ── Pure: normalization + markers ────────────────────────────────────────────

test("normalizeAppPath relativizes inside the workspace and rejects outside", () => {
  const ws = "/Users/team_bot_a/.openclaw/workspace";
  assert.equal(normalizeAppPath(`${ws}/scripts/Tasks.py`, ws), "scripts/Tasks.py");
  assert.equal(normalizeAppPath("./scripts/a.py", ws), "scripts/a.py");
  assert.equal(normalizeAppPath("code: scripts/a.py", ws), "scripts/a.py");
  assert.equal(normalizeAppPath("/tmp/scratch.py", ws), null);
  assert.equal(normalizeAppPath(`${ws}/../secrets.json`, ws), null);
  assert.equal(normalizeAppPath("", ws), null);
  assert.equal(normalizeAppPath(null, ws), null);
});

test("normalizeAppPath preserves case; only indexKey folds it", () => {
  const ws = "/ws";
  assert.equal(normalizeAppPath("/ws/Scripts/A.py", ws), "Scripts/A.py");
  assert.equal(indexKey("Scripts/A.py"), "scripts/a.py");
});

test("parseEvolveMarkerIds reads both marker forms and multi-id markers", () => {
  assert.deepEqual(
    parseEvolveMarkerIds("# evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@1"),
    ["p-a3f91c8b"],
  );
  assert.deepEqual(
    parseEvolveMarkerIds("<!-- evolve: spec=p-b2e04d1a file=f-1 -->"),
    ["p-b2e04d1a"],
  );
  assert.deepEqual(
    parseEvolveMarkerIds("# evolve: pkg=p-aaa@1,p-bbb@2 file=f-1@1"),
    ["p-aaa", "p-bbb"],
  );
  assert.deepEqual(
    parseEvolveMarkerIds('{"_evolve": {"pkg": "p-ccc@2026.1", "file": "f-2"}}'),
    ["p-ccc"],
  );
  assert.deepEqual(parseEvolveMarkerIds("no marker here"), []);
  assert.deepEqual(parseEvolveMarkerIds(null), []);
});


// ── Ownership index ──────────────────────────────────────────────────────────

test("buildOwnershipIndex indexes files, footprint and every marker id", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", {
    app_id: "task-manager",
    pkg_id: "p-a3f91c8b",
    files: ["scripts/Tasks.py", { path: "workspace/config/tasks.json" }],
    crons: [{ schedule: "0 9 * * *", script: "scripts/Tasks.py" }],
    scheduled_actions: [{ id: "daily-digest", script: "scripts/digest.py" }],
  });
  const idx = buildOwnershipIndex(pod.manifestsDir, pod.workspaceRoot);

  assert.equal(idx.byPath.get("scripts/tasks.py"), "task-manager");
  // Both the bare and the workspace/-prefixed alias resolve.
  assert.equal(idx.byPath.get("workspace/scripts/tasks.py"), "task-manager");
  assert.equal(idx.byPath.get("config/tasks.json"), "task-manager");
  assert.equal(idx.byMarkerId.get("p-a3f91c8b"), "task-manager");
  assert.deepEqual(
    idx.footprintByPath.get("scripts/tasks.py"),
    ["cron:0 9 * * *:scripts/Tasks.py"],
  );
  assert.deepEqual(idx.footprintByPath.get("scripts/digest.py"), ["action:daily-digest"]);
});

test("buildOwnershipIndex skips unparseable and identity-less manifests", (t) => {
  const pod = makePod(t);
  fs.writeFileSync(path.join(pod.manifestsDir, "broken.json"), "{not json");
  writeManifest(pod, "nameless", { files: ["scripts/x.py"] });
  writeManifest(pod, "_history_ish", { app_id: "hidden-app", files: ["scripts/y.py"] });
  fs.renameSync(
    path.join(pod.manifestsDir, "_history_ish.json"),
    path.join(pod.manifestsDir, "_history.json"),
  );
  const idx = buildOwnershipIndex(pod.manifestsDir, pod.workspaceRoot);
  assert.equal(idx.byPath.size, 0);
});


// ── The five behaviours the brief names ──────────────────────────────────────

test("a turn editing an OWNED file yields a delta carrying the cause verbatim", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", {
    app_id: "task-manager",
    files: ["scripts/tasks.py"],
    crons: [{ schedule: "0 9 * * *", script: "scripts/tasks.py" }],
  });
  const log = growthLogFor(pod);

  const written = log.recordTurn({
    messages: writeTurn("edit", {
      file_path: path.join(pod.workspaceRoot, "scripts/tasks.py"),
    }),
    sessionId: "sess-1",
    turnId: "turn-1",
    ts: TS,
    userMessage: "can the task list also show me what's overdue?",
    appAttribution: EXPLICIT,
  });

  assert.equal(written.length, 1);
  const rec = readDay(pod, "task-manager")[0];
  assert.equal(rec.schema_version, GROWTH_LOG_SCHEMA_VERSION);
  assert.equal(rec.kind, "app_delta");
  assert.equal(rec.app_id, "task-manager");
  assert.equal(rec.attribution, "manifest");
  assert.deepEqual(rec.files, ["scripts/tasks.py"]);
  assert.deepEqual(rec.footprint, ["cron:0 9 * * *:scripts/tasks.py"]);
  assert.equal(rec.cause, "can the task list also show me what's overdue?");
  assert.equal(rec.cause_source, "user_request");
  assert.equal(rec.cause_truncated, false);
  assert.equal(rec.session_id, "sess-1");
  assert.equal(rec.turn_id, "turn-1");
  assert.equal(rec.bot_id, "team_bot_a");
  assert.deepEqual(rec.tools, ["edit"]);
  assert.equal(rec.turn_app_id, "task-manager");
  assert.equal(rec.turn_app_attribution, "explicit");
});

test("an UNOWNED file edit in an unattributed turn yields nothing", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  const log = growthLogFor(pod);

  const written = log.recordTurn({
    messages: writeTurn("write", { file_path: "notes/scratch.md" }),
    sessionId: "sess-1", turnId: "turn-1", ts: TS,
    userMessage: "jot this down for me",
    appAttribution: NO_ATTRIBUTION,
  });

  assert.deepEqual(written, []);
  assert.equal(fs.existsSync(path.join(pod.sharedDir, GROWTH_LOG_ROOT)), false);
});

test("a write outside the workspace is never app surface", (t) => {
  const pod = makePod(t);
  const log = growthLogFor(pod);
  const written = log.recordTurn({
    messages: writeTurn("write", { file_path: "/tmp/scratch.py" }),
    sessionId: "s", turnId: "t", ts: TS,
    userMessage: "stash it in tmp",
    appAttribution: EXPLICIT,
  });
  assert.deepEqual(written, []);
});

test("an unattributable change in an app-attributed turn lands as unattributed_change", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  const log = growthLogFor(pod);

  const written = log.recordTurn({
    messages: writeTurn("write", { file_path: "scripts/brand_new_helper.py" }),
    sessionId: "sess-2", turnId: "turn-2", ts: TS,
    userMessage: "add a helper for the overdue logic",
    appAttribution: EXPLICIT,
  });

  assert.equal(written.length, 1);
  const rec = readDay(pod, null)[0];
  assert.equal(rec.kind, "unattributed_change");
  assert.equal(rec.app_id, null);
  assert.equal(rec.attribution, "none");
  // The turn's app context rides alongside — never promoted into app_id.
  assert.equal(rec.turn_app_id, "task-manager");
  assert.equal(rec.cause, "add a helper for the overdue logic");
  assert.deepEqual(rec.files, ["scripts/brand_new_helper.py"]);
  assert.equal(
    fs.existsSync(path.join(pod.sharedDir, GROWTH_LOG_ROOT, "team_bot_a", UNATTRIBUTED_SEGMENT)),
    true,
  );
});

test("the log is append-only and rotates by UTC day", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  const log = growthLogFor(pod);

  const turn = (ts, msg) => log.recordTurn({
    messages: writeTurn("edit", { file_path: "scripts/tasks.py" }),
    sessionId: "s", turnId: "t", ts, userMessage: msg, appAttribution: EXPLICIT,
  });

  turn("2026-08-28T09:00:00.000Z", "first change");
  turn("2026-08-28T17:00:00.000Z", "second change");
  turn("2026-08-29T01:00:00.000Z", "next day change");

  const day1 = readDay(pod, "task-manager", "2026-08-28");
  const day2 = readDay(pod, "task-manager", "2026-08-29");
  assert.equal(day1.length, 2, "same UTC day appends, never overwrites");
  assert.deepEqual(day1.map((r) => r.cause), ["first change", "second change"]);
  assert.equal(day2.length, 1, "a new UTC day rotates to a new file");
  assert.equal(day2[0].cause, "next day change");
});

test("retention prunes day-files past the horizon, keeping fresh ones", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  const appDir = growthAppDir(pod.sharedDir, "team_bot_a", "task-manager");
  fs.mkdirSync(appDir, { recursive: true });

  const day = (offset) => new Date(Date.now() - offset * 86_400_000)
    .toISOString().slice(0, 10);
  const stale = `${day(GROWTH_LOG_RETENTION_DAYS + 5)}.jsonl`;
  const fresh = `${day(1)}.jsonl`;
  fs.writeFileSync(path.join(appDir, stale), "{}\n");
  fs.writeFileSync(path.join(appDir, fresh), "{}\n");

  growthLogFor(pod).recordTurn({
    messages: writeTurn("edit", { file_path: "scripts/tasks.py" }),
    sessionId: "s", turnId: "t", ts: new Date().toISOString(),
    userMessage: "touch it", appAttribution: EXPLICIT,
  });

  assert.equal(fs.existsSync(path.join(appDir, stale)), false, "stale day-file pruned");
  assert.equal(fs.existsSync(path.join(appDir, fresh)), true, "fresh day-file kept");
});


// ── Marker-grade attribution ─────────────────────────────────────────────────

test("a file the manifest has not caught up to is attributed by its own marker", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", {
    app_id: "task-manager",
    pkg_id: "p-a3f91c8b",
    files: ["scripts/tasks.py"],
  });
  writeWorkspaceFile(
    pod, "scripts/helper.py",
    "# evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@1\nprint('hi')\n",
  );
  const log = growthLogFor(pod);

  log.recordTurn({
    messages: writeTurn("edit", { file_path: "scripts/helper.py" }),
    sessionId: "s", turnId: "t", ts: TS,
    userMessage: "tidy the helper", appAttribution: NO_ATTRIBUTION,
  });

  const rec = readDay(pod, "task-manager")[0];
  assert.equal(rec.attribution, "marker");
  assert.equal(rec.app_id, "task-manager");
  // Attribution came from the FILE, not the turn: the turn had no app context.
  assert.equal(rec.turn_app_attribution, "none");
});

test("a marker naming an app this bot does not have stays unattributed", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  writeWorkspaceFile(pod, "scripts/orphan.py", "# evolve: pkg=p-gone@1 file=f-1@1\n");
  const log = growthLogFor(pod);

  log.recordTurn({
    messages: writeTurn("edit", { file_path: "scripts/orphan.py" }),
    sessionId: "s", turnId: "t", ts: TS,
    userMessage: "poke the orphan", appAttribution: EXPLICIT,
  });

  assert.deepEqual(readDay(pod, "task-manager"), []);
  assert.equal(readDay(pod, null)[0].kind, "unattributed_change");
});


// ── Privacy + bounds ─────────────────────────────────────────────────────────

test("DNT suppresses the cause text but still records the delta", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  fs.writeFileSync(
    path.join(pod.sharedDir, "network.json"),
    JSON.stringify({ bots: { team_bot_a: { growthLog: false } } }),
  );
  const log = growthLogFor(pod);

  log.recordTurn({
    messages: writeTurn("edit", { file_path: "scripts/tasks.py" }),
    sessionId: "s", turnId: "t", ts: TS,
    userMessage: "something private", appAttribution: EXPLICIT,
  });

  const rec = readDay(pod, "task-manager")[0];
  assert.equal(rec.cause, null);
  assert.equal(rec.cause_source, "dnt");
  assert.deepEqual(rec.files, ["scripts/tasks.py"]);
});

test("an unreadable network.json fails OPEN to the default-on policy", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  fs.writeFileSync(path.join(pod.sharedDir, "network.json"), "{ broken");
  growthLogFor(pod).recordTurn({
    messages: writeTurn("edit", { file_path: "scripts/tasks.py" }),
    sessionId: "s", turnId: "t", ts: TS,
    userMessage: "still recorded", appAttribution: EXPLICIT,
  });
  assert.equal(readDay(pod, "task-manager")[0].cause, "still recorded");
});

test("cause text is truncated at the bound and says so", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  const long = "x".repeat(MAX_CAUSE_CHARS + 500);
  growthLogFor(pod).recordTurn({
    messages: writeTurn("edit", { file_path: "scripts/tasks.py" }),
    sessionId: "s", turnId: "t", ts: TS, userMessage: long, appAttribution: EXPLICIT,
  });
  const rec = readDay(pod, "task-manager")[0];
  assert.equal(rec.cause.length, MAX_CAUSE_CHARS);
  assert.equal(rec.cause_truncated, true);
});

test("the per-day-file record cap stops the log growing without bound", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  const appDir = growthAppDir(pod.sharedDir, "team_bot_a", "task-manager");
  fs.mkdirSync(appDir, { recursive: true });
  fs.writeFileSync(
    path.join(appDir, "2026-08-28.jsonl"),
    "{}\n".repeat(MAX_RECORDS_PER_DAY_FILE),
  );

  const written = growthLogFor(pod).recordTurn({
    messages: writeTurn("edit", { file_path: "scripts/tasks.py" }),
    sessionId: "s", turnId: "t", ts: TS,
    userMessage: "one too many", appAttribution: EXPLICIT,
  });
  assert.deepEqual(written, []);
  assert.equal(readDay(pod, "task-manager").length, MAX_RECORDS_PER_DAY_FILE);
});

test("one turn touching two apps writes one record per app", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  writeManifest(pod, "digest", { app_id: "digest-app", files: ["scripts/digest.py"] });
  const msgs = [
    {
      role: "assistant",
      content: [
        { type: "tool_use", id: "a", name: "edit", input: { file_path: "scripts/tasks.py" } },
        { type: "tool_use", id: "b", name: "edit", input: { file_path: "scripts/digest.py" } },
      ],
    },
  ];
  const written = growthLogFor(pod).recordTurn({
    messages: msgs, sessionId: "s", turnId: "t", ts: TS,
    userMessage: "wire the digest to the task list", appAttribution: EXPLICIT,
  });
  assert.equal(written.length, 2);
  assert.equal(readDay(pod, "task-manager").length, 1);
  assert.equal(readDay(pod, "digest-app").length, 1);
});

test("recordTurn never throws — an unwritable sharedDir is swallowed", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  const log = growthLogFor(pod, { sharedDir: path.join(pod.root, "no", "such", "\0bad") });
  assert.doesNotThrow(() => log.recordTurn({
    messages: writeTurn("edit", { file_path: "scripts/tasks.py" }),
    sessionId: "s", turnId: "t", ts: TS,
    userMessage: "boom", appAttribution: EXPLICIT,
  }));
});

test("a turn with no write calls writes nothing at all", (t) => {
  const pod = makePod(t);
  writeManifest(pod, "task_manager", { app_id: "task-manager", files: ["scripts/tasks.py"] });
  const written = growthLogFor(pod).recordTurn({
    messages: writeTurn("read", { file_path: "scripts/tasks.py" }),
    sessionId: "s", turnId: "t", ts: TS,
    userMessage: "what does this do?", appAttribution: EXPLICIT,
  });
  assert.deepEqual(written, []);
  assert.equal(fs.existsSync(path.join(pod.sharedDir, GROWTH_LOG_ROOT)), false);
});
