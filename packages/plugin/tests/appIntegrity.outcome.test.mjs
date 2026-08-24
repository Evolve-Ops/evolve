/**
 * Tests for the app-integrity middleware's outcome reader + result builders +
 * the wired handler.
 *
 * Auditor-grade: every adversarial path from the chip DoD is pinned here —
 *   • failing app script ⇒ HONEST failure (raw traceback never reaches model)
 *   • partial/buffered stdout that LOOKS successful + non-zero exit ⇒ failed
 *     (the exit code wins over the success-looking body)
 *   • non-UTF8 / control-char stderr ⇒ sanitized, never raw
 *   • a passing NON-app tool result ⇒ untouched (void passthrough)
 *   • a passing app result ⇒ content preserved verbatim, only details annotated
 *   • a middleware internal error ⇒ fail-to-truth (void ⇒ OC keeps original)
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/appIntegrity.outcome.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  extractCommand,
  readOutcome,
  sanitizeReason,
  textOf,
  buildFailureResult,
  buildSuccessResult,
} from "../dist/integrity/toolOutcome.js";
import { makeAppIntegrityHandler } from "../dist/integrity/AppIntegrityMiddleware.js";
import {
  resolveForTurn as resolveAttributionForTurn,
  _resetForTests as _resetAttributionForTests,
} from "../dist/apps/AppAttribution.js";

const NOOP_LOGGER = { info() {}, warn() {}, error() {}, debug() {} };

function textResult(text, details) {
  return { content: [{ type: "text", text }], details };
}

// ── extractCommand ───────────────────────────────────────────────────────────

test("extractCommand reads command/cmd/array, ignores non-shell args", () => {
  assert.equal(extractCommand({ command: "python3 a.py" }), "python3 a.py");
  assert.equal(extractCommand({ cmd: "ls" }), "ls");
  assert.equal(extractCommand({ command: ["python3", "a.py"] }), "python3 a.py");
  assert.equal(extractCommand("raw string"), "raw string");
  // Non-shell tools / empties → null (middleware passes through).
  assert.equal(extractCommand({ query: "weather" }), null);
  assert.equal(extractCommand({ input: "/Users/b/.openclaw/workspace/x.py" }), null);
  assert.equal(extractCommand(null), null);
  assert.equal(extractCommand(undefined), null);
  assert.equal(extractCommand(42), null);
  assert.equal(extractCommand({ command: "" }), null);
});

// ── readOutcome: the exit code WINS ──────────────────────────────────────────

test("non-zero exit in details ⇒ failed even with success-looking stdout", () => {
  const ev = {
    args: {},
    result: textResult("Task LA015 created successfully!", { exitCode: 1, stderr: "boom" }),
  };
  const o = readOutcome(ev);
  assert.equal(o.kind, "failed");
  assert.equal(o.exitCode, 1);
  assert.match(o.errorSummary, /boom/);
});

test("exit 0 in details ⇒ ok", () => {
  const o = readOutcome({ args: {}, result: textResult("done", { exitCode: 0 }) });
  assert.equal(o.kind, "ok");
  assert.equal(o.exitCode, 0);
});

test("non-zero exit marker in TEXT (no details) ⇒ failed", () => {
  const o = readOutcome({ args: {}, result: textResult("traceback...\nexit code: 2") });
  assert.equal(o.kind, "failed");
  assert.equal(o.exitCode, 2);
});

test("text says success but exit marker is non-zero ⇒ failed (exit wins)", () => {
  const o = readOutcome({
    args: {},
    result: textResult("All good created 3 tasks\n[exit status 137]"),
  });
  assert.equal(o.kind, "failed");
  assert.equal(o.exitCode, 137);
});

test("host isError flag ⇒ failed when no code present", () => {
  const o = readOutcome({ args: {}, isError: true, result: textResult("timed out") });
  assert.equal(o.kind, "failed");
  assert.equal(o.exitCode, null);
});

test("details.status=error ⇒ failed", () => {
  const o = readOutcome({ args: {}, result: textResult("x", { status: "error" }) });
  assert.equal(o.kind, "failed");
});

test("contradictory host shape: isError=true wins over a 0 exit code ⇒ failed", () => {
  const o = readOutcome({ args: {}, isError: true, result: textResult("done", { exitCode: 0 }) });
  assert.equal(o.kind, "failed", "a host error flag must never resolve to success");
});

test("details.error truthy wins over a 0 exit code ⇒ failed", () => {
  const o = readOutcome({ args: {}, result: textResult("ok", { exitCode: 0, error: "killed" }) });
  assert.equal(o.kind, "failed");
});

test("empty details.error is NOT an error ⇒ exit 0 stays ok", () => {
  const o = readOutcome({ args: {}, result: textResult("ok", { exitCode: 0, error: "" }) });
  assert.equal(o.kind, "ok");
});

test("no authoritative signal at all ⇒ unknown (NOT ok — passthrough)", () => {
  const o = readOutcome({ args: {}, result: textResult("hello world") });
  assert.equal(o.kind, "unknown", "absence of signal must never be certified ok");
  assert.equal(o.exitCode, null);
});

test("a crashed script with only buffered stdout ⇒ unknown, never ok", () => {
  // No details, no isError — just a traceback in the text and no exit marker.
  const o = readOutcome({ args: {}, result: textResult("Segmentation fault\ncore dumped") });
  assert.equal(o.kind, "unknown");
});

test("text-only 'exit code 0' does NOT certify success ⇒ unknown", () => {
  // A script can author "exit code 0" in its own stdout; we must not trust it.
  const o = readOutcome({ args: {}, result: textResult("did stuff\nexit code 0") });
  assert.equal(o.kind, "unknown", "self-reported zero in stdout cannot certify ok");
});

test("text markers mixing non-zero and zero ⇒ failed (non-zero is evidence)", () => {
  // Reviewer case A: a real worker failure followed by a benign 'exit code 0'.
  const o = readOutcome({
    args: {},
    result: textResult("WORKER exit code 1\nshutdown complete, exit code 0"),
  });
  assert.equal(o.kind, "failed");
  assert.equal(o.exitCode, 1);
});

test("forged 'exit code 0' in stdout + host isError=true ⇒ failed (host wins)", () => {
  const o = readOutcome({
    args: {},
    isError: true,
    result: textResult("Segmentation fault\nexit code 0", { stderr: "boom" }),
  });
  assert.equal(o.kind, "failed", "host error flag is never overridden by stdout text");
});

test(">5-digit exit code in text is not parsed ⇒ unknown (safe; never ok-certified)", () => {
  const o = readOutcome({ args: {}, result: textResult("fatal\nprocess exited with exit code 12345678901") });
  // Not a real exit code; no authoritative signal → unknown (passthrough), not ok.
  assert.equal(o.kind, "unknown");
});

test("there is NO path from a non-zero exit to ok", () => {
  for (const code of [1, 2, 126, 127, 130, 137, 255, -1]) {
    const o = readOutcome({ args: {}, result: textResult("looks fine", { exitCode: code }) });
    assert.equal(o.kind, "failed", `exit ${code} must be failed`);
  }
});

// ── sanitizeReason: non-UTF8 / control chars / truncation ────────────────────

test("sanitizeReason strips ANSI + control + replacement chars", () => {
  const ESC = String.fromCharCode(0x1b);
  const NUL = String.fromCharCode(0x00);
  const BEL = String.fromCharCode(0x07);
  const DEL = String.fromCharCode(0x7f);
  const FFFD = String.fromCharCode(0xfffd);
  const raw = `${ESC}[31mERROR${ESC}[0m:${NUL}${BEL} bad byte ${FFFD}${FFFD} here${DEL}`;
  const s = sanitizeReason(raw);
  // eslint-disable-next-line no-control-regex
  assert.ok(!/[\x00-\x1f\x7f-\x9f]/.test(s), "no control chars survive");
  assert.ok(!/�/.test(s), "no replacement chars survive");
  assert.match(s, /ERROR.*bad.*byte.*here/);
});

test("sanitizeReason keeps the TAIL and clamps length", () => {
  const raw = "HEAD " + "x".repeat(5000) + " PROXIMATE_ERROR";
  const s = sanitizeReason(raw, 200);
  assert.ok(s.length <= 200, `len ${s.length}`);
  assert.match(s, /PROXIMATE_ERROR$/);
  assert.ok(s.startsWith("…"), "ellipsis prefix on truncation");
});

test("textOf joins text blocks, ignores images", () => {
  const r = {
    content: [
      { type: "text", text: "a" },
      { type: "image", mimeType: "x", data: "y" },
      { type: "text", text: "b" },
    ],
  };
  assert.equal(textOf(r), "a\nb");
  assert.equal(textOf(null), "");
});

// ── buildFailureResult: honest, no raw traceback ─────────────────────────────

test("buildFailureResult omits the raw traceback from model content", () => {
  const tb = "Traceback (most recent call last):\n  File x\nKeyError: 'q'";
  const o = readOutcome({ args: {}, result: textResult(tb, { exitCode: 1, stderr: tb }) });
  const r = buildFailureResult("atlas-research", "/Users/b/scripts/r.py", o);
  const text = r.content[0].text;
  assert.ok(!text.includes("Traceback (most recent"), "no raw traceback header in content");
  assert.match(text, /couldn't complete/i);
  assert.match(text, /exit 1/);
  // Structured detail carries the (sanitized) machine-readable outcome.
  assert.equal(r.details.evolve_app_integrity.status, "failed");
  assert.equal(r.details.evolve_app_integrity.app_id, "atlas-research");
  assert.equal(r.details.evolve_app_integrity.exit_code, 1);
});

// ── buildSuccessResult: non-destructive ──────────────────────────────────────

test("buildSuccessResult preserves content verbatim, merges details", () => {
  const original = textResult("the real payload", { foo: "bar" });
  const o = readOutcome({ args: {}, result: original });
  const r = buildSuccessResult("appX", "/s.py", o, original);
  assert.equal(r.content, original.content, "content preserved by reference");
  assert.equal(r.details.foo, "bar", "existing details preserved");
  assert.equal(r.details.evolve_app_integrity.status, "ok");
});

test("buildSuccessResult refuses to clobber a non-object details ⇒ null", () => {
  const original = { content: [{ type: "text", text: "x" }], details: "a string details" };
  const o = readOutcome({ args: {}, result: original });
  assert.equal(buildSuccessResult("appX", "/s.py", o, original), null);
});

// ── Wired handler ────────────────────────────────────────────────────────────

const APP = { appId: "atlas-research", script: "/Users/b/.openclaw/workspace/scripts/r.py" };
const fakeRegistry = (matchFor) => ({
  lookup(command) {
    return matchFor(command) ? { ...APP } : null;
  },
});

test("handler: non-app command ⇒ void passthrough", () => {
  const h = makeAppIntegrityHandler(fakeRegistry(() => false), NOOP_LOGGER);
  const ev = { args: { command: "ls -la" }, result: textResult("files", { exitCode: 1 }) };
  assert.equal(h(ev), undefined, "non-app result is never touched, even on failure");
});

test("handler: non-shell tool (no command) ⇒ void passthrough", () => {
  const h = makeAppIntegrityHandler(fakeRegistry(() => true), NOOP_LOGGER);
  assert.equal(h({ args: { query: "x" }, result: textResult("y") }), undefined);
});

test("handler: known app FAILED ⇒ honest failure result", () => {
  const h = makeAppIntegrityHandler(fakeRegistry(() => true), NOOP_LOGGER);
  const ev = {
    args: { command: "python3 /Users/b/.openclaw/workspace/scripts/r.py" },
    result: textResult("Created!", { exitCode: 1, stderr: "KeyError" }),
  };
  const out = h(ev);
  assert.ok(out && out.result, "returns a replacement result");
  assert.equal(out.result.details.evolve_app_integrity.status, "failed");
  assert.ok(!out.result.content[0].text.includes("Created!"), "confabulation-shaped stdout dropped");
});

test("handler: known app SUCCESS ⇒ content preserved + annotated", () => {
  const h = makeAppIntegrityHandler(fakeRegistry(() => true), NOOP_LOGGER);
  const original = textResult("real answer", { exitCode: 0 });
  const out = h({ args: { command: "python3 r.py" }, result: original });
  assert.ok(out && out.result);
  assert.equal(out.result.content, original.content, "preserved verbatim");
  assert.equal(out.result.details.evolve_app_integrity.status, "ok");
});

test("handler: registry that THROWS ⇒ fail-to-truth (void, keeps original)", () => {
  const boomRegistry = {
    lookup() {
      throw new Error("registry exploded");
    },
  };
  const h = makeAppIntegrityHandler(boomRegistry, NOOP_LOGGER);
  const ev = { args: { command: "python3 r.py" }, result: textResult("orig", { exitCode: 1 }) };
  assert.equal(h(ev), undefined, "internal error never fabricates an outcome");
});

test("handler: known app with NO authoritative signal ⇒ void passthrough (unknown)", () => {
  const h = makeAppIntegrityHandler(fakeRegistry(() => true), NOOP_LOGGER);
  // No details / isError → unknown → raw result passes through untouched.
  const out = h({ args: { command: "python3 r.py" }, result: textResult("some output") });
  assert.equal(out, undefined, "absence of signal is never stamped ok");
});

test("handler: host-confirmed FAILURE with forged stdout 'ok' ⇒ honest failure", () => {
  const h = makeAppIntegrityHandler(fakeRegistry(() => true), NOOP_LOGGER);
  const out = h({
    args: { command: "python3 r.py" },
    isError: true,
    result: textResult("All tasks created! exit code 0", { stderr: "Killed" }),
  });
  assert.ok(out && out.result);
  assert.equal(out.result.details.evolve_app_integrity.status, "failed");
  assert.ok(!out.result.content[0].text.includes("All tasks created"), "forged success dropped");
});

test("handler: success-merge guard miss (non-object details) ⇒ void passthrough", () => {
  const h = makeAppIntegrityHandler(fakeRegistry(() => true), NOOP_LOGGER);
  // exitCode 0 in a non-object details: asRecord rejects the string, so there is
  // no authoritative signal → unknown → void (and even on ok, the merge guard
  // would refuse a non-object details). Either way: untouched passthrough.
  const original = { content: [{ type: "text", text: "ok" }], details: "weird" };
  assert.equal(h({ args: { command: "python3 r.py" }, result: original }), undefined);
});

// ── App attribution (AL-1.1, design §4.2 source 2) ────────────────────────────

test("handler: known app command records explicit attribution regardless of outcome", () => {
  _resetAttributionForTests();
  const h = makeAppIntegrityHandler(fakeRegistry(() => true), NOOP_LOGGER);
  const ctx = { runtime: "openclaw", runId: "run-mw-1", sessionId: "sess-mw-1" };
  // A FAILING app run still served that app — attribution records either way.
  h({
    args: { command: "python3 r.py" },
    result: textResult("boom", { exitCode: 1, stderr: "KeyError" }),
  }, ctx);
  assert.deepEqual(resolveAttributionForTurn("run-mw-1", "sess-mw-1"), {
    app_id: "atlas-research",
    app_attribution: "explicit",
    app_confidence: 1.0,
    app_attribution_source: "script_middleware",
  });
});

test("handler: non-app command records NO attribution", () => {
  _resetAttributionForTests();
  const h = makeAppIntegrityHandler(fakeRegistry(() => false), NOOP_LOGGER);
  h({ args: { command: "ls -la" }, result: textResult("files", { exitCode: 0 }) },
    { runtime: "openclaw", runId: "run-mw-2", sessionId: "sess-mw-2" });
  assert.equal(resolveAttributionForTurn("run-mw-2", "sess-mw-2").app_attribution, "none");
});

test("handler: missing context (older gateway) still passes through without recording", () => {
  _resetAttributionForTests();
  const h = makeAppIntegrityHandler(fakeRegistry(() => true), NOOP_LOGGER);
  const original = textResult("real answer", { exitCode: 0 });
  const out = h({ args: { command: "python3 r.py" }, result: original });
  assert.ok(out && out.result, "behavior unchanged with no context");
});
