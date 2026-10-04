/**
 * Tests for _buildScheduledToolDisciplineNote — the BOT-visible tool-contract
 * note injected (via before_prompt_build → appendSystemContext) on scheduled
 * turns only.
 *
 * Motivation (one team bot, logs 2026-07-28 → 2026-09-09): ~1.4k failed tool calls
 * dominated by three refusals the model can avoid if it knows the contract on
 * the first call — exec preflight's "complex interpreter invocation", read's
 * EISDIR on a directory, and message's missing/unknown channel target. The
 * 2026-09-09 heartbeat that died with "Provider completed tool call with
 * malformed JSON arguments" hit all three in the 15s before it failed.
 *
 * The gate is classifySessionKind(...) === "scheduled", which is what keeps
 * these bytes off user turns; these tests pin the gate's inputs rather than
 * the prose, EXCEPT for the three contracts the note exists to state.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.scheduledToolDiscipline.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { _buildScheduledToolDisciplineNote } from "../dist/observer/TurnObserver.js";
import { classifySessionKind } from "../dist/tools/ToolProfiles.js";

test("the note states all three tool contracts it exists for", () => {
  const note = _buildScheduledToolDisciplineNote();
  // exec: a script file, not an inline interpreter invocation.
  assert.match(note, /exec:/);
  assert.match(note, /python path\/to\/script\.py/);
  assert.match(note, /heredoc/);
  // read: a file path, not a directory.
  assert.match(note, /read: the path must be a FILE/);
  // message: an explicit, actually-seen target.
  assert.match(note, /message: pass an explicit target/);
});

test("the note is tagged and stays within its per-turn byte budget", () => {
  const note = _buildScheduledToolDisciplineNote();
  assert.ok(note.startsWith("[EVOLVE BACKGROUND TURN]"));
  // Scheduled turns are cheap-rung and frequent; this block is only worth
  // its slot while it stays small. A deliberate raise should ride a diff.
  assert.ok(note.length < 800, `note is ${note.length} chars`);
});

test("the note quotes no OC error string verbatim (an upstream reword must not stale it)", () => {
  const note = _buildScheduledToolDisciplineNote();
  for (const ocErrorText of [
    "complex interpreter invocation detected",
    "EISDIR: illegal operation on a directory",
    "Explicit message target required for this run",
    "Unknown channel:",
  ]) {
    assert.ok(!note.includes(ocErrorText), `note quotes OC's wording: ${ocErrorText}`);
  }
});

test("gate: the real heartbeat key classifies as scheduled despite a slack channel", () => {
  // The 2026-09-09 failure was delivered over a live Slack DM, so a
  // channel-based test would have read it as user traffic.
  const { kind } = classifySessionKind("agent:main:main:heartbeat", "slack");
  assert.equal(kind, "scheduled");
});

test("gate: cron and scheduled keys are in, user and subagent keys are out", () => {
  for (const key of [
    "agent:main:cron:nightly-digest",
    "agent:main:scheduled:weekly",
    "agent:main:main:heartbeat",
  ]) {
    assert.equal(classifySessionKind(key, null).kind, "scheduled", key);
  }
  for (const [key, channel] of [
    ["agent:main:slack:direct:u0an8b80ajy", "slack"],
    ["agent:main:telegram:direct:1260193629", "telegram"],
    ["agent:main:subagent:abc", null],
  ]) {
    assert.notEqual(classifySessionKind(key, channel).kind, "scheduled", key);
  }
});

test("gate: a missing session key does not classify as scheduled", () => {
  // Fail-open direction: unknown provenance gets no injection rather than
  // guessing a background turn.
  for (const key of [null, undefined, ""]) {
    assert.notEqual(classifySessionKind(key, null).kind, "scheduled");
  }
});
