/**
 * SessionSummarizer × recurring_request — the WIRING, not the pure parts.
 *
 * Design §7.1a. These tests exist because the review of #3726 found the
 * wiring untested, and two real defects lived exactly there:
 *
 *   - the requester was read at session end (the LAST turn's sender) while
 *     the label came from the FIRST user turn, so in a multi-sender group
 *     session one person's ask was keyed under another person's identity
 *     — and the per-identity do-not-track gate was then checked against
 *     the wrong person;
 *   - the direct `session_end` hook path carries no `runId`, so on that
 *     path the field could never be emitted at all.
 *
 * Both are fixed by capturing the sender onto each TurnRecord when the
 * turn is recorded. These tests pin that the summarizer uses it.
 *
 * HOME/sharedDir are real temp dirs so a stray laptop ~/.openclaw or the
 * real /Users/Shared/evolve can never satisfy a config read.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/sessionSummarizer.recurringRequest.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { SessionSummarizer } from "../dist/observer/SessionSummarizer.js";
import { _resetRecurringRequestCaches } from "../dist/observer/recurringRequest.js";
import { _resetPodTimezoneCache } from "../dist/util/podTimezone.js";

function sharedDirWith(network, overlay) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-ss-"));
  fs.writeFileSync(
    path.join(dir, "network.json"),
    JSON.stringify({ timezone: "UTC", ...(network ?? {}) }),
  );
  if (overlay) {
    fs.mkdirSync(path.join(dir, "rosters"), { recursive: true });
    fs.writeFileSync(path.join(dir, "rosters", "bot-a.json"), JSON.stringify(overlay));
  }
  _resetRecurringRequestCaches();
  _resetPodTimezoneCache();
  return dir;
}

function makeSummarizer(sharedDir) {
  const logger = { debug() {}, info() {}, warn() {}, error() {} };
  const config = {
    botId: "bot-a",
    sharedDir,
    // Keep the tier3 outcome call out of a unit test entirely.
    enableLLMSummarization: false,
  };
  const api = {
    runtime: {
      subagent: {
        waitForRun() {
          throw new Error("no LLM call may happen in this test");
        },
      },
    },
  };
  return new SessionSummarizer(config, logger, api);
}

function turn(userMessage, requester, extra = {}) {
  return {
    userMessage,
    assistantMessage: "ok",
    session_class: "productive",
    class_confidence: 0.9,
    correction_detected: false,
    input_tokens: 10,
    output_tokens: 10,
    requester,
    ...extra,
  };
}

const ALICE = { platform: "telegram", senderId: "alice" };
const BOB = { platform: "telegram", senderId: "bob" };

async function summarize(sharedDir, turns) {
  const written = [];
  await makeSummarizer(sharedDir).summarize("sess-1", turns, (r) => written.push(r));
  assert.equal(written.length, 1, "exactly one session_summary must be written");
  return written[0];
}

test("emits recurring_request for a human-initiated session", async () => {
  const dir = sharedDirWith();
  const rec = await summarize(dir, [turn("give me the morning summary", ALICE)]);
  assert.equal(rec.type, "session_summary");
  // "summary" is itself an APPLICATION_PATTERNS keyword, so the session
  // tags as document-generation and the tag rides into the label —
  // design §7.1a's "<head>: <tags>" shape.
  assert.deepEqual(rec.recurring_request, {
    label: "morning summary: document-generation",
    requester: "telegram:alice",
    hour: new Date(rec.ts).getUTCHours(),
  });
  assert.deepEqual(Object.keys(rec.recurring_request).sort(),
    ["hour", "label", "requester"]);
});

test("the requester is the FIRST turn's sender, not the last", async () => {
  // Alice asks; Bob says thanks. The row must be Alice's.
  const dir = sharedDirWith();
  const rec = await summarize(dir, [
    turn("give me the morning summary", ALICE),
    turn("thanks", BOB),
  ]);
  assert.equal(rec.recurring_request.requester, "telegram:alice");
});

test("the per-identity DNT gate is checked against the ASKER", async () => {
  // Alice has opted out and asks; Bob, who has not, replies. Reading the
  // sender at session end would check Bob and emit Alice's ask anyway.
  const dir = sharedDirWith(null, { do_not_track: { "telegram:alice": {} } });
  const rec = await summarize(dir, [
    turn("give me the morning summary", ALICE),
    turn("thanks", BOB),
  ]);
  assert.equal(rec.recurring_request, undefined);
});

test("omits the field when no turn carries a sender (heartbeat / cron)", async () => {
  const dir = sharedDirWith();
  for (const requester of [null, undefined, { platform: null, senderId: null }]) {
    const rec = await summarize(dir, [turn("run the nightly digest", requester)]);
    assert.equal(rec.recurring_request, undefined);
    // The load-bearing summary is still written in full.
    assert.equal(rec.type, "session_summary");
    assert.equal(rec.turn_count, 1);
  }
});

test("omits the field when the per-bot DNT flag is off", async () => {
  const dir = sharedDirWith({ bots: { "bot-a": { recurringRequestSignal: false } } });
  const rec = await summarize(dir, [turn("give me the morning summary", ALICE)]);
  assert.equal(rec.recurring_request, undefined);
});

test("omits the field for an unkeyable ask, keeping the summary", async () => {
  const dir = sharedDirWith();
  const rec = await summarize(dir, [turn("thanks!", ALICE)]);
  assert.equal(rec.recurring_request, undefined);
  assert.equal(rec.type, "session_summary");
});

test("the field carries the app tags detected for the session", async () => {
  const dir = sharedDirWith();
  const rec = await summarize(dir, [
    turn("give me the morning summary of my email and calendar", ALICE),
  ]);
  const [head, tags] = rec.recurring_request.label.split(": ");
  assert.equal(head, "morning summary email calendar");
  assert.deepEqual(tags.split(" + ").sort(), ["calendar", "document-generation", "email"]);
});

test("an unusable sharedDir cannot cost the session_summary record", async () => {
  // The summary is the load-bearing record; the recurrence row is extra.
  const dir = path.join(os.tmpdir(), "evolve-does-not-exist-" + Date.now());
  _resetRecurringRequestCaches();
  _resetPodTimezoneCache();
  const rec = await summarize(dir, [turn("give me the morning summary", ALICE)]);
  assert.equal(rec.type, "session_summary");
  assert.equal(rec.turn_count, 1);
  // network.json unreadable → per-bot DNT fails OPEN, so the row still forms.
  assert.equal(rec.recurring_request.requester, "telegram:alice");
});

test("no session_summary field is lost by adding the new one", async () => {
  const dir = sharedDirWith();
  const rec = await summarize(dir, [turn("give me the morning summary", ALICE)]);
  for (const k of [
    "schema_version", "type", "session_id", "ts", "bot_id", "turn_count",
    "session_class", "tier", "tier_confidence", "first_response_resolution",
    "outcome", "complexity", "applications_invoked", "promises_made",
    "correction_count", "efficiency_flag", "total_input_tokens",
    "total_output_tokens",
  ]) {
    assert.ok(k in rec, `session_summary lost field: ${k}`);
  }
});
