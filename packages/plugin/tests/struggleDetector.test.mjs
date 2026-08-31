/**
 * Tests for StruggleDetector — pure-function feature extraction from
 * agent_end message arrays.
 *
 * Spec: internal/spec-tier-cascade-2026-05-26.md § 2.1.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/struggleDetector.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  computeStruggle,
  countToolErrors,
  countToolRetries,
  countRestartMarkers,
  countClarificationLoops,
  tokensPerProgressRatio,
  countToolCalls,
  DEFAULT_STRUGGLE_WEIGHTS,
} from "../dist/observer/StruggleDetector.js";

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

// ── countToolErrors ──────────────────────────────────────────────────────────

test("countToolErrors: zero when no tool_result blocks", () => {
  assert.equal(countToolErrors([userText("hi"), assistantText("hello")]), 0);
});

test("countToolErrors: counts is_error=true blocks only", () => {
  const msgs = [
    userText("do a thing"),
    assistantWithTools([toolUse("readFile"), toolUse("writeFile")]),
    userWithResults([
      toolResult("a", "ok", false),
      toolResult("b", "boom", true),
      toolResult("c", "boom", true),
    ]),
  ];
  assert.equal(countToolErrors(msgs), 2);
});

test("countToolErrors: tolerates malformed messages", () => {
  assert.equal(countToolErrors([null, undefined, 42, { role: "user" }]), 0);
});

// ── countToolRetries ─────────────────────────────────────────────────────────

test("countToolRetries: zero with one call per tool", () => {
  const msgs = [
    assistantWithTools([toolUse("a"), toolUse("b"), toolUse("c")]),
  ];
  assert.equal(countToolRetries(msgs), 0);
});

test("countToolRetries: two same-name calls = 1 retry", () => {
  const msgs = [
    assistantWithTools([toolUse("readFile"), toolUse("readFile")]),
  ];
  assert.equal(countToolRetries(msgs), 1);
});

test("countToolRetries: three same-name calls = 2 retries", () => {
  const msgs = [
    assistantWithTools([
      toolUse("readFile"),
      toolUse("readFile"),
      toolUse("readFile"),
    ]),
  ];
  assert.equal(countToolRetries(msgs), 2);
});

test("countToolRetries: counts per-tool, summed", () => {
  const msgs = [
    assistantWithTools([
      toolUse("a"), toolUse("a"),       // 1 retry
      toolUse("b"), toolUse("b"), toolUse("b"),  // 2 retries
      toolUse("c"),                     // 0 retries
    ]),
  ];
  assert.equal(countToolRetries(msgs), 3);
});

// ── countRestartMarkers ──────────────────────────────────────────────────────

test("countRestartMarkers: zero for plain text", () => {
  assert.equal(
    countRestartMarkers([assistantText("Here is your answer.")]),
    0,
  );
});

test("countRestartMarkers: detects 'let me try a different'", () => {
  assert.equal(
    countRestartMarkers([assistantText("Let me try a different approach.")]),
    // This matches both "let me try ... different" and "approach" patterns,
    // which is desirable — multiple signals = stronger struggle indication.
    2,
  );
});

test("countRestartMarkers: detects 'that didn't work'", () => {
  assert.equal(
    countRestartMarkers([assistantText("That didn't work. Trying again.")]),
    1,
  );
});

test("countRestartMarkers: detects 'hmm, actually'", () => {
  // "Hmm, actually" matches the "hmm,? \s+ (actually|wait|let me|that|i)"
  // pattern; "actually let me" matches the separate "(actually|wait),? \s+
  // (let me|...)" pattern. Two patterns hitting the same phrase is fine —
  // overlapping signals = stronger struggle indication.
  assert.equal(
    countRestartMarkers([assistantText("Hmm, actually let me reconsider.")]),
    2,
  );
});

test("countRestartMarkers: case-insensitive", () => {
  assert.equal(
    countRestartMarkers([assistantText("LET ME TRY AGAIN")]),
    1,
  );
});

test("countRestartMarkers: counts multiple hits across multiple blocks", () => {
  const msgs = [
    assistantWithTools([
      { type: "text", text: "Let me try again." },
      { type: "tool_use", name: "x" },
      { type: "text", text: "That didn't work." },
    ]),
  ];
  assert.equal(countRestartMarkers(msgs), 2);
});

// ── countClarificationLoops ──────────────────────────────────────────────────

test("countClarificationLoops: zero for benign user message", () => {
  assert.equal(
    countClarificationLoops([userText("Could you summarize the document?")]),
    0,
  );
});

test("countClarificationLoops: detects 'no, that's not'", () => {
  assert.equal(
    countClarificationLoops([
      assistantText("here's the summary"),
      userText("No, that's not what I asked for."),
    ]),
    // Three patterns hit this phrase: "no, that" (broad "no" pattern),
    // "that's not what i" (specific clarification), and "i asked for"
    // (verb-of-correction). Multiple overlapping hits = strong signal.
    3,
  );
});

test("countClarificationLoops: detects 'i meant'", () => {
  assert.equal(
    countClarificationLoops([userText("I meant the other file.")]),
    1,
  );
});

test("countClarificationLoops: only looks at last user message", () => {
  // Even though an earlier user message is benign, only the last user
  // message is analyzed (avoids re-attributing across turns).
  const msgs = [
    userText("first thing"),
    assistantText("ok"),
    userText("You misunderstood completely."),
  ];
  assert.equal(countClarificationLoops(msgs), 1);
});

// ── tokensPerProgressRatio ───────────────────────────────────────────────────

test("tokensPerProgressRatio: zero when no tools used", () => {
  assert.equal(
    tokensPerProgressRatio([assistantText("just text")], 10000),
    0,
  );
});

test("tokensPerProgressRatio: zero when no duration", () => {
  const msgs = [assistantWithTools([toolUse("a")])];
  assert.equal(tokensPerProgressRatio(msgs, undefined), 0);
  assert.equal(tokensPerProgressRatio(msgs, 0), 0);
});

test("tokensPerProgressRatio: duration / successful_calls", () => {
  const msgs = [
    assistantWithTools([toolUse("a"), toolUse("b")]),
    userWithResults([
      toolResult("a", "ok", false),
      toolResult("b", "ok", false),
    ]),
  ];
  // 10000ms / 2 successful = 5000
  assert.equal(tokensPerProgressRatio(msgs, 10000), 5000);
});

test("tokensPerProgressRatio: all-error doesn't divide by zero", () => {
  const msgs = [
    assistantWithTools([toolUse("a")]),
    userWithResults([toolResult("a", "boom", true)]),
  ];
  // 0 successful → denom floor = 1; ratio = durationMs
  assert.equal(tokensPerProgressRatio(msgs, 10000), 10000);
});

// ── computeStruggle: integration ─────────────────────────────────────────────

test("computeStruggle: clean turn → low score", () => {
  const sig = computeStruggle({
    messages: [
      userText("what's the weather?"),
      assistantWithTools([
        toolUse("get_weather"),
      ]),
      userWithResults([toolResult("a", "65 and sunny", false)]),
      assistantText("It's 65 and sunny."),
    ],
    durationMs: 1500,
    success: true,
  });
  assert.ok(sig.score < 0.1, `expected low score, got ${sig.score}`);
  assert.equal(sig.raw.tool_error_count, 0);
  assert.equal(sig.raw.tool_retry_count, 0);
});

test("computeStruggle: tool errors push score up", () => {
  const sig = computeStruggle({
    messages: [
      userText("read file"),
      assistantWithTools([toolUse("readFile"), toolUse("readFile")]),
      userWithResults([
        toolResult("a", "ENOENT", true),
        toolResult("b", "EACCES", true),
      ]),
    ],
    durationMs: 5000,
    success: true,
  });
  // 2 errors saturates tool_error_count (sat=2) → 1.0 * 0.25 = 0.25
  // 2 same-name calls = 1 retry → 1/3 * 0.20 ≈ 0.067
  // Should clearly be >= 0.25.
  assert.ok(sig.score >= 0.25, `expected ≥0.25, got ${sig.score}`);
  assert.equal(sig.raw.tool_error_count, 2);
  assert.equal(sig.raw.tool_retry_count, 1);
});

test("computeStruggle: user correction is a strong signal", () => {
  const sig = computeStruggle({
    messages: [
      assistantText("Here's the summary."),
      userText("No, that's not what I asked for."),
    ],
    durationMs: 800,
    success: true,
  });
  // 2 clarification hits saturates (sat=1) → 1.0 * 0.15 = 0.15
  assert.ok(sig.score >= 0.15, `expected ≥0.15, got ${sig.score}`);
});

test("computeStruggle: success=false floors score at 0.5", () => {
  const sig = computeStruggle({
    messages: [assistantText("...")],
    durationMs: 500,
    success: false,
  });
  assert.ok(sig.score >= 0.5, `expected ≥0.5 on failed turn, got ${sig.score}`);
});

test("computeStruggle: combined struggle saturates near 1", () => {
  const sig = computeStruggle({
    messages: [
      userText("No, that's not what I asked. I meant the other file."),
      assistantWithTools([
        { type: "text", text: "Let me try a different approach. That didn't work either." },
        toolUse("a"), toolUse("a"), toolUse("a"),
      ]),
      userWithResults([
        toolResult("a", "boom", true),
        toolResult("b", "boom", true),
        toolResult("c", "boom", true),
      ]),
    ],
    durationMs: 60000,
    success: false,
  });
  // Default weights sum to 0.85, so a fully-saturated combined struggle
  // tops out near 0.85 (not 1.0). That's by design — we leave headroom
  // for the two features deferred from v1 (plan_drift_distance and
  // tool_timeout_rate). Cascade thresholds are calibrated against the
  // observed distribution in Phase 2, not against a synthetic ceiling.
  assert.ok(sig.score >= 0.75, `expected ≥0.75 for full struggle, got ${sig.score}`);
  assert.ok(sig.score <= 1.0, `clamped to ≤1`);
});

test("computeStruggle: malformed message ITEMS still produce a score", () => {
  // Array-of-junk is well-formed payload shape — just unusable content.
  // Detector treats it as a real (empty) array; score=0 with no drift.
  const sig = computeStruggle({
    messages: [null, undefined, 42, "not an object", { role: "user" }],
    durationMs: 1000,
    success: true,
  });
  assert.equal(sig.score, 0);
  assert.equal(sig.payload_drift, null);
});

// ── Payload drift contract (round-3 code review #1) ──────────────────────────

test("computeStruggle: score=null when messages is undefined", () => {
  const sig = computeStruggle({
    messages: undefined,
    durationMs: 1000,
    success: true,
  });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "no_messages");
});

test("computeStruggle: score=null when messages is null", () => {
  const sig = computeStruggle({
    messages: null,
    durationMs: 1000,
    success: true,
  });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "no_messages");
});

test("computeStruggle: score=null when messages is not an array", () => {
  const sig = computeStruggle({
    messages: "this is not an array",
    durationMs: 1000,
    success: true,
  });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "messages_not_array");
});

test("computeStruggle: score=null when messages is empty AND turn failed", () => {
  // The specific drift case: turn marked failed but produced no inspectable
  // content. Distinguished from "successful empty-message turn" which is
  // legitimately a 0 (e.g., a turn that ended immediately).
  const sig = computeStruggle({
    messages: [],
    durationMs: 1000,
    success: false,
  });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "empty_on_failure");
});

test("computeStruggle: empty messages on SUCCESSFUL turn → score=0, not null", () => {
  // Successful empty turn is a real outcome (rare but valid). Don't flag
  // it as drift.
  const sig = computeStruggle({
    messages: [],
    durationMs: 100,
    success: true,
  });
  assert.equal(sig.score, 0);
  assert.equal(sig.payload_drift, null);
});

test("computeStruggle: null-score sentinel still carries zeroed features (not undefined)", () => {
  // Downstream consumers expect features/raw to always be present as
  // {tool_error_count: 0, ...} so they can sum / display without
  // null-checking each one. Only the top-level score is nullable.
  const sig = computeStruggle({ messages: undefined });
  assert.equal(typeof sig.features, "object");
  assert.equal(sig.features.tool_error_count, 0);
  assert.equal(sig.features.tool_retry_count, 0);
  assert.equal(typeof sig.raw, "object");
  assert.equal(sig.raw.tool_error_count, 0);
});

test("computeStruggle: custom weights are honored", () => {
  // Zero out everything except tool_error_count, max it out → ~1.0.
  const sig = computeStruggle({
    messages: [
      assistantWithTools([toolUse("a"), toolUse("b")]),
      userWithResults([
        toolResult("a", "x", true),
        toolResult("b", "x", true),
      ]),
    ],
    durationMs: 1000,
    weights: {
      tool_error_count: 1.0,
      tool_retry_count: 0,
      restart_markers: 0,
      clarification_loops: 0,
      tokens_per_progress: 0,
      tool_count_per_turn: 0,
    },
  });
  assert.ok(Math.abs(sig.score - 1.0) < 0.001, `expected score=1, got ${sig.score}`);
});

test("computeStruggle: features dict sums to score (sans success floor)", () => {
  const sig = computeStruggle({
    messages: [
      userText("hi"),
      assistantWithTools([toolUse("a")]),
      userWithResults([toolResult("a", "x", true)]),
    ],
    durationMs: 1000,
    success: true,
  });
  const sum =
    sig.features.tool_error_count +
    sig.features.tool_retry_count +
    sig.features.restart_markers +
    sig.features.clarification_loops +
    sig.features.tokens_per_progress +
    sig.features.tool_count_per_turn;
  assert.ok(
    Math.abs(sum - sig.score) < 0.0001,
    `feature sum ${sum} !== score ${sig.score}`,
  );
});

// ── countToolCalls (payload-shape-tolerant) ─────────────────────────────────

test("countToolCalls: zero on a turn with no tool work", () => {
  assert.equal(
    countToolCalls([userText("hi"), assistantText("hello")]),
    0,
  );
});

test("countToolCalls: counts Anthropic-style tool_use blocks", () => {
  // The canonical shape today — same path the existing extractors walk.
  const n = countToolCalls([
    assistantWithTools([toolUse("a"), toolUse("b"), toolUse("c")]),
  ]);
  assert.equal(n, 3);
});

test("countToolCalls: counts OpenAI-style top-level tool_calls array", () => {
  // The smoking-gun shape. If OC ever emits OpenAI-style payloads, the
  // existing tool_error_count / tool_retry_count detectors miss
  // everything — they walk only content[].type==="tool_use". This
  // extractor catches them.
  const msg = {
    role: "assistant",
    content: null,
    tool_calls: [
      { id: "call_1", function: { name: "search", arguments: "{}" } },
      { id: "call_2", function: { name: "fetch", arguments: "{}" } },
    ],
  };
  assert.equal(countToolCalls([msg]), 2);
});

test("countToolCalls: counts OpenAI legacy function_call wrapper as 1", () => {
  // Pre-tool_calls OpenAI API shape — single function_call object on
  // the message. Still surfaces occasionally in older payloads.
  const msg = {
    role: "assistant",
    content: null,
    function_call: { name: "search", arguments: "{}" },
  };
  assert.equal(countToolCalls([msg]), 1);
});

test("countToolCalls: sums all three shapes in a mixed-payload turn", () => {
  // Adversarial robustness: a single assistant message could carry
  // Anthropic blocks AND OpenAI tool_calls AND a function_call wrapper.
  // The extractor doesn't deduplicate — each shape contributes its
  // count. If OC ever emits a translation-layer payload that mirrors
  // calls across shapes, this would over-count; preferable to silently
  // missing the calls in the most likely path.
  const msg = {
    role: "assistant",
    content: [toolUse("a"), toolUse("b")],   // +2
    tool_calls: [                              // +3
      { id: "c1", function: { name: "x" } },
      { id: "c2", function: { name: "y" } },
      { id: "c3", function: { name: "z" } },
    ],
    function_call: { name: "legacy" },        // +1
  };
  assert.equal(countToolCalls([msg]), 6);
});

test("countToolCalls: ignores tool_use blocks on user/tool messages", () => {
  // tool_use is an assistant-message concept. A user or "tool"-role
  // message carrying a tool_use-shaped block is malformed and should
  // NOT count — otherwise we'd double-count tool_result tracking on
  // user messages.
  const msgs = [
    { role: "user", content: [{ type: "tool_use", name: "x" }] },
    { role: "tool", tool_call_id: "x", content: "result" },
  ];
  assert.equal(countToolCalls(msgs), 0);
});

test("countToolCalls: tolerates malformed messages without throwing", () => {
  const msgs = [null, undefined, "string-msg", { role: "assistant" }];
  assert.equal(countToolCalls(msgs), 0);
});

test("countToolCalls: aggregates across multiple assistant messages", () => {
  // Multi-step turn (rare in agent_end, but possible) — count adds
  // across messages, not just per-message.
  const msgs = [
    assistantWithTools([toolUse("a"), toolUse("b")]),
    userWithResults([toolResult("a", "x"), toolResult("b", "y")]),
    assistantWithTools([toolUse("c")]),
  ];
  assert.equal(countToolCalls(msgs), 3);
});

// ── tool_count_per_turn in computeStruggle ──────────────────────────────────

test("computeStruggle: tool_count_per_turn raw fires on healthy multi-tool turn", () => {
  // A normal subagent doing 4 reads — partial signal (4/8 = 0.5
  // normalized) but small contribution by itself (0.5 × 0.15 = 0.075).
  // This is the design: doesn't single-handedly trip the threshold.
  const sig = computeStruggle({
    messages: [
      assistantWithTools([toolUse("a"), toolUse("b"), toolUse("c"), toolUse("d")]),
    ],
    success: true,
  });
  assert.equal(sig.raw.tool_count_per_turn, 4);
  assert.ok(Math.abs(sig.features.tool_count_per_turn - 0.075) < 0.0001,
    `expected ~0.075 contribution from 4 calls, got ${sig.features.tool_count_per_turn}`);
});

test("computeStruggle: 8+ tool calls saturate the feature contribution", () => {
  // Saturation = 8. The single-feature contribution maxes at the full
  // weight (0.15). On a success=false turn, the 0.5 floor wins over
  // this single feature alone (Math.max, not addition) — that's the
  // existing floor semantics ("Conservative — doesn't replace, only
  // raises"). The feature shows its value only when combined with
  // others (the multi-feature test below proves that).
  const blocks = Array.from({ length: 8 }, (_, i) => toolUse(`t${i}`));
  const sig = computeStruggle({
    messages: [assistantWithTools(blocks)],
    success: false,
  });
  assert.equal(sig.raw.tool_count_per_turn, 8);
  assert.ok(Math.abs(sig.features.tool_count_per_turn - 0.15) < 0.0001,
    `expected full 0.15 weight at saturation, got ${sig.features.tool_count_per_turn}`);
  // Success=false floor wins (0.5 > 0.15 single-feature contribution).
  assert.equal(sig.score, 0.5);
});

test("computeStruggle: tool_count adds to other features above the floor", () => {
  // The realistic use case: tool_count combines with another feature
  // (here clarification_loops at saturation) to push past the floor.
  // 8 tool calls (0.15) + 1 clarification (0.15) = 0.30 raw.
  // Still below the 0.5 floor on success=false, BUT on success=true
  // there's no floor and the score is the raw 0.30 — that's signal
  // operators can see in the annotation JSONL even if the cascade
  // controller doesn't act on it. This proves the feature contributes
  // to the multi-signal aggregate that the audit layer will read.
  const blocks = Array.from({ length: 8 }, (_, i) => toolUse(`t${i}`));
  const sig = computeStruggle({
    messages: [
      { role: "user", content: "no, that's not what I asked for" },
      assistantWithTools(blocks),
    ],
    success: true,
  });
  assert.equal(sig.raw.tool_count_per_turn, 8);
  // The text triggers multiple clarification patterns; raw count is
  // the unbounded match count, saturation caps at 1 so the
  // contribution is still the full weight (0.15).
  assert.ok(sig.raw.clarification_loops >= 1, "clarification feature must fire");
  assert.ok(Math.abs(sig.features.clarification_loops - 0.15) < 0.0001,
    "clarification feature contribution should saturate to weight");
  // 0.15 (tool_count) + 0.15 (clarification) = 0.30. No floor since success=true.
  assert.ok(Math.abs(sig.score - 0.30) < 0.0001,
    `expected score=0.30 (tool_count + clarification), got ${sig.score}`);
});

test("computeStruggle: tool_count fires even when payload omits is_error", () => {
  // The whole reason this feature exists. The OpenAI-style payload here
  // would make every other tool feature return 0, but tool_count_per_turn
  // still surfaces struggle on volume alone.
  const msg = {
    role: "assistant",
    content: null,
    tool_calls: Array.from({ length: 8 }, (_, i) => ({
      id: `call_${i}`, function: { name: `t${i}`, arguments: "{}" },
    })),
  };
  const sig = computeStruggle({ messages: [msg], success: false });
  // Other tool features blind to this shape — they read content[]
  assert.equal(sig.raw.tool_error_count, 0);
  assert.equal(sig.raw.tool_retry_count, 0);
  // But tool_count sees all 8 — payload-tolerance pays off
  assert.equal(sig.raw.tool_count_per_turn, 8);
  assert.ok(sig.features.tool_count_per_turn > 0, "tool_count_per_turn must fire");
});

test("computeStruggle: null-score sentinel zeroes tool_count_per_turn", () => {
  // Companion to the existing test on the empty-signal shape — adding a
  // new feature requires _emptySignal to include the key so downstream
  // sum/display code doesn't NaN out.
  const sig = computeStruggle({ messages: undefined });
  assert.equal(sig.features.tool_count_per_turn, 0);
  assert.equal(sig.raw.tool_count_per_turn, 0);
});

test("DEFAULT_STRUGGLE_WEIGHTS: sums to 1.00 (sane initial allocation)", () => {
  // Sum was 0.85 across five features through 2026-06-06. PR that added
  // tool_count_per_turn intentionally lifted to 1.00: the prior cap
  // meant tier2_struggle_threshold=0.65 was unreachable for nine days
  // because three of five features never fired on real OC payloads.
  // The new sixth feature is payload-shape-tolerant and fires on raw
  // tool-call volume, so it widens reachability deliberately. See the
  // comment block on DEFAULT_STRUGGLE_WEIGHTS for the calibration math.
  const sum = Object.values(DEFAULT_STRUGGLE_WEIGHTS).reduce((a, b) => a + b, 0);
  assert.ok(Math.abs(sum - 1.0) < 0.0001, `expected default-weight sum ≈ 1.0, got ${sum}`);
});
