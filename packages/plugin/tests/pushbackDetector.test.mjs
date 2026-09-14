/**
 * Tests for PushbackDetector — pure-function user-pushback signal that
 * replaces the keyword-substring `correction_detected` heuristic.
 *
 * Spec: internal/spec-user-pushback-signal-2026-05-30.md.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/pushbackDetector.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  computePushback,
  countClarificationHits,
  tokenizeForJaccard,
  jaccardSimilarity,
  isShortFollowup,
  DEFAULT_PUSHBACK_WEIGHTS,
} from "../dist/observer/PushbackDetector.js";

// A reasonably substantive prior assistant reply (≥100 chars), so the
// short_followup gate can fire when the current user text is bare.
const SUBSTANTIVE_ASSISTANT_REPLY =
  "Here's a summary of yesterday's meeting: we agreed on the new launch date, " +
  "assigned action items to each team, and scheduled a follow-up review for next Tuesday afternoon.";

// ── countClarificationHits ───────────────────────────────────────────────────

test("countClarificationHits: zero on empty", () => {
  assert.equal(countClarificationHits(""), 0);
  assert.equal(countClarificationHits("   "), 0);
});

test("countClarificationHits: catches each pattern at least once", () => {
  assert.ok(countClarificationHits("no, that's not what i wanted") >= 1);
  assert.ok(countClarificationHits("i meant the other one") >= 1);
  assert.ok(countClarificationHits("you misunderstood the question") >= 1);
  assert.ok(countClarificationHits("not quite, try the second option") >= 1);
  assert.ok(countClarificationHits("again, but in french this time") >= 1);
});

test("countClarificationHits: does NOT fire on innocuous code prose", () => {
  // The research-bot/coding-bot false-positive case the spec exists to fix.
  // The old correction_detected substring matcher fires on "incorrect"
  // and "that's wrong" appearing in code-review discussion. The new
  // clarification regex requires the addressee-of-pushback shape.
  assert.equal(
    countClarificationHits("The function returns an incorrect type when n is negative."),
    0,
  );
  assert.equal(
    countClarificationHits("That's wrong because the test asserts strict equality."),
    0,
  );
});

// ── tokenizeForJaccard ───────────────────────────────────────────────────────

test("tokenizeForJaccard: lowercases + strips punctuation", () => {
  const set = tokenizeForJaccard("Hello, World! It's 5pm.");
  assert.ok(set.has("hello"));
  assert.ok(set.has("world"));
  assert.ok(set.has("it's"));   // apostrophe preserved
  assert.ok(set.has("5pm"));
});

test("tokenizeForJaccard: removes stop words", () => {
  const set = tokenizeForJaccard("the quick brown fox is over the lazy dog");
  assert.ok(!set.has("the"));
  assert.ok(!set.has("is"));
  assert.ok(set.has("quick"));
  assert.ok(set.has("fox"));
});

test("tokenizeForJaccard: empty on empty input", () => {
  assert.equal(tokenizeForJaccard("").size, 0);
  assert.equal(tokenizeForJaccard("the a is").size, 0);  // all stop words
});

// ── jaccardSimilarity ────────────────────────────────────────────────────────

test("jaccardSimilarity: 1.0 on identical content-word sets", () => {
  const a = tokenizeForJaccard("summarize the meeting notes");
  const b = tokenizeForJaccard("summarize the meeting notes");
  assert.equal(jaccardSimilarity(a, b), 1);
});

test("jaccardSimilarity: 0 on disjoint sets", () => {
  const a = tokenizeForJaccard("calendar event tomorrow");
  const b = tokenizeForJaccard("weather forecast wednesday");
  assert.equal(jaccardSimilarity(a, b), 0);
});

test("jaccardSimilarity: ≥0.4 on a real rephrase", () => {
  // Same user asking essentially the same question with different
  // wording — this is exactly the case the signal exists to catch.
  const a = tokenizeForJaccard("can you summarize yesterday's meeting notes");
  const b = tokenizeForJaccard("please summarize the notes from yesterday's meeting");
  const j = jaccardSimilarity(a, b);
  assert.ok(j >= 0.4, `expected ≥0.4 on rephrase, got ${j}`);
});

test("jaccardSimilarity: 0 if either side empty", () => {
  assert.equal(jaccardSimilarity(new Set(), new Set(["a"])), 0);
  assert.equal(jaccardSimilarity(new Set(["a"]), new Set()), 0);
});

// ── isShortFollowup ──────────────────────────────────────────────────────────

test("isShortFollowup: bare 'no' after a substantive reply fires", () => {
  assert.equal(isShortFollowup("no", SUBSTANTIVE_ASSISTANT_REPLY), true);
  assert.equal(isShortFollowup("hmm", SUBSTANTIVE_ASSISTANT_REPLY), true);
  assert.equal(isShortFollowup("try again", SUBSTANTIVE_ASSISTANT_REPLY), true);
});

test("isShortFollowup: questions don't fire (presumed new ask)", () => {
  assert.equal(isShortFollowup("when?", SUBSTANTIVE_ASSISTANT_REPLY), false);
});

test("isShortFollowup: long messages don't fire", () => {
  const long = "Actually, on reflection, I'd like you to redo this with a different tone.";
  assert.equal(isShortFollowup(long, SUBSTANTIVE_ASSISTANT_REPLY), false);
});

test("isShortFollowup: short prior assistant reply doesn't fire", () => {
  assert.equal(isShortFollowup("no", "ok"), false);
});

// ── computePushback: payload drift ──────────────────────────────────────────

test("computePushback: no prior user turn → null + 'no_prior_turn'", () => {
  const sig = computePushback({
    currentUserText: "no, that's not what i meant",
    previousUserText: null,
    previousAssistantText: null,
  });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "no_prior_turn");
  // Clarification raw still computed for debug visibility — multiple
  // patterns in CLARIFICATION_PATTERNS can match the same message
  // ("no, that's", "that's not what i", "i meant" each fire once here).
  assert.ok(sig.raw.clarification_loops >= 1, `expected ≥1 clarification hits, got ${sig.raw.clarification_loops}`);
});

test("computePushback: DNT off → null + 'dnt'", () => {
  const sig = computePushback({
    currentUserText: "no, that's wrong",
    previousUserText: "summarize the docs",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
    dntEnabled: false,
  });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "dnt");
  // No measurement performed when DNT is off.
  assert.equal(sig.raw.clarification_loops, 0);
  assert.equal(sig.raw.jaccard_similarity, 0);
});

test("computePushback: empty both messages → 'empty_messages' via no_prior_turn check", () => {
  // The no_prior_turn check fires first (priorUser is empty after trim).
  // The empty_messages case is covered when both are empty AND we somehow
  // get past the prior-turn guard, which the current code path doesn't
  // hit — defense in depth, but no_prior_turn wins in practice.
  const sig = computePushback({
    currentUserText: "",
    previousUserText: "",
    previousAssistantText: "",
  });
  assert.equal(sig.score, null);
  assert.equal(sig.payload_drift, "no_prior_turn");
});

// ── computePushback: feature firing ─────────────────────────────────────────

test("computePushback: clean turn with no overlap → low score", () => {
  const sig = computePushback({
    currentUserText: "what's the weather tomorrow",
    previousUserText: "summarize my meeting notes please",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
  });
  assert.equal(sig.payload_drift, null);
  assert.ok(sig.score !== null && sig.score < 0.2, `expected low, got ${sig.score}`);
});

test("computePushback: clarification alone fires modestly", () => {
  // Clarification regex hits, but no rephrase overlap and not a bare
  // follow-up. Score = 1.0 * 0.35 = 0.35. NOT enough to fire the chip
  // (which uses 0.5 threshold) on its own.
  const sig = computePushback({
    currentUserText: "you misunderstood — i wanted something completely different next time around",
    previousUserText: "summarize my meeting notes please",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
  });
  assert.equal(sig.payload_drift, null);
  assert.ok(sig.score !== null);
  assert.ok(sig.score >= 0.30 && sig.score < 0.5, `expected 0.30..0.5, got ${sig.score}`);
});

test("computePushback: rephrase alone fires modestly", () => {
  // High Jaccard but no clarification phrase and not bare. Score ≈ 0.40.
  // Also under threshold — by design, the signal requires more than one
  // signal class.
  const sig = computePushback({
    currentUserText: "can you summarize the meeting notes from yesterday afternoon",
    previousUserText: "summarize my meeting notes from yesterday please",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
  });
  assert.equal(sig.payload_drift, null);
  assert.ok(sig.score !== null);
  assert.ok(sig.raw.jaccard_similarity >= 0.4, `Jaccard too low: ${sig.raw.jaccard_similarity}`);
  assert.ok(sig.score >= 0.3 && sig.score < 0.5, `expected 0.3..0.5, got ${sig.score}`);
});

test("computePushback: bare follow-up after substantive reply fires modestly", () => {
  const sig = computePushback({
    currentUserText: "hmm",
    previousUserText: "summarize my meeting notes",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
  });
  assert.equal(sig.payload_drift, null);
  assert.ok(sig.score !== null);
  // Only short_followup fires (clarification doesn't catch "hmm", Jaccard
  // low). Contribution = 1 * 0.25 = 0.25.
  assert.ok(sig.score >= 0.20 && sig.score < 0.5, `expected ~0.25, got ${sig.score}`);
});

test("computePushback: clarification + rephrase + short → fires above chip threshold", () => {
  // The shape that should reliably trip the chip:
  //   - Clarification regex hits ("no, i meant" matches two patterns).
  //   - Short follow-up (<25 chars, no question mark, prior assistant
  //     reply substantive).
  //   - Some rephrase overlap with the prior user turn ("notes").
  // With these three feature classes co-firing, the composite clears 0.5.
  const sig = computePushback({
    currentUserText: "no, i meant the notes",
    previousUserText: "give me the notes please",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
  });
  assert.equal(sig.payload_drift, null);
  assert.ok(sig.raw.clarification_loops >= 1, `clarification regex didn't fire`);
  assert.ok(sig.score !== null && sig.score >= 0.5, `expected ≥0.5, got ${sig.score}`);
});

// ── computePushback: false-positive resistance ──────────────────────────────

test("computePushback: code review chatter with no rephrase does NOT fire chip", () => {
  // The research-bot/coding-bot false-positive case. "that's wrong" appears in
  // code review context. Previous turn was about a different topic so
  // Jaccard is low. No short follow-up. Below the chip threshold even
  // though the legacy correction_detected substring matcher would fire.
  const sig = computePushback({
    currentUserText:
      "Looking at the failing test, that's wrong because the assertion expects strict equality but we're comparing object references.",
    previousUserText: "Can you run the test suite and report what fails?",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
  });
  assert.equal(sig.payload_drift, null);
  assert.ok(sig.score !== null && sig.score < 0.5, `expected <0.5 chip threshold, got ${sig.score}`);
});

test("computePushback: coincidental word overlap without clarification does NOT fire chip", () => {
  // Two messages that share common words but aren't a rephrase.
  // Without a clarification phrase or short follow-up, rephrase alone
  // (≤0.40 contribution) doesn't reach the chip threshold.
  const sig = computePushback({
    currentUserText: "tomorrow's meeting agenda needs the budget items added",
    previousUserText: "tomorrow's meeting room is changing to the larger conference space",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
  });
  assert.equal(sig.payload_drift, null);
  assert.ok(sig.score !== null && sig.score < 0.5, `expected <0.5, got ${sig.score}`);
});

// ── computePushback: features sum correctly ─────────────────────────────────

test("computePushback: contributions sum to score (within float epsilon)", () => {
  const sig = computePushback({
    currentUserText: "no, the meeting notes",
    previousUserText: "the meeting notes please",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
  });
  assert.ok(sig.score !== null);
  const sum =
    sig.features.clarification_regex +
    sig.features.rephrase_similarity +
    sig.features.short_followup;
  assert.ok(Math.abs(sig.score - Math.min(sum, 1)) < 1e-9);
});

test("computePushback: weights override changes score", () => {
  // Zero out all weights → score is 0 (not null, since payload_drift=null).
  const sig = computePushback({
    currentUserText: "no, the meeting notes",
    previousUserText: "the meeting notes please",
    previousAssistantText: SUBSTANTIVE_ASSISTANT_REPLY,
    weights: { clarification_regex: 0, rephrase_similarity: 0, short_followup: 0 },
  });
  assert.equal(sig.payload_drift, null);
  assert.equal(sig.score, 0);
});

test("computePushback: defaults exposed sum to 1.0", () => {
  // Sanity check on the published weight set: they sum to 1 so the
  // maximum theoretical score is 1, and the 0.5 chip threshold sits
  // squarely at "more than half the signal classes fired."
  const total =
    DEFAULT_PUSHBACK_WEIGHTS.clarification_regex +
    DEFAULT_PUSHBACK_WEIGHTS.rephrase_similarity +
    DEFAULT_PUSHBACK_WEIGHTS.short_followup;
  assert.ok(Math.abs(total - 1.0) < 1e-9, `weights sum to ${total}`);
});
