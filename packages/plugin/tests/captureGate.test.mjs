/**
 * Tests for the D-TM4 capture gate (observer/CaptureGate.ts) — stage one.
 *
 * WHAT THESE PIN:
 *   * **Gate precision/recall on a 30-turn corpus** in the operator's phrasing
 *     (15 with a commitment, 15 without; user words and bot replies both).
 *     The numbers are pinned exactly — a regex edit that moves them must move
 *     this table, on purpose.
 *   * **A non-matching turn makes zero daemon calls** (and so zero model
 *     calls: the classifier lives only behind the daemon endpoint) — until the
 *     hourly counter report, which carries no capture.
 *   * **A matching turn posts exactly the capture request** — excerpt,
 *     speaker, turn id — plus the gate counters the health control reads.
 *   * **Non-user runs (heartbeat, cron, memory flush) are never gated.**
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/captureGate.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CaptureGate, GATE_REPORT_INTERVAL_MS, captureRequestsForTurn, gateMatches, registerCaptureGate,
} from "../dist/observer/CaptureGate.js";

// [speaker, text, isCommitment]
export const CORPUS = [
  ["user", "remind me to pay the deposit by the 30th", true],
  ["user", "I need to get the deposit to the venue by the 30th", true],
  ["user", "don't forget to renew the car registration", true],
  ["user", "can you follow up with the contractor about the quote", true],
  ["user", "let's pick this up tomorrow", true],
  ["user", "I have to send the invoice by Friday", true],
  ["user", "we'll need to book flights for the trip next week", true],
  ["user", "check back with me on the passport renewal", true],
  ["user", "the tax deadline is coming up, keep it on the list", true],
  ["user", "come back to the budget spreadsheet when you can", true],
  ["bot", "I'll chase the contract Thursday.", true],
  ["bot", "Got it — I'll check back Monday to see if the plumber replied.", true],
  ["bot", "I'll keep an eye on the inbox for the signed lease and let you know.", true],
  ["bot", "Let me get back to you tomorrow with the options.", true],
  ["bot", "I'll remind you about the dentist the day before.", true],
  ["user", "what's the weather like in the morning", false],
  ["user", "thanks, that's perfect", false],
  ["user", "how much did we spend on groceries this month", false],
  ["user", "can you summarise this email for me", false],
  ["user", "what time is the meeting today", false],
  ["user", "book a table for two at 7", false],
  ["user", "who sent the last message about the roof", false],
  ["user", "sounds good", false],
  ["bot", "I'll explain how the deposit terms work: the venue holds it until the event.", false],
  ["bot", "Here's the summary of the email you forwarded.", false],
  ["bot", "The meeting is at 3pm in the main office.", false],
  ["bot", "I will need the account number to look that up.", false],
  ["bot", "Done — the table is booked for 7pm.", false],
  ["bot", "You spent about 40 dollars more than last month.", false],
  ["bot", "Let me know if you want me to change anything.", false],
];

test("gate precision and recall on the 30-turn corpus are pinned", () => {
  let tp = 0, fp = 0, fn = 0, tn = 0;
  const misses = [];
  for (const [speaker, text, want] of CORPUS) {
    const got = gateMatches(text, speaker).length > 0;
    if (got && want) tp++;
    else if (got && !want) { fp++; misses.push(`FP ${speaker}: ${text}`); }
    else if (!got && want) { fn++; misses.push(`FN ${speaker}: ${text}`); }
    else tn++;
  }
  assert.equal(CORPUS.length, 30);
  assert.equal(CORPUS.filter((r) => r[2]).length, 15);
  // Pinned: recall 15/15, precision 15/15. Moving either is a deliberate act.
  assert.deepEqual({ tp, fp, fn, tn }, { tp: 15, fp: 0, fn: 0, tn: 15 }, misses.join("\n"));
});

function recordingGate() {
  const posts = [];
  const gate = new CaptureGate(async (body) => { posts.push(body); });
  return { gate, posts };
}

test("a non-matching turn makes zero daemon calls (hence zero model calls)", async () => {
  const { gate, posts } = recordingGate();
  const t0 = 1_000_000_000_000;
  await gate.observeTurn("sounds good", "Done — the table is booked for 7pm.", "s", "r0", t0);
  assert.equal(posts.length, 1, "the first turn reports the counters once");
  assert.equal(posts[0].capture, undefined);
  for (let i = 1; i <= 20; i++) {
    await gate.observeTurn("thanks, that's perfect", "Here's the summary.", "s", `r${i}`, t0 + i * 1000);
  }
  assert.equal(posts.length, 1, "no post at all for 20 quiet turns inside the hour");
  await gate.observeTurn("thanks", "ok", "s", "r21", t0 + GATE_REPORT_INTERVAL_MS);
  assert.equal(posts.length, 2);
  assert.deepEqual(posts[1], { gate: { evaluated: 22, matched: 0 } });
});

test("a matching turn posts the capture request with the gate counters", async () => {
  const { gate, posts } = recordingGate();
  await gate.observeTurn(
    "remind me to pay the deposit by the 30th", "I'll chase the contract Thursday.", "sess-1", "run-9");
  assert.equal(posts.length, 2);
  const [user, bot] = posts;
  assert.equal(user.capture.speaker, "user");
  assert.equal(user.capture.turn_id, "run-9");
  assert.equal(user.capture.session, "sess-1");
  assert.match(user.capture.text_excerpt, /deposit by the 30th/);
  assert.equal(bot.capture.speaker, "bot");
  assert.deepEqual(user.gate, { evaluated: 1, matched: 1 });
});

test("the gate strips the channel envelope before matching", () => {
  const wrapped = "Conversation info (untrusted metadata):\n```json\n{\"x\":1}\n```\nremind me to call the bank";
  const reqs = captureRequestsForTurn(wrapped, "", "s", "r");
  assert.equal(reqs.length, 1);
  assert.equal(reqs[0].text_excerpt.trim(), "remind me to call the bank");
});

test("a daemon failure is swallowed — nothing reaches the turn", async () => {
  const gate = new CaptureGate(async () => { throw new Error("socket down"); }, { info() {}, warn() {} });
  const reqs = await gate.observeTurn("remind me to call the bank", "", "s", "r");
  assert.equal(reqs.length, 1);
});

test("only user-triggered runs are gated", async () => {
  const handlers = [];
  const api = { on: (name, fn) => handlers.push([name, fn]) };
  const { gate, posts } = recordingGate();
  registerCaptureGate(api, gate, () => ({
    userMessage: "remind me to call the bank", assistantMessage: "",
  }));
  assert.deepEqual(handlers.map((h) => h[0]), ["agent_end"]);
  const fire = handlers[0][1];
  for (const trigger of ["heartbeat", "cron", "memory"]) await fire({ messages: [] }, { trigger, runId: "x" });
  assert.equal(posts.length, 0);
  await fire({ messages: [] }, { trigger: "user", runId: "run-1", sessionId: "s1" });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].capture.turn_id, "run-1");
});
