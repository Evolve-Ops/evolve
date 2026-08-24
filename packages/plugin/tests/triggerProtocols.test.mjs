/**
 * Tests for triggerProtocols — Phase 2.3 of the agent-freelance-bypass spec
 * (internal/spec-agent-freelance-bypass-phase2-2026-06-06.md).
 *
 * Covers:
 *   - atlas_research protocol: ANSWERED (base64 decode), RATE_LIMITED,
 *     BUDGET_EXCEEDED, REFUSED, FAILED, empty stdout, multi-line tolerance
 *   - atlas_capture protocol: ARCHIVED (silent), DEDUP (silent),
 *     REFUSED, RATE_LIMITED, FAILED, empty stdout
 *   - Known-protocol gate (matches the Python validator's _KNOWN_STDOUT_PROTOCOLS)
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/triggerProtocols.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  parseProtocol,
  isKnownProtocol,
  KNOWN_PROTOCOLS,
} from "../dist/observer/triggerProtocols.js";


// ── Known-protocol gate ──────────────────────────────────────────────────────

test("KNOWN_PROTOCOLS matches the Python validator's stdout_protocol enum", () => {
  // Source of truth: docs/schemas/manifest-v7-spec.schema.json
  //   ::properties.event_triggers.items.properties.invocation
  //   .properties.stdout_protocol.enum
  // AND
  //   packages/admin/evolve_admin/applications/bot_guidance_freelance_validator.py
  //   ::_KNOWN_STDOUT_PROTOCOLS
  //
  // Three places, one truth. This test pins TS ↔ JSON-schema; the Python
  // test pins Python ↔ JSON-schema; transitively all three stay aligned.
  assert.deepEqual([...KNOWN_PROTOCOLS].sort(), ["atlas_capture", "atlas_research", "raw_text"]);
});

test("isKnownProtocol accepts registered names", () => {
  assert.equal(isKnownProtocol("atlas_research"), true);
  assert.equal(isKnownProtocol("atlas_capture"), true);
  assert.equal(isKnownProtocol("raw_text"), true);
});

test("isKnownProtocol rejects unknown / non-string", () => {
  assert.equal(isKnownProtocol("myapp_custom"), false);
  assert.equal(isKnownProtocol(""), false);
  assert.equal(isKnownProtocol(null), false);
  assert.equal(isKnownProtocol(undefined), false);
  assert.equal(isKnownProtocol(42), false);
});


// ── atlas_research protocol ──────────────────────────────────────────────────

function b64(text) {
  return Buffer.from(text, "utf8").toString("base64");
}

test("atlas_research: ANSWERED decodes base64 reply", () => {
  const reply = "Per Claude's release notes, model X is faster.";
  const stdout = `RESEARCH_ANSWERED:${b64(reply)}\n`;
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.outcome, "answered");
  assert.equal(res.text, reply);
});

test("atlas_research: ANSWERED with invalid base64 → failed", () => {
  // Base64 decode in Node is lenient; we mostly want to check empty-decode → failed.
  const stdout = "RESEARCH_ANSWERED:\n";
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.outcome, "failed");
  assert.equal(res.text, null);
});

test("atlas_research: RATE_LIMITED passes through verbatim", () => {
  const stdout = "RESEARCH_RATE_LIMITED:Slow down — 3/hr cap hit.\n";
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.outcome, "rate_limited");
  assert.equal(res.text, "Slow down — 3/hr cap hit.");
});

test("atlas_research: BUDGET_EXCEEDED passes through verbatim", () => {
  const stdout = "RESEARCH_BUDGET_EXCEEDED:Pod budget for today is gone.\n";
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.outcome, "budget_exceeded");
  assert.equal(res.text, "Pod budget for today is gone.");
});

test("atlas_research: REFUSED passes through verbatim", () => {
  const stdout = "RESEARCH_REFUSED:I only answer questions about Claude / OC.\n";
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.outcome, "refused");
  assert.equal(res.text, "I only answer questions about Claude / OC.");
});

test("atlas_research: FAILED returns null text + failed outcome", () => {
  const stdout = "RESEARCH_FAILED\n";
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.outcome, "failed");
  assert.equal(res.text, null);
});

test("atlas_research: empty stdout → silent", () => {
  const res = parseProtocol("atlas_research", "");
  assert.equal(res.outcome, "silent");
  assert.equal(res.text, null);
});

test("atlas_research: stdout with only whitespace → silent", () => {
  const res = parseProtocol("atlas_research", "\n\n   \n");
  assert.equal(res.outcome, "silent");
});

test("atlas_research: ignores noise before the protocol line", () => {
  // Scripts may emit debug noise to stdout before the protocol line;
  // parser tolerates this.
  const stdout = "[debug] starting scope check\n[debug] cost=$0.01\nRESEARCH_REFUSED:Off-topic.\n";
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.outcome, "refused");
  assert.equal(res.text, "Off-topic.");
});

test("atlas_research: first recognised protocol line wins", () => {
  // If two protocol lines appear, the first one is authoritative
  // (scripts emit exactly one in normal operation).
  const stdout = "RESEARCH_REFUSED:no\nRESEARCH_ANSWERED:" + b64("yes") + "\n";
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.outcome, "refused");
});

test("atlas_research: trims trailing whitespace from text", () => {
  const stdout = "RESEARCH_REFUSED:Off-topic.   \n";
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.text, "Off-topic.");
});

test("atlas_research: CRLF line endings handled", () => {
  const stdout = "RESEARCH_REFUSED:Off-topic.\r\n";
  const res = parseProtocol("atlas_research", stdout);
  assert.equal(res.outcome, "refused");
});


// ── atlas_capture protocol ───────────────────────────────────────────────────

test("atlas_capture: ARCHIVED is silent success", () => {
  const stdout = "CAPTURE_ARCHIVED:bucket_2026_06\n";
  const res = parseProtocol("atlas_capture", stdout);
  assert.equal(res.outcome, "silent");
  assert.equal(res.text, null);
});

test("atlas_capture: DEDUP is silent success", () => {
  const stdout = "CAPTURE_DEDUP:bucket_2026_06\n";
  const res = parseProtocol("atlas_capture", stdout);
  assert.equal(res.outcome, "silent");
  assert.equal(res.text, null);
});

test("atlas_capture: REFUSED passes through verbatim", () => {
  const stdout = "CAPTURE_REFUSED:Opted out — not capturing.\n";
  const res = parseProtocol("atlas_capture", stdout);
  assert.equal(res.outcome, "refused");
  assert.equal(res.text, "Opted out — not capturing.");
});

test("atlas_capture: RATE_LIMITED passes through verbatim", () => {
  const stdout = "CAPTURE_RATE_LIMITED:Capture limit hit, retry tomorrow.\n";
  const res = parseProtocol("atlas_capture", stdout);
  assert.equal(res.outcome, "rate_limited");
  assert.equal(res.text, "Capture limit hit, retry tomorrow.");
});

test("atlas_capture: FAILED returns null text + failed outcome", () => {
  const stdout = "CAPTURE_FAILED\n";
  const res = parseProtocol("atlas_capture", stdout);
  assert.equal(res.outcome, "failed");
  assert.equal(res.text, null);
});

test("atlas_capture: empty stdout → silent", () => {
  const res = parseProtocol("atlas_capture", "");
  assert.equal(res.outcome, "silent");
  assert.equal(res.text, null);
});

test("atlas_capture: ignores unrecognised lines", () => {
  const stdout = "[debug] scanned URL\n[debug] passed guard\nCAPTURE_ARCHIVED:b\n";
  const res = parseProtocol("atlas_capture", stdout);
  assert.equal(res.outcome, "silent"); // ARCHIVED is silent
});

test("atlas_capture vs atlas_research: don't cross-contaminate", () => {
  // A RESEARCH_ANSWERED line in atlas_capture stdout should not match
  // (and vice versa).
  const stdout = "RESEARCH_ANSWERED:" + b64("hi") + "\n";
  const res = parseProtocol("atlas_capture", stdout);
  assert.equal(res.outcome, "silent");
});


// ── raw_text protocol ────────────────────────────────────────────────────────

test("raw_text: non-empty stdout posts verbatim (answered)", () => {
  const stdout = "You have 3 open tasks: groceries, taxes, call mom.\n";
  const res = parseProtocol("raw_text", stdout);
  assert.equal(res.outcome, "answered");
  assert.equal(res.text, "You have 3 open tasks: groceries, taxes, call mom.");
});

test("raw_text: multi-line stdout preserved (only outer whitespace trimmed)", () => {
  const stdout = "  Line one\nLine two  \n";
  const res = parseProtocol("raw_text", stdout);
  assert.equal(res.outcome, "answered");
  assert.equal(res.text, "Line one\nLine two");
});

test("raw_text: empty stdout → silent", () => {
  const res = parseProtocol("raw_text", "");
  assert.equal(res.outcome, "silent");
  assert.equal(res.text, null);
});

test("raw_text: whitespace-only stdout → silent", () => {
  const res = parseProtocol("raw_text", "\n   \t\n");
  assert.equal(res.outcome, "silent");
  assert.equal(res.text, null);
});

test("raw_text: no marker grammar — atlas markers post verbatim, not parsed", () => {
  // raw_text has no marker vocabulary; a line that happens to look like an
  // atlas marker is just text and is posted as-is.
  const stdout = "RESEARCH_FAILED\n";
  const res = parseProtocol("raw_text", stdout);
  assert.equal(res.outcome, "answered");
  assert.equal(res.text, "RESEARCH_FAILED");
});
