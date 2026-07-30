/**
 * Tests for the struggle-payload sampler — the one-shot diagnostic that
 * captures shape-only snapshots of ``event.messages`` on success=false
 * turns where the struggle detector hit the 0.5 floor.
 *
 * Goal (2026-06-06): differentiate two hypotheses about why three of five
 * struggle-detector features have never fired across 744 spans:
 *   (a) the live work genuinely doesn't struggle in the ways the features
 *       were designed to catch, OR
 *   (b) OC's agent_end.messages payload shape isn't what the detector
 *       walks (OpenAI-style vs Anthropic-style blocks; consolidated
 *       retries; collapsed tool_result content; etc.).
 *
 * The sampler captures a structural snapshot — text content, tool args,
 * and tool-result bodies are all reduced to length-only fields. These
 * tests pin both halves of that contract:
 *   1. The interest predicate fires for success=false ∩ score=0.5 only.
 *   2. The sanitizer surfaces detector-relevant flags (is_error, tool_use,
 *      tool_result, OpenAI-style tool_calls, etc.) AND never preserves
 *      raw text/value content.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.struggleSampler.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  STRUGGLE_SAMPLE_DAILY_CAP,
  _shouldCaptureStruggleSample,
  _sanitizeMessagesForShape,
} from "../dist/observer/TurnObserver.js";

// ── Interest predicate ─────────────────────────────────────────────────────

test("shouldCapture: fires on success=false + score=0.5 (the target population)", () => {
  assert.equal(
    _shouldCaptureStruggleSample(
      { success: false },
      { score: 0.5 },
    ),
    true,
  );
});

test("shouldCapture: skips healthy turns (success=true)", () => {
  assert.equal(
    _shouldCaptureStruggleSample(
      { success: true },
      { score: 0.5 },
    ),
    false,
  );
});

test("shouldCapture: skips turns where the floor was NOT the binding constraint", () => {
  // success=false but score > 0.5 → real features fired and pushed past
  // the floor. That's exactly the case we DON'T need to diagnose; the
  // detector saw something.
  assert.equal(
    _shouldCaptureStruggleSample(
      { success: false },
      { score: 0.62 },
    ),
    false,
  );
});

test("shouldCapture: skips turns where success is absent (OC payload drift)", () => {
  // event.success undefined → can't tell if OC marked failure. Skip
  // rather than sample, since the floor wouldn't have applied either.
  assert.equal(
    _shouldCaptureStruggleSample(
      {},
      { score: 0.5 },
    ),
    false,
  );
});

test("shouldCapture: skips null signal (payload drift before signal compute)", () => {
  assert.equal(
    _shouldCaptureStruggleSample({ success: false }, null),
    false,
  );
});

test("shouldCapture: skips score=null (detector returned no measurement)", () => {
  assert.equal(
    _shouldCaptureStruggleSample(
      { success: false },
      { score: null },
    ),
    false,
  );
});

test("daily cap is sized to bound disk usage but allow meaningful sampling", () => {
  // Pin the cap so a future "let's go higher!" PR has to update both the
  // constant and this test — forces the reviewer to think about disk
  // impact on the noisiest bot (hundreds of turns/day on the busiest
  // gateway).
  assert.equal(STRUGGLE_SAMPLE_DAILY_CAP, 20);
});

// ── Sanitizer: shape preservation ───────────────────────────────────────

test("sanitize: returns notArray=true when messages isn't an array", () => {
  const r = _sanitizeMessagesForShape(undefined);
  assert.equal(r.notArray, true);
  assert.equal(r.raw_type, "undefined");
  assert.equal(r.totalMessages, 0);
  assert.equal(r.sample.length, 0);
});

test("sanitize: walks an Anthropic-style assistant message with tool_use", () => {
  const messages = [
    {
      role: "assistant",
      content: [
        { type: "text", text: "I'll look that up." },
        {
          type: "tool_use",
          id: "toolu_abc",
          name: "search",
          input: { query: "foo", limit: 10 },
        },
      ],
    },
  ];
  const r = _sanitizeMessagesForShape(messages);
  assert.equal(r.totalMessages, 1);
  assert.equal(r.truncated, false);
  assert.equal(r.sample.length, 1);
  const msg = r.sample[0];
  assert.equal(msg.role, "assistant");
  assert.equal(msg.contentType, "array");
  assert.equal(msg.blockCount, 2);
  const blocks = msg.blocks;
  assert.equal(blocks[0].type, "text");
  assert.equal(blocks[0].text_len, "I'll look that up.".length);
  // NEVER preserve the actual text content.
  assert.equal("text" in blocks[0], false);
  assert.equal(blocks[1].type, "tool_use");
  assert.equal(blocks[1].has_name, true);
  assert.equal(blocks[1].has_id, true);
  assert.deepEqual(blocks[1].input_keys, ["query", "limit"]);
  // NEVER preserve the input VALUES.
  assert.equal("input" in blocks[1], false);
  // tool_use name string itself must not leak through.
  assert.equal("name" in blocks[1], false);
});

test("sanitize: surfaces tool_result.is_error — the key detector signal", () => {
  // tool_error_count feature reads ``is_error: true`` on tool_result
  // blocks. If OC's payload omits this field, the feature fires 0 even
  // when the underlying call errored. The sample must surface the
  // is_error flag verbatim so audit can confirm.
  const messages = [
    {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "toolu_abc",
          is_error: true,
          content: "command not found: doesntexist",
        },
      ],
    },
  ];
  const r = _sanitizeMessagesForShape(messages);
  const blk = r.sample[0].blocks[0];
  assert.equal(blk.type, "tool_result");
  assert.equal(blk.is_error, true);
  assert.equal(blk.has_tool_use_id, true);
  assert.equal(blk.result_content_len, "command not found: doesntexist".length);
  // The error message itself MUST NOT leak.
  assert.equal("content" in blk, false);
});

test("sanitize: surfaces nested tool_result content as inner block types", () => {
  const messages = [
    {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "t1",
          content: [
            { type: "text", text: "ok" },
            { type: "image", source: { type: "base64", data: "..." } },
          ],
        },
      ],
    },
  ];
  const r = _sanitizeMessagesForShape(messages);
  const blk = r.sample[0].blocks[0];
  assert.equal(blk.result_content_blocks, 2);
  assert.deepEqual(blk.result_block_types, ["text", "image"]);
});

test("sanitize: catches OpenAI-style tool_calls (the smoking-gun shape)", () => {
  // If OC ever sends an OpenAI-shaped payload (function_call /
  // tool_calls array on the message object directly), the Anthropic-
  // style block walker misses it entirely. The sampler surfaces the
  // counts so an audit run can identify this as the cause.
  const messages = [
    {
      role: "assistant",
      content: null,
      tool_calls: [
        { id: "call_1", function: { name: "search", arguments: "{}" } },
        { id: "call_2", function: { name: "fetch", arguments: "{}" } },
      ],
    },
    {
      role: "tool",
      tool_call_id: "call_1",
      name: "search",
      content: "results...",
    },
  ];
  const r = _sanitizeMessagesForShape(messages);
  const m0 = r.sample[0];
  assert.equal(m0.contentType, "null");
  assert.equal(m0.top_tool_calls_count, 2);
  const m1 = r.sample[1];
  assert.equal(m1.role, "tool");
  assert.equal(m1.top_has_tool_call_id, true);
  assert.equal(m1.top_has_name, true);
  assert.equal(m1.contentType, "string");
  assert.equal(m1.text_len, "results...".length);
});

test("sanitize: truncates to the last 50 messages on long histories", () => {
  const messages = Array.from({ length: 120 }, (_, i) => ({
    role: i % 2 === 0 ? "user" : "assistant",
    content: [{ type: "text", text: `m${i}` }],
  }));
  const r = _sanitizeMessagesForShape(messages);
  assert.equal(r.totalMessages, 120);
  assert.equal(r.truncated, true);
  assert.equal(r.sample.length, 50);
  // The sample is the TAIL — first preserved idx should be 70.
  assert.equal(r.sample[0].idx, 70);
  assert.equal(r.sample[49].idx, 119);
});

test("sanitize: caps blocks-per-message at 30 with a truncation flag", () => {
  const blocks = Array.from({ length: 45 }, (_, i) => ({
    type: "text",
    text: `b${i}`,
  }));
  const messages = [{ role: "assistant", content: blocks }];
  const r = _sanitizeMessagesForShape(messages);
  const msg = r.sample[0];
  assert.equal(msg.blockCount, 45);
  assert.equal(msg.blocks.length, 30);
  assert.equal(msg.blocksTruncated, true);
});

test("sanitize: tolerates malformed blocks without throwing", () => {
  const messages = [
    { role: "assistant", content: [null, undefined, "string-block", { type: 7 }] },
  ];
  const r = _sanitizeMessagesForShape(messages);
  const msg = r.sample[0];
  assert.equal(msg.blocks.length, 4);
  // type !== "string" → null
  for (const b of msg.blocks) {
    assert.equal(b.type, null);
  }
});

test("sanitize: never preserves raw content even when block is unknown shape", () => {
  // Adversarial: every block carries SOMETHING that looks like content.
  // The sanitizer must not pass any of it through. This test is the
  // privacy contract — if a future change accidentally widens the
  // shape, this test catches it.
  const messages = [
    {
      role: "user",
      content: [
        { type: "weird", text: "SECRET-TEXT-1", input: { pw: "SECRET-2" } },
        { type: "tool_result", content: "SECRET-RESULT-3" },
      ],
    },
  ];
  const r = _sanitizeMessagesForShape(messages);
  const serialized = JSON.stringify(r);
  assert.equal(serialized.includes("SECRET-TEXT-1"), false);
  assert.equal(serialized.includes("SECRET-2"), false);
  assert.equal(serialized.includes("SECRET-RESULT-3"), false);
});
