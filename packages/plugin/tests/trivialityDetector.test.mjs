/**
 * Tests for TrivialityDetector — pure-function feature extraction.
 *
 * Sibling of struggleDetector.test.mjs. Triviality is the demote signal
 * for user-facing cascade (spec § 2.5); demote requires positive
 * evidence (high triviality + low struggle), so this detector must
 * cleanly distinguish "trivial work" from "absence of struggle."
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/trivialityDetector.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  computeTriviality,
  lastUserMessageWordCount,
  lastAssistantMessageWordCount,
  singleDecisiveToolUsed,
  countStruggleMarkersForTriviality,
  countClarificationForTriviality,
  DEFAULT_TRIVIALITY_WEIGHTS,
} from "../dist/observer/TrivialityDetector.js";

// ── Test-message helpers ─────────────────────────────────────────────────────

function userText(text) {
  return { role: "user", content: text };
}

function assistantText(text) {
  return { role: "assistant", content: [{ type: "text", text }] };
}

function toolUse(name, args = {}) {
  return { type: "tool_use", id: `tu_${Math.random()}`, name, input: args };
}

function toolResult(toolUseId, content, isError = false) {
  return {
    type: "tool_result",
    tool_use_id: toolUseId,
    content,
    is_error: isError,
  };
}

function assistantWithTools(blocks) {
  return { role: "assistant", content: blocks };
}

function userWithResults(blocks) {
  return { role: "user", content: blocks };
}

// ── Word counts ──────────────────────────────────────────────────────────────

test("lastUserMessageWordCount: counts last user msg words", () => {
  assert.equal(lastUserMessageWordCount([userText("what's the weather")]), 3);
});

test("lastUserMessageWordCount: ignores earlier user msgs", () => {
  const msgs = [
    userText("a very long first message with many many words to ignore"),
    assistantText("ok"),
    userText("thanks"),
  ];
  assert.equal(lastUserMessageWordCount(msgs), 1);
});

test("lastUserMessageWordCount: empty when no user msg", () => {
  assert.equal(lastUserMessageWordCount([assistantText("hi")]), 0);
});

test("lastAssistantMessageWordCount: counts last assistant msg", () => {
  assert.equal(
    lastAssistantMessageWordCount([assistantText("65 degrees and sunny")]),
    4,
  );
});

test("lastAssistantMessageWordCount: ignores tool_use blocks", () => {
  const msgs = [
    assistantWithTools([
      toolUse("get_weather"),
      { type: "text", text: "It's sunny" },
    ]),
  ];
  assert.equal(lastAssistantMessageWordCount(msgs), 2);
});

// ── singleDecisiveToolUsed ──────────────────────────────────────────────────

test("singleDecisiveToolUsed: true on 1 tool, no errors", () => {
  const msgs = [
    assistantWithTools([toolUse("a")]),
    userWithResults([toolResult("a", "ok", false)]),
  ];
  assert.equal(singleDecisiveToolUsed(msgs), true);
});

test("singleDecisiveToolUsed: false on 1 tool with error", () => {
  const msgs = [
    assistantWithTools([toolUse("a")]),
    userWithResults([toolResult("a", "boom", true)]),
  ];
  assert.equal(singleDecisiveToolUsed(msgs), false);
});

test("singleDecisiveToolUsed: false on 2+ tools", () => {
  const msgs = [
    assistantWithTools([toolUse("a"), toolUse("b")]),
  ];
  assert.equal(singleDecisiveToolUsed(msgs), false);
});

test("singleDecisiveToolUsed: false on zero tools", () => {
  // Zero tools is NOT triviality — could be pure-reasoning hard question.
  assert.equal(singleDecisiveToolUsed([assistantText("yes")]), false);
});

// ── Struggle markers / clarification (rewards absence) ──────────────────────

test("countStruggleMarkersForTriviality: zero for clean text", () => {
  assert.equal(
    countStruggleMarkersForTriviality([assistantText("Here you go.")]),
    0,
  );
});

test("countStruggleMarkersForTriviality: detects 'let me try again'", () => {
  assert.equal(
    countStruggleMarkersForTriviality([assistantText("Let me try again with a different approach.")]),
    2,  // "let me try again" + "a different approach"
  );
});

test("countClarificationForTriviality: zero on benign user msg", () => {
  assert.equal(
    countClarificationForTriviality([userText("what's the weather?")]),
    0,
  );
});

test("countClarificationForTriviality: detects 'i meant'", () => {
  assert.equal(
    countClarificationForTriviality([userText("I meant the other one.")]),
    1,
  );
});

// ── computeTriviality: integration ──────────────────────────────────────────

test("computeTriviality: classic trivial turn → high score", () => {
  // Short user message + 1 successful tool + short assistant + no struggle markers
  // + no clarification + fast completion → maximally trivial.
  const sig = computeTriviality({
    messages: [
      userText("what's the weather"),
      assistantWithTools([toolUse("get_weather")]),
      userWithResults([toolResult("a", "65F sunny", false)]),
      assistantText("65 and sunny."),
    ],
    durationMs: 1500,
    success: true,
  });
  // Defaults sum to 1.0. Most features fully saturated.
  assert.ok(sig.score >= 0.85, `expected ≥0.85 trivial score, got ${sig.score}`);
  assert.equal(sig.raw.single_decisive_tool, 1);
});

test("computeTriviality: long thoughtful question → low score", () => {
  // Long user message, even though no struggle markers.
  const longQ = "I'd like you to help me think through a complex multi-part planning problem ".repeat(3);
  const sig = computeTriviality({
    messages: [
      userText(longQ),
      assistantText("Let me think about this. Here's my analysis: " + "many words ".repeat(50)),
    ],
    durationMs: 30000,  // 30s — not fast
    success: true,
  });
  assert.ok(sig.score < 0.4, `expected low triviality, got ${sig.score}`);
});

test("computeTriviality: presence of struggle markers tanks triviality", () => {
  // Short Q, but assistant said "let me try a different approach" — that's
  // struggle, not triviality. Triviality should be low even though some
  // features point trivial.
  const sig = computeTriviality({
    messages: [
      userText("quick q"),
      assistantText("Hmm, let me try a different approach. Got it now."),
    ],
    durationMs: 2000,
    success: true,
  });
  // no_struggle_markers contribution is zero (struggle markers present),
  // so weighted sum loses 0.15 from that feature.
  assert.ok(sig.score < 0.85, `expected reduced triviality, got ${sig.score}`);
});

test("computeTriviality: clarification loop tanks triviality", () => {
  // User said "i meant X" — they're correcting the bot. Even if the previous
  // turn was fast, the SESSION isn't trivial.
  const sig = computeTriviality({
    messages: [
      assistantText("here's your answer"),
      userText("I meant the other one"),
    ],
    durationMs: 1000,
    success: true,
  });
  assert.equal(sig.raw.no_clarification, 1);  // 1 clarification hit
  // no_clarification contribution drops to 0; that's 0.10 weight lost.
  assert.ok(sig.score < 0.85, `expected reduced triviality with clarification`);
});

test("computeTriviality: failed turn → score forced to 0", () => {
  // No matter how many positive features fire, a failed turn isn't trivial.
  const sig = computeTriviality({
    messages: [
      userText("yo"),
      assistantWithTools([toolUse("a")]),
      userWithResults([toolResult("a", "ok", false)]),
      assistantText("done"),
    ],
    durationMs: 500,
    success: false,
  });
  assert.equal(sig.score, 0);
});

test("computeTriviality: zero tools → single_decisive_tool feature is 0", () => {
  // Pure text exchange — single_decisive_tool fires 0, NOT 1.
  // Demotion logic should NOT demote on these turns (no positive evidence).
  const sig = computeTriviality({
    messages: [
      userText("hi"),
      assistantText("hello"),
    ],
    durationMs: 500,
    success: true,
  });
  assert.equal(sig.raw.single_decisive_tool, 0);
  assert.equal(sig.features.single_decisive_tool, 0);
});

test("computeTriviality: slow completion drops fast_completion contribution", () => {
  // Same content but two durations: fast vs slow.
  const baseMsgs = [
    userText("ok"),
    assistantText("yes"),
  ];
  const fast = computeTriviality({ messages: baseMsgs, durationMs: 500, success: true });
  const slow = computeTriviality({ messages: baseMsgs, durationMs: 60000, success: true });
  assert.ok(fast.score > slow.score, `fast should score higher than slow`);
  assert.equal(slow.features.fast_completion, 0);
});

// ── Payload drift (spec § 2.7) ──────────────────────────────────────────────

test("computeTriviality: score=null when messages is undefined", () => {
  const sig = computeTriviality({ messages: undefined, durationMs: 1000, success: true });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "no_messages");
});

test("computeTriviality: score=null when messages is not an array", () => {
  const sig = computeTriviality({ messages: "not an array", durationMs: 1000, success: true });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "messages_not_array");
});

test("computeTriviality: score=null when empty messages on failed turn", () => {
  const sig = computeTriviality({ messages: [], durationMs: 1000, success: false });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "empty_on_failure");
});

test("computeTriviality: empty messages on SUCCESS → score=0, not null", () => {
  const sig = computeTriviality({ messages: [], durationMs: 100, success: true });
  assert.equal(sig.score, 0);
  assert.equal(sig.payload_drift, null);
});

// ── Symmetry check vs StruggleDetector ────────────────────────────────────

test("computeTriviality: failed-on-trivial-input still triviality=0", () => {
  // Sanity: triviality and struggle should not both fire high on the same
  // turn. A failed turn IS struggle (StruggleDetector floors at 0.5).
  // Triviality must NOT fire high.
  const sig = computeTriviality({
    messages: [
      userText("ok"),
      assistantText("done"),
    ],
    durationMs: 500,
    success: false,
  });
  assert.equal(sig.score, 0);
});

// ── Custom weights ──────────────────────────────────────────────────────────

test("computeTriviality: custom weights honored", () => {
  // Zero all but short_user_message; max it out → score = 1.0
  const sig = computeTriviality({
    messages: [userText("hi"), assistantText("hello")],
    durationMs: 1000,
    weights: {
      short_user_message: 1.0,
      single_decisive_tool: 0,
      short_assistant_response: 0,
      no_struggle_markers: 0,
      no_clarification: 0,
      fast_completion: 0,
    },
  });
  // "hi" is 1 word, threshold 50 → normalize to (1 - 1/50) = 0.98
  // 0.98 × 1.0 = 0.98
  assert.ok(sig.score > 0.9 && sig.score <= 1.0, `expected ~0.98, got ${sig.score}`);
});

test("DEFAULT_TRIVIALITY_WEIGHTS: sums to 1.0", () => {
  // Triviality defaults sum to 1.0 (unlike Struggle's 0.85 which left
  // headroom for deferred features). All triviality features are in v1.
  const sum = Object.values(DEFAULT_TRIVIALITY_WEIGHTS).reduce((a, b) => a + b, 0);
  assert.ok(Math.abs(sum - 1.0) < 0.0001, `expected sum=1.0, got ${sum}`);
});
