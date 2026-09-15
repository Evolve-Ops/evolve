/**
 * Regression tests for extractMessages — pins the "last non-empty per role"
 * semantic added 2026-06-06 after the pushback-detector audit.
 *
 * Background:
 *   The original implementation unconditionally overwrote userMessage /
 *   assistantMessage on each iteration. Real OC agent_end payloads end
 *   with non-text content blocks (trailing tool_result on user side, or
 *   trailing tool_use on assistant side when the agent loop finishes
 *   mid-tool-call). The trailing zero-text iteration wiped the captured
 *   text, leaving TurnRecord.{userMessage,assistantMessage} = "".
 *
 *   Production audit (2026-06-06, all 9 bots over 30 days):
 *     • 654 annotations had the pushback field populated (post-deploy)
 *     • 107 had a prior turn in the data (multi-turn session)
 *     • 105 of those (98%) returned `no_prior_turn` because the prior
 *       TurnRecord had empty userMessage and the detector bailed
 *     • Only 2 turns ever produced a non-null pushback score across 30
 *       days — i.e., the detector was effectively non-functional
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.extractMessages.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { _extractMessagesForTest as extractMessages } from "../dist/observer/TurnObserver.js";


// ── Pre-fix bug shapes ────────────────────────────────────────────────────

test("preserves user prompt even when last user message is tool_result-only", () => {
  // The canonical broken shape: user asks a question, agent calls a tool,
  // tool returns, agent replies. The LAST user message in the array is a
  // tool_result with no text content. Pre-fix code wiped userMessage to ""
  // because contentToString returned "" for the tool_result block.
  const messages = [
    { role: "user", content: "what's the weather in SF?" },
    { role: "assistant", content: [
      { type: "text", text: "Let me check." },
      { type: "tool_use", id: "t1", name: "weather", input: { city: "SF" } },
    ] },
    { role: "user", content: [
      { type: "tool_result", tool_use_id: "t1", content: "65F, sunny" },
    ] },
    { role: "assistant", content: [
      { type: "text", text: "It's 65 and sunny in San Francisco." },
    ] },
  ];
  const { userMessage, assistantMessage } = extractMessages(messages);
  assert.equal(userMessage, "what's the weather in SF?",
    "user prompt must survive trailing tool_result");
  assert.equal(assistantMessage, "It's 65 and sunny in San Francisco.",
    "final assistant text wins over earlier 'Let me check.'");
});

test("preserves assistant text even when last assistant block is tool_use only", () => {
  // The other broken shape: agent loop terminates mid-tool-call. The last
  // assistant message has only a tool_use block (no text). Pre-fix code
  // wiped assistantMessage to "".
  const messages = [
    { role: "user", content: "list files in /tmp" },
    { role: "assistant", content: [
      { type: "text", text: "I'll list /tmp for you." },
      { type: "tool_use", id: "t1", name: "shell", input: { cmd: "ls /tmp" } },
    ] },
    { role: "user", content: [
      { type: "tool_result", tool_use_id: "t1", content: "file1\nfile2" },
    ] },
    { role: "assistant", content: [
      { type: "tool_use", id: "t2", name: "shell", input: { cmd: "stat file1" } },
    ] },  // ← no text block, agent loop continuing
  ];
  const { userMessage, assistantMessage } = extractMessages(messages);
  assert.equal(userMessage, "list files in /tmp");
  assert.equal(assistantMessage, "I'll list /tmp for you.",
    "earlier non-empty assistant text must survive trailing tool_use-only block");
});

test("both ends trailing non-text: still recovers original prompt + reply", () => {
  // Adversarial: user-tool_result AND assistant-tool_use both trail.
  const messages = [
    { role: "user", content: "summarize this paper" },
    { role: "assistant", content: [
      { type: "text", text: "Here's a summary: lorem ipsum dolor sit amet." },
      { type: "tool_use", id: "t1", name: "fetch", input: { url: "x" } },
    ] },
    { role: "user", content: [
      { type: "tool_result", tool_use_id: "t1", content: "..." },
    ] },
    { role: "assistant", content: [
      { type: "tool_use", id: "t2", name: "search", input: {} },
    ] },
  ];
  const { userMessage, assistantMessage } = extractMessages(messages);
  assert.equal(userMessage, "summarize this paper");
  assert.equal(assistantMessage, "Here's a summary: lorem ipsum dolor sit amet.");
});

// ── Existing behavior preserved ────────────────────────────────────────────

test("last text wins among multiple user text messages (no regression)", () => {
  // Multi-turn conversation in a single messages array. The fix must
  // still pick the LATER of two non-empty user texts (so we capture the
  // current turn's prompt, not an older one).
  const messages = [
    { role: "user", content: "first question" },
    { role: "assistant", content: [{ type: "text", text: "first answer" }] },
    { role: "user", content: "second question" },
    { role: "assistant", content: [{ type: "text", text: "second answer" }] },
  ];
  const { userMessage, assistantMessage } = extractMessages(messages);
  assert.equal(userMessage, "second question");
  assert.equal(assistantMessage, "second answer");
});

test("string-form content (no blocks) works unchanged", () => {
  const messages = [
    { role: "user", content: "plain string user msg" },
    { role: "assistant", content: "plain string assistant msg" },
  ];
  const { userMessage, assistantMessage } = extractMessages(messages);
  assert.equal(userMessage, "plain string user msg");
  assert.equal(assistantMessage, "plain string assistant msg");
});

test("empty messages array returns empty strings", () => {
  const { userMessage, assistantMessage } = extractMessages([]);
  assert.equal(userMessage, "");
  assert.equal(assistantMessage, "");
});

test("non-array input returns empty strings (defensive)", () => {
  const r1 = extractMessages(null);
  assert.equal(r1.userMessage, "");
  assert.equal(r1.assistantMessage, "");
  const r2 = extractMessages(undefined);
  assert.equal(r2.userMessage, "");
  assert.equal(r2.assistantMessage, "");
  const r3 = extractMessages("not an array");
  assert.equal(r3.userMessage, "");
  assert.equal(r3.assistantMessage, "");
});

test("multiple text blocks in one assistant message are joined", () => {
  const messages = [
    { role: "user", content: "hi" },
    { role: "assistant", content: [
      { type: "text", text: "Hello" },
      { type: "text", text: "there." },
    ] },
  ];
  const { assistantMessage } = extractMessages(messages);
  // contentToString joins text blocks with a space + trims
  assert.equal(assistantMessage, "Hello there.");
});

test("messages with no text content anywhere return empty strings", () => {
  // Edge case: a turn that's purely tool-mediated with no human-readable
  // text on either side. Correct behavior is empty (detector will skip).
  const messages = [
    { role: "user", content: [
      { type: "tool_result", tool_use_id: "t1", content: "ok" },
    ] },
    { role: "assistant", content: [
      { type: "tool_use", id: "t2", name: "x", input: {} },
    ] },
  ];
  const { userMessage, assistantMessage } = extractMessages(messages);
  assert.equal(userMessage, "");
  assert.equal(assistantMessage, "");
});
