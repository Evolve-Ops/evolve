/**
 * Tests for the structured judge tail (D-OH4 —
 * internal/decision-evolve-overhead-2026-09-07.md).
 *
 * The contract under test:
 *   - a well-formed tail round-trips: the verdict is read and the tail
 *     (with its separating whitespace) is removed from the reply;
 *   - a MALFORMED tail is left VISIBLE and yields no verdict. Ugly beats
 *     lossy: a stray marker is a bug someone can see and report, while a
 *     heuristic strip that guesses at the boundary deletes a reply the
 *     user was waiting for and nobody finds out;
 *   - nothing throws on any input shape.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/structuredTail.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  parseStructuredTail,
  stripStructuredTail,
  hasTailMarker,
  buildJudgeTailRequest,
  TAIL_OPEN,
  TAIL_CLOSE,
} from "../dist/observer/structuredTail.js";

const REPLY = "Sure — I moved the meeting to Thursday and let the room know.";

function withTail(verdict, reply = REPLY) {
  return `${reply}\n${TAIL_OPEN} ${verdict} ${TAIL_CLOSE}`;
}

// ── Round trip ──────────────────────────────────────────────────────────────

for (const verdict of ["OK", "STRUGGLING", "AMBIGUOUS"]) {
  test(`tail: ${verdict} round-trips — verdict read, tail stripped`, () => {
    const parsed = parseStructuredTail(withTail(verdict));
    assert.equal(parsed.verdict, verdict);
    assert.equal(parsed.stripped, REPLY);
    assert.equal(parsed.found, true);
    assert.equal(parsed.malformed, false);
  });
}

test("tail: a lowercase verdict is accepted and normalised", () => {
  const parsed = parseStructuredTail(withTail("struggling"));
  assert.equal(parsed.verdict, "STRUGGLING");
  assert.equal(parsed.stripped, REPLY);
});

test("tail: extra whitespace inside the markers is tolerated", () => {
  const parsed = parseStructuredTail(`${REPLY}\n\n${TAIL_OPEN}\n  OK  \n${TAIL_CLOSE}`);
  assert.equal(parsed.verdict, "OK");
  assert.equal(parsed.stripped, REPLY);
});

test("tail: a multi-paragraph reply keeps its internal blank lines", () => {
  const body = "First paragraph.\n\nSecond paragraph, with detail.";
  const parsed = parseStructuredTail(withTail("OK", body));
  assert.equal(parsed.stripped, body);
});

test("tail: the LAST marker wins, so a reply discussing the marker still parses", () => {
  const body = `The plugin appends a line starting with ${TAIL_OPEN} to your turn.`;
  const parsed = parseStructuredTail(`${body}\n${TAIL_OPEN} OK ${TAIL_CLOSE}`);
  assert.equal(parsed.verdict, "OK");
  assert.equal(parsed.stripped, body);
});

// ── Nothing there ───────────────────────────────────────────────────────────

test("tail: a reply with no tail is returned untouched, no verdict", () => {
  const parsed = parseStructuredTail(REPLY);
  assert.equal(parsed.verdict, null);
  assert.equal(parsed.stripped, REPLY);
  assert.equal(parsed.found, false);
  assert.equal(parsed.malformed, false);
});

test("tail: non-string input is safe", () => {
  for (const input of [null, undefined, 42, {}, []]) {
    const parsed = parseStructuredTail(input);
    assert.equal(parsed.verdict, null);
    assert.equal(parsed.stripped, "");
  }
});

// ── Malformed: left visible ─────────────────────────────────────────────────

test("tail: an opener with no closer is LEFT VISIBLE and yields no verdict", () => {
  const text = `${REPLY}\n${TAIL_OPEN} OK`;
  const parsed = parseStructuredTail(text);
  assert.equal(parsed.verdict, null);
  assert.equal(parsed.stripped, text, "we do not guess where an unterminated tail ends");
  assert.equal(parsed.malformed, true);
  assert.equal(parsed.found, false);
});

test("tail: a bare closer is LEFT VISIBLE and flagged malformed", () => {
  const text = `${REPLY}\n${TAIL_CLOSE}`;
  const parsed = parseStructuredTail(text);
  assert.equal(parsed.verdict, null);
  assert.equal(parsed.stripped, text);
  assert.equal(parsed.malformed, true);
});

test("tail: an unrecognised verdict is LEFT VISIBLE and yields no verdict", () => {
  const text = withTail("PROBABLY FINE?");
  const parsed = parseStructuredTail(text);
  assert.equal(parsed.verdict, null);
  assert.equal(parsed.stripped, text);
  assert.equal(parsed.malformed, true);
});

test("tail: an empty verdict body is malformed, not silently OK", () => {
  const text = `${REPLY}\n${TAIL_OPEN} ${TAIL_CLOSE}`;
  const parsed = parseStructuredTail(text);
  assert.equal(parsed.verdict, null);
  assert.equal(parsed.malformed, true);
  assert.equal(parsed.stripped, text);
});

test("tail: a malformed tail never removes any of the user's reply", () => {
  // The property that matters most, stated directly.
  for (const bad of [
    `${REPLY}\n${TAIL_OPEN} nope`,
    `${REPLY}\n${TAIL_CLOSE}`,
    `${REPLY}\n${TAIL_OPEN} maybe ${TAIL_CLOSE}`,
  ]) {
    assert.ok(
      parseStructuredTail(bad).stripped.includes(REPLY),
      "the reply text must survive every malformed shape",
    );
  }
});

// ── Helpers ─────────────────────────────────────────────────────────────────

test("stripStructuredTail: removes a good tail, leaves a bad one", () => {
  assert.equal(stripStructuredTail(withTail("OK")), REPLY);
  const bad = `${REPLY}\n${TAIL_OPEN} OK`;
  assert.equal(stripStructuredTail(bad), bad);
});

test("hasTailMarker: cheap pre-check catches both markers", () => {
  assert.equal(hasTailMarker(REPLY), false);
  assert.equal(hasTailMarker(withTail("OK")), true);
  assert.equal(hasTailMarker(`${REPLY} ${TAIL_CLOSE}`), true);
  assert.equal(hasTailMarker(null), false);
});

test("buildJudgeTailRequest: names both markers and every valid verdict", () => {
  const req = buildJudgeTailRequest();
  assert.ok(req.includes(TAIL_OPEN));
  assert.ok(req.includes(TAIL_CLOSE));
  for (const v of ["OK", "STRUGGLING", "AMBIGUOUS"]) assert.ok(req.includes(v));
  // The request rides every sampled turn's prompt, so its own size is
  // part of the overhead this decision removes. Keep it small.
  assert.ok(req.length < 600, `tail request is ${req.length} chars — keep it short`);
});
