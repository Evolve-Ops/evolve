/**
 * Regression tests for the keyword fallback's classification of
 * SUBSTANTIVE user requests — the 2026-07-31 fleet incident.
 *
 * With OC 2026.7.1-2 rejecting the classifier's subagent model pin, the
 * LLM tier classifier died on every invocation and keyword
 * classification became the fleet's routing input. Two properties must
 * hold for that fallback to be safe:
 *
 *   1. A substantive request (the incident's literal message) classifies
 *      productive on the user's own words.
 *   2. Error vocabulary in the ASSISTANT's reply ("failed to",
 *      "error:", …) must not flip a productive user request into
 *      maintenance/tie — pre-fix it did, and the verdict then stuck on
 *      the session via setSessionTypeIfMoreSpecific, downgrading every
 *      subsequent turn to the fast rung (haiku), where a real document
 *      task died with stopReason=length.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/tierClassifier.substantiveRequest.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { classifyTierByKeywords } from "../dist/observer/TierClassifier.js";

// The literal user message from the 2026-07-31 incident (affected
// member bot, telegram, 15:43 local).
const INCIDENT_MESSAGE =
  "I will work on a Brave API. In the meantime, can you create a " +
  "consolidated career document that includes all 4 regions for Slylar?";

test("incident message classifies productive on user text alone", () => {
  const r = classifyTierByKeywords(INCIDENT_MESSAGE, "", "s1");
  assert.equal(r.class, "productive");
});

test("assistant error vocabulary must not flip a productive request (incident tie)", () => {
  // The incident session's combined text scored a tie (ambiguous 0.4):
  // the assistant's reply about the interrupted turn carried maintenance
  // vocabulary. The user's request is unambiguous — productive must win.
  const assistantReply =
    "The previous attempt failed to complete — error: the gateway " +
    "interrupted the turn. Let me retry and fix the formatting.";
  const r = classifyTierByKeywords(INCIDENT_MESSAGE, assistantReply, "s2");
  assert.equal(r.class, "productive");
});

test("assistant error vocabulary must not flip a productive request (maintenance-dominant combined)", () => {
  // Heavier pollution: combined text is maintenance-DOMINANT, not just a
  // tie. User text still strictly productive → productive.
  const assistantReply =
    "Traceback: exception — permission denied. failed to write. " +
    "error: exit code 1. Run sudo chmod to fix the permission error.";
  const r = classifyTierByKeywords(INCIDENT_MESSAGE, assistantReply, "s3");
  assert.equal(r.class, "productive");
});

test("a user pasting errors still classifies maintenance", () => {
  // User-text priority must NOT suppress genuine maintenance sessions:
  // when the USER's own words carry the maintenance signal, the class
  // stays maintenance.
  const r = classifyTierByKeywords(
    "the gateway is broken again — permission denied and exit code 1 when I restart gateway",
    "",
    "s4",
  );
  assert.equal(r.class, "maintenance");
});

test("empty user message with maintenance-flavored assistant text stays maintenance", () => {
  // Auto sessions (heartbeats etc.) have empty user text; the assistant
  // text is then the only signal and must keep classifying.
  const r = classifyTierByKeywords(
    "",
    "watchdog fired: gateway restart needed, permission error in launchd plist",
    "s5",
  );
  assert.equal(r.class, "maintenance");
});
